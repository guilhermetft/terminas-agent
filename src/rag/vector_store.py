"""Repositório vetorial em SQLite (arquivo local) com busca por similaridade de cosseno."""

from __future__ import annotations

import heapq
import math
from array import array
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.memory.store import SQLiteStore

DEFAULT_RAG_DB_PATH = Path(".terminas") / "rag.sqlite"

MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE documents (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        path         TEXT NOT NULL UNIQUE,
        content_hash TEXT NOT NULL,
        kind         TEXT NOT NULL,
        indexed_at   TEXT NOT NULL
    );
    CREATE TABLE chunks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
        chunk_index INTEGER NOT NULL,
        start_line  INTEGER NOT NULL,
        end_line    INTEGER NOT NULL,
        content     TEXT NOT NULL,
        embedding   BLOB NOT NULL
    );
    CREATE INDEX idx_chunks_document ON chunks (document_id);
    """,
)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Similaridade de cosseno entre dois vetores de mesma dimensão (0.0 se algum for nulo)."""
    if len(a) != len(b):
        raise ValueError(f"Dimensões diferentes: {len(a)} != {len(b)}.")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return 0.0 if norm == 0 else dot / norm


def _unit(vector: Sequence[float]) -> array:
    norm = math.sqrt(sum(x * x for x in vector))
    return array("f", vector if norm == 0 else (x / norm for x in vector))


@dataclass(frozen=True)
class StoredChunk:
    index: int
    start_line: int
    end_line: int
    content: str
    vector: Sequence[float]


@dataclass(frozen=True)
class SearchHit:
    path: str
    start_line: int
    end_line: int
    content: str
    score: float

    @property
    def lines(self) -> str:
        if self.start_line == self.end_line:
            return str(self.start_line)
        return f"{self.start_line}-{self.end_line}"

    @property
    def citation(self) -> str:
        return f"{self.path}:{self.lines}"


class VectorStore:
    """Persiste documentos e chunks vetorizados; busca por força bruta (escala local)."""

    def __init__(self, path: str | Path = DEFAULT_RAG_DB_PATH) -> None:
        # Acesso serializado pelo lock do RAGEngine; permite rodar em thread auxiliar.
        self.db = SQLiteStore(path, MIGRATIONS, check_same_thread=False)

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str) -> str | None:
        row = self.db.query_one("SELECT value FROM meta WHERE key = ?", (key,))
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ------------------------------------------------------------- documents
    def get_document_hash(self, path: str) -> str | None:
        row = self.db.query_one("SELECT content_hash FROM documents WHERE path = ?", (path,))
        return None if row is None else row["content_hash"]

    def upsert_document(
        self, path: str, content_hash: str, kind: str, chunks: Sequence[StoredChunk]
    ) -> None:
        """Substitui atomicamente o documento e todos os seus chunks."""
        with self.db.conn:
            self.db.conn.execute("DELETE FROM documents WHERE path = ?", (path,))
            cursor = self.db.conn.execute(
                "INSERT INTO documents (path, content_hash, kind, indexed_at) VALUES (?, ?, ?, ?)",
                (path, content_hash, kind, datetime.now(UTC).isoformat()),
            )
            self.db.conn.executemany(
                "INSERT INTO chunks (document_id, chunk_index, start_line, end_line, content, "
                "embedding) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        cursor.lastrowid,
                        c.index,
                        c.start_line,
                        c.end_line,
                        c.content,
                        _unit(c.vector).tobytes(),
                    )
                    for c in chunks
                ],
            )

    def remove_document(self, path: str) -> bool:
        return self.db.execute("DELETE FROM documents WHERE path = ?", (path,)).rowcount > 0

    def list_documents(self) -> list[dict[str, str | int]]:
        rows = self.db.query_all(
            "SELECT d.path, d.kind, d.indexed_at, COUNT(c.id) AS chunks "
            "FROM documents d LEFT JOIN chunks c ON c.document_id = d.id "
            "GROUP BY d.id ORDER BY d.path"
        )
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, int]:
        docs = self.db.query_one("SELECT COUNT(*) AS n FROM documents")["n"]
        chunks = self.db.query_one("SELECT COUNT(*) AS n FROM chunks")["n"]
        return {"documents": docs, "chunks": chunks}

    # ---------------------------------------------------------------- search
    def search(
        self, query_vector: Sequence[float], top_k: int = 5, min_score: float = 0.0
    ) -> list[SearchHit]:
        """Top-K chunks por cosseno (vetores guardados normalizados: cosseno = produto escalar)."""
        if top_k < 1:
            raise ValueError("top_k deve ser >= 1.")
        query = _unit(query_vector)
        scored: list[tuple[float, int]] = []
        rows = self.db.query_all(
            "SELECT c.id, c.embedding FROM chunks c"
        )
        for row in rows:
            stored = array("f")
            stored.frombytes(row["embedding"])
            if len(stored) != len(query):
                raise ValueError(
                    f"Dimensão da consulta ({len(query)}) difere da indexada ({len(stored)})."
                )
            score = sum(x * y for x, y in zip(query, stored, strict=True))
            if score >= min_score:
                scored.append((score, row["id"]))
        best = heapq.nlargest(top_k, scored)
        hits: list[SearchHit] = []
        for score, chunk_id in best:
            row = self.db.query_one(
                "SELECT d.path, c.start_line, c.end_line, c.content FROM chunks c "
                "JOIN documents d ON d.id = c.document_id WHERE c.id = ?",
                (chunk_id,),
            )
            hits.append(
                SearchHit(row["path"], row["start_line"], row["end_line"], row["content"], score)
            )
        return hits

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> VectorStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
