"""Turn PDFs and Markdown notes into page-anchored text chunks."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from . import ocr
from .profile import Profile

pymupdf.TOOLS.mupdf_display_errors(False)

SKIP_DIRS = {".git", ".obsidian", ".trash", "Templates", "Excalidraw", "node_modules"}
MIN_CHUNK_WORDS = 25


@dataclass
class Chunk:
    text: str
    page: int  # 1-based for PDFs, 0 for notes
    section: str = ""


@dataclass
class Extracted:
    chunks: list[Chunk] = field(default_factory=list)
    pages: int = 0
    image_only_pages: list[int] = field(default_factory=list)
    ocr_pages: int = 0


def iter_corpus(root: Path):
    """Yield indexable files under root, skipping hidden/tooling folders."""
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        if not p.is_file():
            continue
        name = p.name.lower()
        if name.endswith(".pdf"):
            yield p
        elif name.endswith(".md") and not name.endswith(".excalidraw.md"):
            yield p


def source_label(rel: Path, profile: Profile) -> str:
    """Coarse collection label used for filtering and grouping results.

    The profile's rules come first; then Markdown is "Notes" and anything else takes the
    name of its top-level folder, so organising the library in folders gives the filters.
    """
    top = rel.parts[0] if len(rel.parts) > 1 else ""
    label = profile.source_label(top, rel.name)
    if label:
        return label
    if rel.suffix.lower() == ".md":
        return "Notes"
    return top or "Other"


def display_title(path: Path) -> str:
    stem = path.stem
    stem = re.split(r"\s+--\s+", stem)[0]  # "Title -- Author -- Publisher" -> "Title"
    return stem.strip()


# ---------------------------------------------------------------- chunking

def _words(s: str) -> list[str]:
    return s.split()


def pack(paragraphs: list[str], target: int, overlap: int, min_words: int = MIN_CHUNK_WORDS) -> list[str]:
    """Greedy-pack paragraphs into ~target-word chunks with word overlap."""
    hard_max = int(target * 1.5)
    out: list[str] = []
    buf: list[str] = []
    for para in paragraphs:
        w = _words(para)
        while w:
            # Prefer ending chunks at paragraph boundaries; hard-split only past hard_max.
            room = hard_max - len(buf)
            buf += w[:room]
            w = w[room:]
            if len(buf) >= target:
                out.append(" ".join(buf))
                buf = buf[-overlap:] if overlap else []
    # Tail: merge small leftovers into the previous chunk.
    tail_new = len(buf) - (overlap if out else 0)
    if buf and tail_new > 0:
        if out and tail_new < 60:
            out[-1] = out[-1] + " " + " ".join(buf[overlap:])
        elif len(buf) >= min_words or not out:
            out.append(" ".join(buf))
    return [c for c in out if len(_words(c)) >= min_words]


# ---------------------------------------------------------------- PDF

_norm_re = re.compile(r"\d+")


def _norm(s: str) -> str:
    return _norm_re.sub("#", " ".join(s.lower().split()))


def _clean(s: str) -> str:
    s = s.replace("­", "").replace("ﬁ", "fi").replace("ﬂ", "fl")
    return " ".join(s.split())


def _section_index(doc: pymupdf.Document) -> list[str]:
    """For every page (0-based) return the 'Chapter › Section' path from the PDF outline."""
    labels = [""] * doc.page_count
    toc = doc.get_toc(simple=True)
    if not toc:
        return labels
    starts: list[tuple[int, int, str]] = []  # (page0, level, title)
    for level, title, page in toc:
        if page >= 1 and level <= 3:
            starts.append((page - 1, level, " ".join(title.split())))
    starts.sort(key=lambda t: t[0])
    current: dict[int, str] = {}
    j = 0
    for p in range(doc.page_count):
        while j < len(starts) and starts[j][0] <= p:
            _, level, title = starts[j]
            current[level] = title
            for deeper in [k for k in current if k > level]:
                del current[deeper]
            j += 1
        labels[p] = " › ".join(current[k] for k in sorted(current))[:200]
    return labels


def extract_pdf(path: Path, target: int, overlap: int, progress=lambda done, total: None) -> Extracted:
    res = Extracted()
    with pymupdf.open(path) as doc:
        res.pages = doc.page_count
        sections = _section_index(doc)
        flags = pymupdf.TEXT_DEHYPHENATE | pymupdf.TEXT_MEDIABOX_CLIP
        page_blocks: list[list[str]] = []
        for page in doc:
            blocks = [
                _clean(b[4]) for b in page.get_text("blocks", sort=True, flags=flags) if b[6] == 0
            ]
            page_blocks.append([b for b in blocks if b])

        # Scanned pages (no text layer, only an image): read them with the system's OCR.
        scanned = [i for i, blocks in enumerate(page_blocks)
                   if sum(len(b.split()) for b in blocks) < 10 and doc[i].get_images()]
        for i, lines in ocr.ocr_pages(doc, scanned, progress).items():
            page_blocks[i] = [_clean(line) for line in lines if line.strip()]
            res.ocr_pages += 1

        # Drop running headers/footers: short blocks repeated on many pages.
        freq = Counter(n for blocks in page_blocks for n in {_norm(b) for b in blocks})
        threshold = max(3, int(0.25 * res.pages))
        boiler = {n for n, c in freq.items() if c >= threshold}

        for i, blocks in enumerate(page_blocks):
            kept = [b for b in blocks if not (_norm(b) in boiler and len(b.split()) <= 30)]
            n_words = sum(len(b.split()) for b in kept)
            if n_words < MIN_CHUNK_WORDS:
                if n_words < 10 and doc[i].get_images():
                    res.image_only_pages.append(i + 1)
                continue
            for text in pack(kept, target, overlap):
                res.chunks.append(Chunk(text=text, page=i + 1, section=sections[i]))
    return res


# ---------------------------------------------------------------- Markdown

_frontmatter = re.compile(r"\A---\n.*?\n---\n", re.S)
_embed = re.compile(r"!\[\[[^\]]*\]\]|!\[[^\]]*\]\([^)]*\)")
_wikilink = re.compile(r"\[\[([^\]|#]*)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")
_mdlink = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_heading = re.compile(r"^(#{1,6})\s+(.*)$")


def _md_inline(s: str) -> str:
    s = _embed.sub("", s)
    s = _wikilink.sub(lambda m: m.group(2) or m.group(1), s)
    s = _mdlink.sub(r"\1", s)
    return s


def extract_md(path: Path, target: int, overlap: int) -> Extracted:
    raw = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    if "excalidraw-plugin" in raw[:500]:
        return Extracted()
    raw = _frontmatter.sub("", raw)
    res = Extracted(pages=1)
    headings: dict[int, str] = {}  # level -> title
    paras: list[str] = []
    cur: list[str] = []

    def flush_section():
        if cur:
            paras.append(" ".join(cur))
            cur.clear()
        if paras:
            section = " › ".join(headings[k] for k in sorted(headings))
            # Notes have short sections (a single formula); keep them.
            for text in pack(paras, target, overlap, min_words=8):
                res.chunks.append(Chunk(text=text, page=0, section=section))
            paras.clear()

    in_code = False
    for line in raw.split("\n"):
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        m = _heading.match(line) if not in_code else None
        if m:
            flush_section()
            level = len(m.group(1))
            for deeper in [k for k in headings if k >= level]:
                del headings[deeper]
            headings[level] = _md_inline(m.group(2)).strip()
            continue
        if not line.strip():
            if cur:
                paras.append(" ".join(cur))
                cur.clear()
            continue
        cur.append(_md_inline(line.strip()))
    flush_section()
    return res


def extract(path: Path, target: int, overlap: int,
            progress=lambda done, total: None) -> Extracted:
    """progress(done, total) reports OCR of scanned pages, if any."""
    if path.suffix.lower() == ".pdf":
        return extract_pdf(path, target, overlap, progress)
    return extract_md(path, target, overlap)
