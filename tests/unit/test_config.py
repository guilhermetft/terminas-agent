"""Configuração (config.yaml), comportamento customizável e log de auditoria."""

import json
import threading
from pathlib import Path

import pytest

from src.core import JsonlAuditLog
from src.memory import MemoryEngine
from src.memory.config import (
    AppConfig,
    BehaviorConfig,
    BehaviorManager,
    ConfigError,
    apply_api_keys,
    expand_env,
    load_config,
)

ROOT = Path(__file__).resolve().parents[2]


def write(tmp_path, text, name="config.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# =================================================================== carregamento
def test_defaults_match_the_spec():
    config = AppConfig()
    assert config.limits.max_steps == 15 and config.limits.timeout_seconds == 120
    assert config.behavior.language == "pt-BR" and config.behavior.rules == []
    assert config.data_dir == Path(".terminas")
    assert config.memory_db == Path(".terminas/memory.sqlite") and config.rag_db.name == "rag.sqlite"
    assert config.audit_log == Path(".terminas/logs/audit.jsonl")
    assert config.models_dir.name == "models" and config.history_file.name == "history"


def test_config_example_file_is_valid_and_loadable():
    config = load_config(ROOT / "config.example.yaml", environ={})
    assert config.mcp_servers == [] and config.skills.plugins_dir == "src/skills/plugins"
    assert config.llm.api_keys == {"anthropic": "", "gemini": "", "openai": ""}  # ${VAR} sem valor
    assert config.paths.data_dir == ".terminas"


def test_load_custom_config_with_env_expansion(tmp_path):
    path = write(
        tmp_path,
        """
behavior: {tone: direto, rules: ["sempre em português"]}
llm: {default_model: "x/y", api_keys: {anthropic: "${MINHA_CHAVE}", openai: "${NAO_DEFINIDA}"}}
paths: {data_dir: "${BASE}/dados", log_file: "${BASE}/audit.jsonl"}
mcp_servers:
  - {name: google-workspace, command: servidor, args: ["--x", "${BASE}"]}
""",
    )
    config = load_config(path, environ={"MINHA_CHAVE": "segredo", "BASE": "/tmp/t"})
    assert config.behavior.tone == "direto" and config.behavior.rules == ["sempre em português"]
    assert config.llm.api_keys == {"anthropic": "segredo", "openai": ""}
    assert config.data_dir == Path("/tmp/t/dados") and config.audit_log == Path("/tmp/t/audit.jsonl")
    assert config.mcp_servers[0].args == ["--x", "/tmp/t"]


def test_load_default_file_in_cwd_or_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_config() == AppConfig()  # sem ./config.yaml: padrões
    write(tmp_path, "behavior: {tone: seco}")
    assert load_config().behavior.tone == "seco"
    write(tmp_path, "")  # arquivo vazio
    assert load_config() == AppConfig()


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        ("behavior: {tom: x}", "behavior.tom"),  # chave desconhecida (typo) é erro
        ("limits: {max_steps: 99}", "limits.max_steps"),
        ("limits: {timeout_seconds: 0}", "limits.timeout_seconds"),
        ("mcp_servers: [{name: 'nome ruim', command: x}]", "mcp_servers"),
        ("- lista\n- solta", "mapa YAML"),
        ("a: [unclosed", "Não foi possível ler"),
        ("surpresa: 1", "surpresa"),
    ],
)
def test_invalid_config_gives_clear_error(tmp_path, content, fragment):
    with pytest.raises(ConfigError, match="config.yaml") as exc:
        load_config(write(tmp_path, content))
    assert fragment in str(exc.value)


def test_explicit_missing_config_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="não encontrado"):
        load_config(tmp_path / "nada.yaml")


def test_expand_env_handles_nesting_and_non_strings():
    env = {"A": "1"}
    assert expand_env({"k": ["${A}x", 3, {"z": "${B}"}], "n": None}, env) == {"k": ["1x", 3, {"z": ""}], "n": None}


def test_apply_api_keys_never_overrides_existing_environment():
    config = AppConfig.model_validate({"llm": {"api_keys": {"anthropic": "K1", "gemini": "K2", "openai": ""}}})
    env = {"GEMINI_API_KEY": "do-ambiente"}
    assert apply_api_keys(config, env) == ["ANTHROPIC_API_KEY"]
    assert env == {"GEMINI_API_KEY": "do-ambiente", "ANTHROPIC_API_KEY": "K1"}


def test_apply_api_keys_uses_process_environment_by_default(monkeypatch):
    monkeypatch.delenv("FOO_API_KEY", raising=False)
    config = AppConfig.model_validate({"llm": {"api_keys": {"foo": "valor"}}})
    assert apply_api_keys(config) == ["FOO_API_KEY"]
    import os

    assert os.environ["FOO_API_KEY"] == "valor"
    monkeypatch.delenv("FOO_API_KEY")


# ===================================================================== comportamento
@pytest.fixture
def behavior(tmp_path):
    with MemoryEngine(tmp_path / "m.sqlite") as memory:
        yield BehaviorManager(BehaviorConfig(rules=["regra padrão"]), memory)


def test_behavior_defaults_then_overrides_then_reset(behavior):
    assert behavior.get().tone == "técnico" and behavior.get().rules == ["regra padrão"]
    behavior.set_field("tone", "  direto ")
    behavior.set_field("language", "en")
    current = behavior.get()
    assert (current.tone, current.language, current.verbosity) == ("direto", "en", "conciso")
    assert behavior.defaults.tone == "técnico"  # os padrões do config não são alterados
    behavior.reset()
    assert behavior.get() == behavior.defaults


def test_behavior_rules_add_remove_and_validation(behavior):
    assert behavior.add_rule(" nunca usar emoji ") == ["regra padrão", "nunca usar emoji"]
    assert behavior.get().rules == ["regra padrão", "nunca usar emoji"]
    assert behavior.remove_rule(1) == "regra padrão"
    assert behavior.get().rules == ["nunca usar emoji"]
    for bad in (0, 2, -1):
        with pytest.raises(ValueError, match="não existe"):
            behavior.remove_rule(bad)
    with pytest.raises(ValueError, match="vazia"):
        behavior.add_rule("  ")
    with pytest.raises(ValueError, match="desconhecido"):
        behavior.set_field("cor", "azul")
    with pytest.raises(ValueError, match="vazio"):
        behavior.set_field("tone", " ")


def test_behavior_overrides_persist_across_reopen(tmp_path):
    path = tmp_path / "m.sqlite"
    with MemoryEngine(path) as memory:
        manager = BehaviorManager(BehaviorConfig(), memory)
        manager.set_field("tone", "irônico")
        manager.add_rule("seja breve")
    with MemoryEngine(path) as memory:
        again = BehaviorManager(BehaviorConfig(), memory).get()
    assert again.tone == "irônico" and again.rules == ["seja breve"]


def test_system_prompt_reflects_behavior():
    prompt = BehaviorConfig(tone="direto", rules=["sempre citar fontes", "sem emoji"]).system_prompt()
    assert "Tom: direto" in prompt and "Idioma: pt-BR" in prompt
    assert "- sempre citar fontes" in prompt and "- sem emoji" in prompt
    assert "Regras do usuário" not in BehaviorConfig().system_prompt()


# ========================================================================== auditoria
def test_audit_log_appends_jsonl_and_reads_back(tmp_path):
    log = JsonlAuditLog(tmp_path / "logs" / "audit.jsonl")
    assert log.read() == []
    log({"event": "evaluated", "action": "rm -rf /tmp/x", "nota": "ação"})
    log({"event": "approved", "n": 2})
    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["nota"] == "ação"  # UTF-8 sem escapes
    assert "ação" in lines[0]
    assert [e["event"] for e in log.read()] == ["evaluated", "approved"]


def test_audit_log_survives_concurrent_writers_and_odd_values(tmp_path):
    log = JsonlAuditLog(tmp_path / "a.jsonl")

    def writer(start):
        for i in range(25):
            log({"event": "x", "n": start + i, "path": tmp_path})  # Path não é JSON nativo

    threads = [threading.Thread(target=writer, args=(k * 100,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    events = log.read()
    assert len(events) == 100 and len({e["n"] for e in events}) == 100
    log.path.write_text(log.path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    assert len(log.read()) == 100  # linhas em branco são ignoradas
