"""Testes da camada de memória (CT-06, RF-09)."""

import sqlite3

import pytest

from src.memory import DEFAULT_DB_PATH, SCHEMA_VERSION, MemoryEngine, SQLiteStore, default_summarizer


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / ".terminas" / "memory.sqlite"


@pytest.fixture
def engine(db_path):
    with MemoryEngine(db_path) as eng:
        yield eng


# ------------------------------------------------------------------ store
def test_default_db_path_is_local_terminas_dir():
    assert DEFAULT_DB_PATH.as_posix() == ".terminas/memory.sqlite"


def test_store_creates_file_dirs_and_tables(db_path):
    with SQLiteStore(db_path) as store:
        names = {r["name"] for r in store.query_all("SELECT name FROM sqlite_master")}
        assert {"sessions", "user_profile", "episodic_memory", "kv_store"} <= names
        assert store.schema_version == SCHEMA_VERSION
    assert db_path.is_file()


def test_store_migration_is_idempotent(db_path):
    SQLiteStore(db_path).close()
    with SQLiteStore(db_path) as store:  # reabre sem recriar tabelas
        assert store.schema_version == SCHEMA_VERSION


def test_store_query_helpers(db_path):
    with SQLiteStore(db_path) as store:
        store.execute("INSERT INTO kv_store VALUES (?, ?, ?, ?)", ("n", "k", "1", "t"))
        assert store.query_one("SELECT value FROM kv_store WHERE key = ?", ("k",))["value"] == "1"
        assert store.query_one("SELECT value FROM kv_store WHERE key = ?", ("x",)) is None
        assert len(store.query_all("SELECT * FROM kv_store")) == 1


def test_store_execute_rolls_back_on_error(db_path):
    with SQLiteStore(db_path) as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.execute("INSERT INTO user_profile (key, value) VALUES ('a', 'b')")
        assert store.query_all("SELECT * FROM user_profile") == []


def test_store_closed_connection_raises_and_close_is_idempotent(db_path):
    store = SQLiteStore(db_path)
    store.close()
    store.close()
    with pytest.raises(RuntimeError):
        store.query_all("SELECT 1")


# --------------------------------------------------- CT-06: perfil/preferências
def test_ct06_preferences_survive_reconnection(db_path):
    prefs = {"theme": "dark", "preferred_model": "gemini-2.5-flash"}
    first = MemoryEngine(db_path)
    first.set_preferences(prefs)
    first.close()

    with MemoryEngine(db_path) as second:  # nova conexão SQLite
        assert second.get_preferences() == prefs


def test_preference_crud(engine):
    assert engine.get_preference("lang") is None
    assert engine.get_preference("lang", "pt-BR") == "pt-BR"
    engine.set_preference("lang", "pt-BR")
    engine.set_preference("lang", "en")  # upsert
    engine.set_preference("nested", {"a": [1, 2], "ç": "ã"})
    assert engine.get_preference("lang") == "en"
    assert engine.get_preference("nested") == {"a": [1, 2], "ç": "ã"}
    assert engine.delete_preference("lang") is True
    assert engine.delete_preference("lang") is False
    assert list(engine.get_preferences()) == ["nested"]


# --------------------------------------------------------------- long-term
def test_long_term_crud_and_namespaces(engine):
    engine.set_long_term("regra", "sempre pt-BR")
    engine.set_long_term("regra", "sempre pt-BR conciso")  # upsert
    engine.set_long_term("regra", "outro", namespace="notes")
    assert engine.get_long_term("regra") == "sempre pt-BR conciso"
    assert engine.get_long_term("regra", namespace="notes") == "outro"
    assert engine.get_long_term("nada", default=42) == 42
    assert engine.list_long_term() == {"regra": "sempre pt-BR conciso"}
    assert engine.delete_long_term("regra") is True
    assert engine.delete_long_term("regra") is False
    assert engine.list_long_term(namespace="notes") == {"regra": "outro"}


def test_long_term_survives_reconnection(db_path):
    with MemoryEngine(db_path) as eng:
        eng.set_long_term("k", {"v": 1})
    with MemoryEngine(db_path) as eng:
        assert eng.get_long_term("k") == {"v": 1}


# ----------------------------------------------------------------- working
def test_working_memory_is_volatile(db_path):
    with MemoryEngine(db_path) as eng:
        eng.set_working("x", 1)
        assert eng.get_working("x") == 1
        assert eng.get_working("y", "d") == "d"
        eng.clear_working()
        assert eng.get_working("x") is None
    with MemoryEngine(db_path) as eng:
        assert eng.get_working("x") is None


# -------------------------------------------------------------- short-term
def test_add_message_autostarts_session_and_returns_context(engine):
    assert engine.session_id is None
    engine.add_message("user", "oi")
    engine.add_message("assistant", "olá")
    assert engine.session_id is not None
    assert engine.get_context() == [
        {"role": "user", "content": "oi"},
        {"role": "assistant", "content": "olá"},
    ]


def test_get_context_returns_copies(engine):
    engine.add_message("user", "oi")
    engine.get_context()[0]["content"] = "alterado"
    assert engine.get_context()[0]["content"] == "oi"


def test_window_compression_keeps_recent_and_summarizes_old(db_path):
    with MemoryEngine(db_path, max_messages=4, keep_recent=2) as eng:
        for i in range(5):
            eng.add_message("user", f"msg {i}")
        context = eng.get_context()
        assert context[0]["role"] == "system"
        assert "msg 0" in context[0]["content"]
        assert [m["content"] for m in context[1:]] == ["msg 3", "msg 4"]
        assert len(context) - 1 <= eng.max_messages


def test_compression_uses_custom_summarizer(db_path):
    calls = []

    def summarizer(previous, messages):
        calls.append((previous, [m["content"] for m in messages]))
        return f"resumo{len(calls)}"

    with MemoryEngine(db_path, max_messages=3, keep_recent=1, summarizer=summarizer) as eng:
        for i in range(7):
            eng.add_message("user", str(i))
        assert calls[0] == ("", ["0", "1", "2"])
        assert calls[1] == ("resumo1", ["3", "4", "5"])  # resumo anterior é encadeado
        assert "resumo2" in eng.get_context()[0]["content"]


def test_default_summarizer_truncates_and_caps():
    long = "x" * 500
    out = default_summarizer("antes", [{"role": "user", "content": long}])
    assert out.startswith("antes\nuser: ")
    assert out.endswith("…")
    assert len(default_summarizer("", [{"role": "user", "content": "a b\n c"}] * 1000)) == 2000


def test_invalid_window_config_rejected(db_path):
    with pytest.raises(ValueError):
        MemoryEngine(db_path, max_messages=5, keep_recent=5)
    with pytest.raises(ValueError):
        MemoryEngine(db_path, max_messages=5, keep_recent=0)


def test_session_persist_end_load_and_list(db_path):
    eng = MemoryEngine(db_path, max_messages=4, keep_recent=2)
    sid = eng.start_session("primeira")
    for i in range(5):
        eng.add_message("user", f"m{i}")
    before = eng.get_context()
    eng.end_session()
    eng.end_session()  # sem sessão ativa: no-op
    assert eng.session_id is None and eng.get_context() == []
    assert eng.load_session("inexistente") is False

    other = eng.start_session("segunda")
    assert eng.load_session(sid) is True  # encerra 'segunda' e retoma 'primeira'
    assert eng.session_id == sid
    assert eng.get_context() == before
    sessions = {s["id"]: s for s in eng.list_sessions()}
    assert sessions[sid]["title"] == "primeira" and sessions[sid]["ended_at"] is None
    assert sessions[other]["ended_at"] is not None
    assert eng.list_sessions(limit=1)[0]["id"] == other
    eng.close()

    with MemoryEngine(db_path) as again:  # sobrevive a reconexão
        assert again.load_session(sid)
        assert again.get_context() == before


def test_start_session_closes_previous(engine):
    first = engine.start_session()
    engine.add_message("user", "a")
    second = engine.start_session()
    assert second != first
    assert engine.get_context() == []
    ended = {s["id"]: s["ended_at"] for s in engine.list_sessions()}
    assert ended[first] is not None and ended[second] is None


def test_load_same_session_keeps_it_active(engine):
    sid = engine.start_session()
    engine.add_message("user", "a")
    assert engine.load_session(sid) is True
    assert engine.get_context() == [{"role": "user", "content": "a"}]


# ---------------------------------------------------------------- episodic
def test_episodic_record_get_and_list(engine):
    sid = engine.start_session()
    ep1 = engine.record_episode(
        "indexar docs",
        "success",
        plan={"nodes": [{"id": "a"}, {"id": "b", "deps": ["a"]}]},
        summary="ok",
        metrics={"tokens": 120},
    )
    ep2 = engine.record_episode("rodar testes", "failed")
    got = engine.get_episode(ep1)
    assert got["goal"] == "indexar docs"
    assert got["session_id"] == sid
    assert got["plan"]["nodes"][1]["deps"] == ["a"]
    assert got["metrics"] == {"tokens": 120}
    bare = engine.get_episode(ep2)
    assert bare["plan"] is None and bare["metrics"] is None and bare["summary"] is None
    assert engine.get_episode(999) is None
    assert [e["id"] for e in engine.list_episodes()] == [ep2, ep1]
    assert [e["id"] for e in engine.list_episodes(status="success")] == [ep1]
    assert len(engine.list_episodes(limit=1)) == 1


def test_episode_without_session_has_null_session(engine):
    ep = engine.record_episode("meta", "success")
    assert engine.get_episode(ep)["session_id"] is None


# --------------------------------------------------------------- lifecycle
def test_engine_accepts_existing_store_and_default_path(db_path, monkeypatch, tmp_path):
    store = SQLiteStore(db_path)
    eng = MemoryEngine(store)
    assert eng.store is store
    eng.close()

    monkeypatch.chdir(tmp_path)
    with MemoryEngine() as default_eng:
        default_eng.set_preference("k", 1)
    assert (tmp_path / ".terminas" / "memory.sqlite").is_file()


def test_close_ends_active_session(db_path):
    eng = MemoryEngine(db_path)
    sid = eng.start_session()
    eng.close()
    with SQLiteStore(db_path) as store:
        row = store.query_one("SELECT ended_at FROM sessions WHERE id = ?", (sid,))
        assert row["ended_at"] is not None
