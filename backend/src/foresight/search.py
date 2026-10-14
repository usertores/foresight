"""Hybrid retrieval (vectors + keywords, fused with RRF) and grounded answers."""

from __future__ import annotations

import json
from collections.abc import Iterator

from .profile import Profile
from .runtime import Runtime
from .store import Store

RRF_K = 60
CANDIDATES = 60


def search(store: Store, rt: Runtime, query: str, k: int = 10,
           sources: list[str] | None = None, max_per_file: int = 2,
           max_per_source: int | None = None) -> list[dict]:
    qv = rt.embed_query(query)
    vec = store.vector_search(qv, CANDIDATES, sources)
    kw = store.keyword_search(query, CANDIDATES, sources)

    fused: dict[str, dict] = {}
    for rank, hit in enumerate(vec):
        h = fused.setdefault(hit["id"], {**hit, "rrf": 0.0})
        h["rrf"] += 1 / (RRF_K + rank)
        h["similarity"] = round(1 - hit["_distance"], 4)
    for rank, hit in enumerate(kw):
        h = fused.setdefault(hit["id"], {**hit, "rrf": 0.0})
        h["rrf"] += 1 / (RRF_K + rank)
        h["keyword"] = True

    ranked = sorted(fused.values(), key=lambda h: h["rrf"], reverse=True)

    # One result per (file, page): overlapping chunks of the same page are redundant.
    # Cap hits per file so one long note can't crowd out the books.
    # Optionally cap per source too, so answers draw on books as well as notes.
    out, seen, per_file, per_source = [], set(), {}, {}
    for h in ranked:
        key = (h["path"], h["page"])
        if key in seen or per_file.get(h["path"], 0) >= max_per_file:
            continue
        if max_per_source and per_source.get(h["source"], 0) >= max_per_source:
            continue
        seen.add(key)
        per_file[h["path"]] = per_file.get(h["path"], 0) + 1
        per_source[h["source"]] = per_source.get(h["source"], 0) + 1
        out.append({
            "id": h["id"], "path": h["path"], "title": h["title"], "source": h["source"],
            "section": h["section"], "page": h["page"], "text": h["text"],
            "score": round(h["rrf"], 5), "similarity": h.get("similarity"),
            "keyword": h.get("keyword", False),
        })
        if len(out) >= k:
            break
    return out


RULES = """You answer questions using ONLY the excerpts from the user's study materials provided below.

Rules:
- Answer in the same language as the user's question.
- Cite sources by their number in square brackets, e.g. [1] or [2][4], right after the claim they support.
- If the excerpts don't contain the answer, say so clearly and don't make anything up.
- Be concise and clear. Use formulas when they help (LaTeX between $...$).
- Put key formulas and definitions in bold."""


def build_messages(question: str, hits: list[dict], profile: Profile) -> list[dict]:
    ctx = []
    for i, h in enumerate(hits, 1):
        where = f"{h['title']}" + (f", p. {h['page']}" if h["page"] else "")
        if h["section"]:
            where += f" — {h['section']}"
        ctx.append(f"[{i}] ({where})\n{h['text']}")
    user = "Excerpts from my materials:\n\n" + "\n\n".join(ctx) + f"\n\nQuestion: {question}"
    system = profile.tutor() + " " + RULES
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def stream_answer(rt: Runtime, question: str, hits: list[dict],
                  profile: Profile) -> Iterator[str]:
    """Yield answer text deltas from Gemma 4."""
    rt.ensure_chat()
    body = {
        "messages": build_messages(question, hits, profile),
        "stream": True,
        "temperature": 0.3,
        "max_tokens": 1200,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    with rt.http.stream("POST", f"{rt.chat_url}/v1/chat/completions", json=body) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data.strip() == "[DONE]":
                break
            delta = json.loads(data)["choices"][0].get("delta", {}).get("content")
            if delta:
                yield delta
