"""After the class: structured notes (map-reduce over the transcript), Q&A, Obsidian export."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import numpy as np

from .meetings import SPEAKERS, MeetingManager, fmt_ts
from .profile import Profile, language_name
from .search import search

CHARS_PER_TOKEN = 3.3  # conservative: measured on Spanish with Gemma's tokenizer
SECTION_CHARS = 14000  # ~4.2k tokens of transcript per "map" call
FINAL_INPUT_TOKENS = 4800  # leaves ~3.3k tokens for the notes in an 8k context
MAX_NOTES_TOKENS = 4000
MIN_NOTES_TOKENS = 1200


def notes_system(profile: Profile, language: str) -> str:
    context = f"\n{profile.context}" if profile.context else ""
    return f"""You are an excellent note-taker for {profile.classes()}.{context}
You work only from the class transcript, the student's quick notes and their study material.
Write in {language_name(language)}, clearly and didactically. Formulas in LaTeX between $...$ (or $$...$$ on their own line).
Never invent facts that are not in the transcript or the material."""


MAP_PROMPT = """Excerpt of a class transcript (minutes {a}–{b}):

{text}

Write concise bullet-point notes on this excerpt: concepts explained, definitions, formulas,
numerical examples (with the numbers), tips and exam advice the teacher gives, and any homework
assigned. Keep the approximate minute [mm:ss] of the important points. No introduction or conclusion."""

CONDENSE_PROMPT = "Condense these notes into bullet points without losing formulas, numbers or minutes:\n\n"

SCRATCH_INPUT = """## The student's quick notes (with the minute they were written)
{scratch}

## What was said in class around each note
{scratch_ctx}

"""

STRUCTURE_HEAD = """---
Write the final notes of this class in Markdown with EXACTLY this structure, headings included,
all in {language}. Synthesise: don't retell the transcript minute by minute. Finish ALL the
sections; if space runs short, shorten the examples rather than leave the last sections unwritten.

# <descriptive title of what the class covered>
## Summary
3-5 sentences.
## Key concepts
Each concept in bold with its explanation (1-3 sentences).
## Formulas
(leave this section out if the class had no formulas)
## Examples and exercises from the class
The important ones, with their numbers, in 2-4 lines each.
"""

# Only requested when the student wrote quick notes; otherwise the model fills it by
# re-telling the class, which duplicates the examples and eats the output budget.
STRUCTURE_MY_NOTES = """## My notes, expanded
Each of the student's quick notes, developed with what the teacher explained at that moment. Start each one with its minute [mm:ss].
"""

STRUCTURE_TAIL = """## Exam tips
## Homework and next steps
## To review
Which related material is worth reviewing and why, citing it as [n]."""


def build_final_prompt(title: str, duration: int, scratch: str | None, scratch_ctx: str,
                       body_title: str, body: str, refs: str, language: str) -> str:
    parts = [f"# Class: {title} ({duration} min)\n\n"]
    if scratch:
        parts.append(SCRATCH_INPUT.format(scratch=scratch, scratch_ctx=scratch_ctx))
    parts.append(f"## {body_title}\n{body}\n\n## Related material from the student's library\n{refs}\n\n")
    parts.append(STRUCTURE_HEAD.format(language=language_name(language))
                 + (STRUCTURE_MY_NOTES if scratch else "") + STRUCTURE_TAIL)
    return "".join(parts)


def _tokens(s: str) -> int:
    return int(len(s) / CHARS_PER_TOKEN)


def transcript_text(segments: list[dict], multi: bool) -> str:
    lines = []
    for s in segments:
        who = f"{SPEAKERS.get(s['source'], s['source'])}: " if multi else ""
        lines.append(f"[{fmt_ts(s['t0'])}] {who}{s['text']}")
    return "\n".join(lines)


def _sections(segments: list[dict], multi: bool) -> list[tuple[float, float, str]]:
    out, cur, start = [], [], None
    size = 0
    for s in segments:
        line = transcript_text([s], multi)
        if cur and size + len(line) > SECTION_CHARS:
            out.append((start, s["t0"], "\n".join(cur)))
            cur, size, start = [], 0, None
        start = s["t0"] if start is None else start
        cur.append(line)
        size += len(line) + 1
    if cur:
        out.append((start, segments[-1]["t1"], "\n".join(cur)))
    return out


class NotesWriter:
    def __init__(self, mgr: MeetingManager):
        self.mgr = mgr
        self.rt = mgr.rt

    # -------------------------------------------------------------- gemma calls

    def _chat(self, messages: list[dict], max_tokens: int, stream: bool = False,
              info: dict | None = None):
        self.rt.ensure_chat()
        body = {"messages": messages, "temperature": 0.3, "max_tokens": max_tokens,
                "chat_template_kwargs": {"enable_thinking": False}, "stream": stream}
        url = f"{self.rt.chat_url}/v1/chat/completions"
        if not stream:
            r = self.rt.http.post(url, json=body)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()

        def gen():
            with self.rt.http.stream("POST", url, json=body) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line.startswith("data: ") or line[6:].strip() == "[DONE]":
                        continue
                    choice = json.loads(line[6:])["choices"][0]
                    if choice.get("finish_reason") and info is not None:
                        info["finish"] = choice["finish_reason"]
                    delta = choice.get("delta", {}).get("content")
                    if delta:
                        yield delta
        return gen()

    # -------------------------------------------------------------- notes

    def generate(self, mid: str) -> Iterator[tuple[str, object]]:
        """Yields ("progress", str) | ("token", str) | ("done", notes)."""
        data = self.mgr.get(mid)
        meta, segs, scratch = data["meta"], data["segments"], data["scratch"]
        if not segs:
            yield "error", "The transcript is empty."
            return
        multi = len({s["source"] for s in segs}) > 1
        duration = round((meta.get("duration") or segs[-1]["t1"]) / 60)
        language = meta.get("language", self.mgr.profile.language)
        system = notes_system(self.mgr.profile, language)

        # Remote: the PC decides which model answers; locally it is override or the selector.
        model = (self.rt.chat_loaded() if self.rt.remote
                 else (self.rt.override or self.mgr.s.chat_model).stem)
        slow = "e4b" in model.lower()
        label = "Gemma 4 E4B" if slow else "Gemma 4 E2B"

        # Context around each quick note: what was said in the 90 s before / 30 s after.
        scratch = [l for l in scratch if l.get("text", "").strip()]
        scratch_lines = "\n".join(f"- [{fmt_ts(l['t'])}] {l['text'].strip()}" for l in scratch)
        ctx_parts = []
        for l in scratch:
            near = [s for s in segs if l["t"] - 90 <= s["t0"] <= l["t"] + 30]
            if near:
                ctx_parts.append(f"For [{fmt_ts(l['t'])}] \"{l['text'].strip()}\":\n"
                                 + " ".join(s["text"] for s in near))
        scratch_ctx = "\n\n".join(ctx_parts)
        while _tokens(scratch_ctx) > 1500 and ctx_parts:
            ctx_parts = [p[: int(len(p) * 0.8)] for p in ctx_parts]
            scratch_ctx = "\n\n".join(ctx_parts)

        # Related material from the library: search with the notes and slices of the class.
        queries = [l["text"] for l in scratch][:6]
        for _, _, text in _sections(segs, False):
            words = text.split()
            queries.append(" ".join(words[len(words) // 2: len(words) // 2 + 120]))
        refs, seen = [], set()
        for q in queries:
            for h in search(self.mgr.store, self.rt, q, k=2, max_per_file=1):
                key = (h["path"], h["page"])
                if key not in seen and (h["similarity"] or 0) >= 0.7 and len(refs) < 6:
                    seen.add(key)
                    refs.append(h)
        refs_text = "\n".join(
            f"[{i}] {h['source']} · {h['title']}" + (f", p. {h['page']}" if h["page"] else "")
            + (f" — {h['section']}" if h["section"] else "") + f"\n{h['text'][:350]}"
            for i, h in enumerate(refs, 1)) or "(nothing relevant)"

        # Body: the full transcript if it fits, otherwise map-reduce partial notes.
        detailed: list[str] = []
        full = transcript_text(segs, multi)
        fixed = _tokens(scratch_lines + scratch_ctx + refs_text) + 700
        if _tokens(full) + fixed <= FINAL_INPUT_TOKENS:
            body_title, body = "Transcript", full
        else:
            parts = _sections(segs, multi)
            partials = []
            # A 3-hour class is ~11 parts: summarising them with E4B (~7 tok/s on a 4 GB GPU)
            # would take ~40 min, with E2B a couple. The selected model still writes the notes.
            with self.rt.prefer_fast():
                for i, (a, b, text) in enumerate(parts, 1):
                    yield "progress", f"Summarising the class: part {i} of {len(parts)}…"
                    partials.append(f"### {fmt_ts(a)}–{fmt_ts(b)}\n" + self._chat([
                        {"role": "system", "content": system},
                        {"role": "user", "content": MAP_PROMPT.format(a=fmt_ts(a), b=fmt_ts(b), text=text)},
                    ], max_tokens=650))
                # Kept whole for the notes: condensing loses detail in long classes.
                detailed = list(partials)
                # Condense pairs until the partial notes fit the final call.
                while _tokens("\n\n".join(partials)) + fixed > FINAL_INPUT_TOKENS and len(partials) > 1:
                    yield "progress", "Condensing the partial notes…"
                    merged = []
                    for j in range(0, len(partials), 2):
                        pair = "\n\n".join(partials[j:j + 2])
                        merged.append(self._chat([
                            {"role": "system", "content": system},
                            {"role": "user", "content": CONDENSE_PROMPT + pair},
                        ], max_tokens=700))
                    partials = merged
            body_title, body = "Class notes by part", "\n\n".join(partials)

        if not self.rt.remote and (self.rt.chat_loaded() != model or not self.rt.chat_ready()):
            yield "progress", f"Loading {label}…"
        self.rt.ensure_chat()
        prompt = build_final_prompt(meta["title"], duration, scratch_lines or None, scratch_ctx,
                                    body_title, body, refs_text, language)
        # Give the notes whatever the context has left (counted with the real tokenizer).
        used = self.rt.count_tokens(system + "\n" + prompt) + 64  # + chat template
        max_tokens = max(MIN_NOTES_TOKENS, min(MAX_NOTES_TOKENS, self.mgr.s.chat_ctx - used))
        yield "progress", (f"{label} is reading the class…"
                           + (" With E4B the notes take a few minutes; they appear here as they are written."
                              if slow else ""))
        notes, info = "", {}
        for delta in self._chat([{"role": "system", "content": system},
                                 {"role": "user", "content": prompt}], max_tokens=max_tokens,
                                stream=True, info=info):
            notes += delta
            yield "token", delta

        notes = notes.strip()
        if info.get("finish") == "length":
            notes += ("\n\n> [!warning] The notes were cut off for length. Try regenerating them; "
                      "if it happens again, the class is too dense for a single pass.")
        if detailed:  # long class: the per-part notes keep what the summary had to leave out
            notes += "\n\n---\n## Detailed notes by part\n\n" + "\n\n".join(detailed)
        if refs:
            notes += "\n\n---\n**References**\n" + "\n".join(
                f"[{i}] {h['source']} · {h['title']}" + (f", p. {h['page']}" if h["page"] else "")
                for i, h in enumerate(refs, 1))
        d = self.mgr.dir / mid
        (d / "notes.md").write_text(notes, encoding="utf-8")
        (d / "notes_refs.json").write_text(json.dumps(refs, ensure_ascii=False), encoding="utf-8")
        m = re.match(r"#\s+(.+)", notes)
        if m and meta["title"].startswith("Class "):
            meta["title"] = m.group(1).strip()[:120]
        if meta.get("exported_to"):
            meta["export_stale"] = True  # the copy in Obsidian is now older than these notes
        self.mgr.save_meta(mid, meta)
        yield "done", {"notes": notes, "refs": refs, "title": meta["title"]}

    # -------------------------------------------------------------- Q&A over the class

    def _windows(self, mid: str, segs: list[dict]) -> tuple[list[dict], np.ndarray]:
        """~120-word windows of the transcript, embedded and cached per transcript length."""
        cache = self.mgr.dir / mid / "windows.npz"
        if cache.exists():
            z = np.load(cache, allow_pickle=False)
            if int(z["n"]) == len(segs):
                return json.loads(str(z["meta"])), z["vecs"]
        wins, cur, t0 = [], [], None
        for s in segs:
            t0 = s["t0"] if t0 is None else t0
            cur.append(s["text"])
            if len(" ".join(cur).split()) >= 120:
                wins.append({"t0": t0, "text": " ".join(cur)})
                cur, t0 = cur[-1:], s["t0"]
        if cur:
            wins.append({"t0": t0, "text": " ".join(cur)})
        vecs = np.zeros((0, self.mgr.s.embed_dims), dtype=np.float32)
        for i in range(0, len(wins), 32):
            v = self.rt.embed_documents([("class", w["text"]) for w in wins[i:i + 32]])
            vecs = np.vstack([vecs, v])
        np.savez(cache, n=len(segs), meta=json.dumps(wins, ensure_ascii=False), vecs=vecs)
        return wins, vecs

    def ask(self, mid: str, question: str) -> Iterator[tuple[str, object]]:
        segs = self.mgr.segments(mid)
        if not segs:
            yield "error", "The transcript is empty."
            return
        self.rt.start_embed()
        multi = len({s["source"] for s in segs}) > 1
        full = transcript_text(segs, multi)
        if _tokens(full) <= 4500:
            # Short enough: give Gemma the whole class, nothing gets lost between windows.
            class_hits = [{"kind": "class", "t0": segs[0]["t0"], "text": full,
                           "title": "Full transcript"}]
        else:
            wins, vecs = self._windows(mid, segs)
            qv = self.rt.embed_query(question)
            top = np.argsort(-(vecs @ qv))[:5] if len(wins) else []
            class_hits = [{"kind": "class", "t0": wins[i]["t0"], "text": wins[i]["text"],
                           "title": f"Class {fmt_ts(wins[i]['t0'])}"} for i in sorted(top)]
        lib_hits = [{"kind": "material", **h}
                    for h in search(self.mgr.store, self.rt, question, k=3, max_per_source=2)
                    if (h["similarity"] or 0) >= 0.72]
        sources = class_hits + lib_hits
        yield "sources", sources
        ctx = "\n\n".join(
            f"[{i}] ({h['title'] if h['kind'] == 'class' else h['title'] + ', p. ' + str(h['page'])})\n{h['text']}"
            for i, h in enumerate(sources, 1))
        about = f" on {self.mgr.profile.subject}" if self.mgr.profile.subject else ""
        for delta in self._chat([
            {"role": "system", "content": f"You answer questions about a class{about}. The main "
             "source is the class transcript; the study material only complements it. Answer "
             "exactly what is asked, in the language of the question, cite with [n] and don't "
             "make anything up. If the class didn't cover it, say so."},
            {"role": "user", "content": f"Sources:\n\n{ctx}\n\nQuestion: {question}"},
        ], max_tokens=900, stream=True):
            yield "token", delta

    # -------------------------------------------------------------- export

    def vault_dir(self) -> Path | None:
        root = self.mgr.s.corpus_dir
        for p in sorted(root.glob("*/.obsidian")) + sorted(root.glob(".obsidian")):
            return p.parent
        return None

    def export(self, mid: str) -> Path:
        data = self.mgr.get(mid)
        meta, notes, segs = data["meta"], data["notes"], data["segments"]
        if not notes:
            raise ValueError("Create the notes first")
        vault = self.vault_dir()
        target_dir = (vault / "Classes") if vault else (self.mgr.s.corpus_dir / "Classes")
        target_dir.mkdir(parents=True, exist_ok=True)
        created = datetime.fromisoformat(meta["created"])
        safe = re.sub(r'[\\/:*?"<>|#^\[\]]', "", meta["title"]).strip()[:90] or "Class"
        path = target_dir / f"{created:%Y-%m-%d} {safe}.md"

        multi = len({s["source"] for s in segs}) > 1
        transcript = "\n".join(f"> {line}  " for line in transcript_text(segs, multi).splitlines())
        front = (f"---\ntype: class\ndate: {created:%Y-%m-%d %H:%M}\n"
                 f"duration_min: {round(meta.get('duration', 0) / 60)}\nsource: Foresight\n---\n\n")
        path.write_text(front + notes + "\n\n## Transcript\n> [!note]- Full transcript\n"
                        + transcript + "\n", encoding="utf-8")
        meta["exported_to"] = str(path)
        meta.pop("export_stale", None)
        self.mgr.save_meta(mid, meta)
        return path
