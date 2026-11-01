"""MemoryEngine: memória em quatro níveis (working, short-term, episódica, long-term)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.memory.store import DEFAULT_DB_PATH, SQLiteStore

Message = dict[str, str]
Summarizer = Callable[[str, list[Message]], str]

LONG_TERM_NAMESPACE = "long_term"
MAX_SUMMARY_CHARS = 2000
SNIPPET_CHARS = 120


def _now() -> str:
    return datetime.now(UTC).isoformat()


def default_summarizer(previous: str, messages: list[Message]) -> str:
    """Resumo determinístico (sem LLM): uma linha truncada por mensagem, limitado em tamanho.

    Para resumo semântico, injete um `summarizer` próprio (ex.: chamada LiteLLM)."""
    lines = [previous] if previous else []
    for msg in messages:
        content = " ".join(msg["content"].split())
        if len(content) > SNIPPET_CHARS:
            content = content[: SNIPPET_CHARS - 1] + "…"
        lines.append(f"{msg['role']}: {content}")
    return "\n".join(lines)[-MAX_SUMMARY_CHARS:]


class MemoryEngine:
    """Fachada de leitura/escrita da memória do agente.

    - Working: dict volátil, só em processo.
    - Short-term: histórico da sessão ativa com janela deslizante e compressão.
    - Episodic: tarefas passadas (SQLite).
    - Long-term: perfil/preferências (`user_profile`) e regras permanentes (`kv_store`).
    """

    def __init__(
        self,
        store: SQLiteStore | str | Path | None = None,
        *,
        max_messages: int = 20,
        keep_recent: int = 10,
        summarizer: Summarizer = default_summarizer,
    ) -> None:
        if keep_recent < 1 or keep_recent >= max_messages:
            raise ValueError("É preciso 1 <= keep_recent < max_messages.")
        if isinstance(store, SQLiteStore):
            self.store = store
        else:
            self.store = SQLiteStore(DEFAULT_DB_PATH if store is None else store)
        self.max_messages = max_messages
        self.keep_recent = keep_recent
        self._summarizer = summarizer
        self._working: dict[str, Any] = {}
        self._session_id: str | None = None
        self._messages: list[Message] = []
        self._summary = ""

    # ---------------------------------------------------------------- working
    def set_working(self, key: str, value: Any) -> None:
        self._working[key] = value

    def get_working(self, key: str, default: Any = None) -> Any:
        return self._working.get(key, default)

    def clear_working(self) -> None:
        self._working.clear()

    # ------------------------------------------------------------ short-term
    @property
    def session_id(self) -> str | None:
        return self._session_id

    def start_session(self, title: str | None = None) -> str:
        """Encerra a sessão ativa (se houver) e abre uma nova."""
        if self._session_id is not None:
            self.end_session()
        self._session_id = uuid.uuid4().hex[:12]
        self._messages = []
        self._summary = ""
        self.store.execute(
            "INSERT INTO sessions (id, title, started_at) VALUES (?, ?, ?)",
            (self._session_id, title, _now()),
        )
        return self._session_id

    def add_message(self, role: str, content: str) -> None:
        if self._session_id is None:
            self.start_session()
        self._messages.append({"role": role, "content": content})
        if len(self._messages) > self.max_messages:
            self._compress()
        self._persist_session()

    def get_context(self) -> list[Message]:
        """Janela de contexto atual: resumo (se houver) + mensagens recentes."""
        context: list[Message] = []
        if self._summary:
            context.append(
                {"role": "system", "content": f"Resumo da conversa anterior:\n{self._summary}"}
            )
        return context + [dict(m) for m in self._messages]

    def end_session(self) -> None:
        if self._session_id is None:
            return
        self._persist_session()
        self.store.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?", (_now(), self._session_id)
        )
        self._session_id = None
        self._messages = []
        self._summary = ""

    def load_session(self, session_id: str) -> bool:
        """Retoma uma sessão persistida como sessão ativa. Retorna False se não existir."""
        row = self.store.query_one(
            "SELECT summary, history FROM sessions WHERE id = ?", (session_id,)
        )
        if row is None:
            return False
        if self._session_id is not None and self._session_id != session_id:
            self.end_session()
        self._session_id = session_id
        self._summary = row["summary"]
        self._messages = json.loads(row["history"])
        self.store.execute("UPDATE sessions SET ended_at = NULL WHERE id = ?", (session_id,))
        return True

    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.store.query_all(
            "SELECT id, title, started_at, ended_at FROM sessions "
            "ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def _compress(self) -> None:
        old = self._messages[: -self.keep_recent]
        self._messages = self._messages[-self.keep_recent :]
        self._summary = self._summarizer(self._summary, old)

    def _persist_session(self) -> None:
        self.store.execute(
            "UPDATE sessions SET summary = ?, history = ? WHERE id = ?",
            (self._summary, json.dumps(self._messages, ensure_ascii=False), self._session_id),
        )

    # ---------------------------------------------------------- user profile
    def set_preference(self, key: str, value: Any) -> None:
        self.store.execute(
            "INSERT INTO user_profile (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), _now()),
        )

    def set_preferences(self, preferences: dict[str, Any]) -> None:
        for key, value in preferences.items():
            self.set_preference(key, value)

    def get_preference(self, key: str, default: Any = None) -> Any:
        row = self.store.query_one("SELECT value FROM user_profile WHERE key = ?", (key,))
        return default if row is None else json.loads(row["value"])

    def get_preferences(self) -> dict[str, Any]:
        rows = self.store.query_all("SELECT key, value FROM user_profile ORDER BY key")
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def delete_preference(self, key: str) -> bool:
        return self.store.execute("DELETE FROM user_profile WHERE key = ?", (key,)).rowcount > 0

    # ------------------------------------------------- long-term / kv_store
    def set_long_term(self, key: str, value: Any, namespace: str = LONG_TERM_NAMESPACE) -> None:
        self.store.execute(
            "INSERT INTO kv_store (namespace, key, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(namespace, key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (namespace, key, json.dumps(value, ensure_ascii=False), _now()),
        )

    def get_long_term(
        self, key: str, default: Any = None, namespace: str = LONG_TERM_NAMESPACE
    ) -> Any:
        row = self.store.query_one(
            "SELECT value FROM kv_store WHERE namespace = ? AND key = ?", (namespace, key)
        )
        return default if row is None else json.loads(row["value"])

    def list_long_term(self, namespace: str = LONG_TERM_NAMESPACE) -> dict[str, Any]:
        rows = self.store.query_all(
            "SELECT key, value FROM kv_store WHERE namespace = ? ORDER BY key", (namespace,)
        )
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def delete_long_term(self, key: str, namespace: str = LONG_TERM_NAMESPACE) -> bool:
        cur = self.store.execute(
            "DELETE FROM kv_store WHERE namespace = ? AND key = ?", (namespace, key)
        )
        return cur.rowcount > 0

    # -------------------------------------------------------------- episodic
    def record_episode(
        self,
        goal: str,
        status: str,
        *,
        plan: Any = None,
        summary: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> int:
        cur = self.store.execute(
            "INSERT INTO episodic_memory "
            "(session_id, goal, status, plan, summary, metrics, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self._session_id,
                goal,
                status,
                None if plan is None else json.dumps(plan, ensure_ascii=False),
                summary,
                None if metrics is None else json.dumps(metrics, ensure_ascii=False),
                _now(),
            ),
        )
        return int(cur.lastrowid)

    def get_episode(self, episode_id: int) -> dict[str, Any] | None:
        row = self.store.query_one("SELECT * FROM episodic_memory WHERE id = ?", (episode_id,))
        return None if row is None else self._episode_to_dict(row)

    def list_episodes(self, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM episodic_memory"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [self._episode_to_dict(r) for r in self.store.query_all(sql, params)]

    @staticmethod
    def _episode_to_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        for field in ("plan", "metrics"):
            if data[field] is not None:
                data[field] = json.loads(data[field])
        return data

    # -------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self.end_session()
        self.store.close()

    def __enter__(self) -> MemoryEngine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
