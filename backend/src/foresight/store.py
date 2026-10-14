"""LanceDB table of chunks + a JSON manifest of indexed files."""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import lancedb
import numpy as np
import pyarrow as pa
from lancedb.index import FTS
from lancedb.table import LanceTable

TABLE = "chunks"
COLUMNS = ["id", "path", "source", "title", "section", "page", "text"]
# lancedb 0.25 (the last release with Intel Mac wheels) has no config= on the sync
# create_index; its create_fts_index builds the same index with FTS()'s defaults.
_FTS_VIA_CONFIG = "config" in inspect.signature(LanceTable.create_index).parameters


def _schema(dims: int) -> pa.Schema:
    return pa.schema([
        ("id", pa.string()),
        ("path", pa.string()),      # relative to the corpus root
        ("source", pa.string()),
        ("title", pa.string()),
        ("section", pa.string()),
        ("page", pa.int32()),
        ("text", pa.string()),
        ("vector", pa.list_(pa.float32(), dims)),
    ])


def _quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


class Store:
    def __init__(self, data_dir: Path, dims: int):
        self.dir = data_dir
        self.db = lancedb.connect(data_dir / "index.lance")
        if TABLE in self.db.table_names():
            self.tbl = self.db.open_table(TABLE)
        else:
            self.tbl = self.db.create_table(TABLE, schema=_schema(dims))
        self.manifest_path = data_dir / "manifest.json"
        self.manifest: dict[str, dict] = (
            json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if self.manifest_path.exists() else {}
        )

    # ------------------------------------------------------------ writes

    def save_manifest(self) -> None:
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.manifest_path)

    def delete_path(self, rel: str) -> None:
        self.tbl.delete(f"path = {_quote(rel)}")

    def add(self, rows: list[dict]) -> None:
        if rows:
            self.tbl.add(rows)

    def finalize(self) -> None:
        """Rebuild the keyword index and compact fragments after a batch of writes."""
        if self.tbl.count_rows():
            if _FTS_VIA_CONFIG:
                self.tbl.create_index("text", config=FTS(), replace=True)
            else:
                self.tbl.create_fts_index("text", replace=True)
        self.tbl.optimize()

    # ------------------------------------------------------------ reads

    def count(self) -> int:
        return self.tbl.count_rows()

    def sources(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for meta in self.manifest.values():
            if meta.get("chunks"):
                counts[meta["source"]] = counts.get(meta["source"], 0) + meta["chunks"]
        return dict(sorted(counts.items()))

    @staticmethod
    def _where(sources: list[str] | None) -> str | None:
        if not sources:
            return None
        return "source IN (" + ", ".join(_quote(s) for s in sources) + ")"

    def vector_search(self, v: np.ndarray, limit: int, sources: list[str] | None) -> list[dict]:
        q = self.tbl.search(v).distance_type("cosine").limit(limit).select(COLUMNS + ["_distance"])
        where = self._where(sources)
        if where:
            q = q.where(where, prefilter=True)
        return q.to_list()

    def keyword_search(self, text: str, limit: int, sources: list[str] | None) -> list[dict]:
        # Strip query-syntax characters so user input can't break the FTS parser.
        clean = re.sub(r"[^\w\s]", " ", text).strip()
        if not clean:
            return []
        q = self.tbl.search(clean, query_type="fts").limit(limit).select(COLUMNS + ["_score"])
        where = self._where(sources)
        if where:
            q = q.where(where, prefilter=True)
        try:
            return q.to_list()
        except Exception:
            return []  # no FTS index yet
