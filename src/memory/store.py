"""Gerenciador de conexão SQLite e migração de esquema da memória do Termina."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path(".terminas") / "memory.sqlite"

# Cada item é uma migração; o índice + 1 é a versão do esquema (PRAGMA user_version).
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE sessions (
        id          TEXT PRIMARY KEY,
        title       TEXT,
        started_at  TEXT NOT NULL,
        ended_at    TEXT,
        summary     TEXT NOT NULL DEFAULT '',
        history     TEXT NOT NULL DEFAULT '[]'
    );
    CREATE TABLE user_profile (
        key         TEXT PRIMARY KEY,
        value       TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE TABLE episodic_memory (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  TEXT,
        goal        TEXT NOT NULL,
        status      TEXT NOT NULL,
        plan        TEXT,
        summary     TEXT,
        metrics     TEXT,
        created_at  TEXT NOT NULL
    );
    CREATE INDEX idx_episodic_created ON episodic_memory (created_at);
    CREATE TABLE kv_store (
        namespace   TEXT NOT NULL,
        key         TEXT NOT NULL,
        value       TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        PRIMARY KEY (namespace, key)
    );
    """,
)

SCHEMA_VERSION = len(MIGRATIONS)


class SQLiteStore:
    """Conexão SQLite local com criação automática do arquivo e migrações versionadas."""

    def __init__(
        self,
        path: str | Path = DEFAULT_DB_PATH,
        migrations: tuple[str, ...] = MIGRATIONS,
    ) -> None:
        self.path = Path(path)
        self.migrations = migrations
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Conexão SQLite já foi fechada.")
        return self._conn

    @property
    def schema_version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        current = self.schema_version
        for version, script in enumerate(self.migrations, start=1):
            if version <= current:
                continue
            with self.conn:  # commit/rollback por migração
                self.conn.executescript(script)
                self.conn.execute(f"PRAGMA user_version = {version}")

    def execute(self, sql: str, params: Iterable[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        with self.conn:
            return self.conn.execute(sql, params)

    def query_all(
        self, sql: str, params: Iterable[Any] | dict[str, Any] = ()
    ) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def query_one(
        self, sql: str, params: Iterable[Any] | dict[str, Any] = ()
    ) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
