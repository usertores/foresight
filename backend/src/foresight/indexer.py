"""Incremental indexer: only new/changed files are extracted and embedded."""

from __future__ import annotations

import hashlib
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import ocr
from .config import Settings
from .extract import display_title, extract, iter_corpus, source_label
from .profile import Profile
from .runtime import Runtime
from .store import Store

EMBED_BATCH = 32


@dataclass
class Progress:
    state: str = "idle"  # idle | scanning | indexing | finalizing | done | error
    files_total: int = 0
    files_done: int = 0
    chunks_done: int = 0
    current: str = ""
    started: float = 0.0
    finished: float = 0.0
    skipped_duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    needs_ocr: list[str] = field(default_factory=list)


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class Indexer:
    def __init__(self, s: Settings, store: Store, rt: Runtime, profile: Profile):
        self.s, self.store, self.rt, self.profile = s, store, rt, profile
        self.progress = Progress()
        self._lock = threading.Lock()
        self._cancel = threading.Event()

    @property
    def running(self) -> bool:
        return self.progress.state in ("scanning", "indexing", "finalizing")

    def start_background(self, force: bool = False) -> bool:
        if self.running:
            return False
        threading.Thread(target=self.run, kwargs={"force": force}, daemon=True).start()
        return True

    def cancel(self) -> None:
        self._cancel.set()

    def run(self, force: bool = False, log=lambda *_: None) -> Progress:
        with self._lock:
            self._cancel.clear()
            p = self.progress = Progress(state="scanning", started=time.time())
            try:
                self._run(p, force, log)
                p.state = "done"
            except Exception as e:
                p.state = "error"
                p.errors.append(f"{type(e).__name__}: {e}")
                log(traceback.format_exc())
            p.finished = time.time()
            p.current = ""
            return p

    def _run(self, p: Progress, force: bool, log) -> None:
        root = self.s.corpus_dir
        man = self.store.manifest
        # Organised sub-folders win over loose copies at the root when contents are identical.
        files = sorted(iter_corpus(root), key=lambda f: (-len(f.relative_to(root).parts), str(f)))
        present = {f.relative_to(root).as_posix(): f for f in files}

        for rel in [r for r in man if r not in present]:
            self.store.delete_path(rel)
            del man[rel]

        todo: list[tuple[str, Path]] = []
        for rel, f in present.items():
            st = f.stat()
            meta = man.get(rel)
            changed = not meta or meta["mtime"] != st.st_mtime or meta["size"] != st.st_size
            # Scanned files indexed before OCR existed get another pass.
            retry_ocr = (meta and meta.get("image_only_pages", 0) > 0
                         and "ocr_pages" not in meta and ocr.available())
            if force or changed or retry_ocr:
                todo.append((rel, f))
        p.files_total = len(todo)
        todo_rels = {r for r, _ in todo}
        p.needs_ocr = [r for r, m in man.items() if r not in todo_rels
                       and m.get("image_only_pages", 0) > 0.8 * max(m.get("pages", 1), 1)]
        if not todo:
            return

        self.rt.start_embed()
        hashes = {m["sha1"]: r for r, m in man.items()
                  if m.get("sha1") and r not in todo_rels and not m.get("duplicate_of")}
        p.state = "indexing"
        for rel, f in todo:
            if self._cancel.is_set():
                break
            p.current = rel
            st = f.stat()
            sha = _sha1(f)
            relp = Path(rel)
            meta = {"mtime": st.st_mtime, "size": st.st_size, "sha1": sha,
                    "source": source_label(relp, self.profile), "title": display_title(relp), "chunks": 0}
            if sha in hashes and hashes[sha] != rel:
                meta["duplicate_of"] = hashes[sha]
                self.store.delete_path(rel)
                man[rel] = meta
                p.skipped_duplicates += 1
                p.files_done += 1
                continue
            hashes[sha] = rel
            try:
                # Scanned PDFs can spend minutes in OCR: show it, so it doesn't look stuck.
                show = lambda i, n: setattr(p, "current", f"OCR {i}/{n} · {rel}")
                ex = extract(f, self.s.chunk_words, self.s.chunk_overlap, progress=show)
            except Exception as e:
                p.errors.append(f"{rel}: {e}")
                p.files_done += 1
                continue
            meta["pages"] = ex.pages
            meta["image_only_pages"] = len(ex.image_only_pages)
            meta["ocr_pages"] = ex.ocr_pages
            if ex.pages and len(ex.image_only_pages) > 0.8 * ex.pages:
                p.needs_ocr.append(rel)

            rows = []
            for i in range(0, len(ex.chunks), EMBED_BATCH):
                batch = ex.chunks[i : i + EMBED_BATCH]
                vecs = self.rt.embed_documents([(meta["title"], c.text) for c in batch])
                for j, (c, v) in enumerate(zip(batch, vecs)):
                    rows.append({
                        "id": f"{sha[:12]}-{i + j}", "path": rel, "source": meta["source"],
                        "title": meta["title"], "section": c.section, "page": c.page,
                        "text": c.text, "vector": v.tolist(),
                    })
                p.chunks_done += len(batch)
            self.store.delete_path(rel)
            self.store.add(rows)
            meta["chunks"] = len(rows)
            man[rel] = meta
            p.files_done += 1
            log(f"[{p.files_done}/{p.files_total}] {len(rows):5d} chunks  {rel}")
            if p.files_done % 20 == 0:
                self.store.save_manifest()

        self.store.save_manifest()
        p.state = "finalizing"
        p.current = "keyword index"
        self.store.finalize()
