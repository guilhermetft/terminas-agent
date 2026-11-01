"""Configuração do Termina (`config.yaml`) e comportamento customizável do agente."""

from __future__ import annotations

import os
import re
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.memory.engine import MemoryEngine
from src.skills.mcp_client import McpServerConfig

DEFAULT_CONFIG_FILE = "config.yaml"
BEHAVIOR_NAMESPACE = "behavior"
_ENV_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """config.yaml ilegível ou inválido."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BehaviorConfig(_Section):
    tone: str = "técnico"
    verbosity: str = "conciso"
    language: str = "pt-BR"
    rules: list[str] = Field(default_factory=list)

    def system_prompt(self) -> str:
        lines = [
            "Você é o Termina's Agent, um copiloto técnico que orquestra tarefas e subagentes.",
            f"Tom: {self.tone}. Nível de detalhe: {self.verbosity}. Idioma: {self.language}.",
        ]
        if self.rules:
            lines.append("Regras do usuário (sempre seguir):")
            lines.extend(f"- {rule}" for rule in self.rules)
        return "\n".join(lines)


class LLMConfig(_Section):
    default_model: str = "gemini/gemini-2.5-flash"
    triage_model: str = "gemini/gemini-2.5-flash"
    api_keys: dict[str, str] = Field(default_factory=dict)


class LimitsConfig(_Section):
    max_steps: int = Field(default=15, ge=1, le=15)
    timeout_seconds: float = Field(default=120, gt=0)
    max_concurrency: int = Field(default=5, ge=1)


class PathsConfig(_Section):
    data_dir: str = ".terminas"
    auth_dir: str = "~/.terminas"  # credenciais (/login): do usuário, valem em qualquer pasta
    log_file: str | None = None  # padrão: <data_dir>/logs/audit.jsonl


class SkillsConfig(_Section):
    vault_dir: str | None = None
    plugins_dir: str | None = "src/skills/plugins"


class AppConfig(_Section):
    behavior: BehaviorConfig = Field(default_factory=BehaviorConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    mcp_servers: list[McpServerConfig] = Field(default_factory=list)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)

    @property
    def data_dir(self) -> Path:
        return Path(self.paths.data_dir).expanduser()

    @property
    def auth_dir(self) -> Path:
        """Pasta das credenciais. A variável TERMINAS_HOME, se existir, tem prioridade."""
        return Path(os.environ.get("TERMINAS_HOME") or self.paths.auth_dir).expanduser()

    @property
    def memory_db(self) -> Path:
        return self.data_dir / "memory.sqlite"

    @property
    def rag_db(self) -> Path:
        return self.data_dir / "rag.sqlite"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def history_file(self) -> Path:
        return self.data_dir / "history"

    @property
    def audit_log(self) -> Path:
        if self.paths.log_file:
            return Path(self.paths.log_file).expanduser()
        return self.data_dir / "logs" / "audit.jsonl"


def expand_env(value: Any, environ: MutableMapping[str, str] | None = None) -> Any:
    """Troca `${VAR}` pelo valor da variável de ambiente ('' se não definida)."""
    env = os.environ if environ is None else environ
    if isinstance(value, str):
        return _ENV_VAR.sub(lambda m: env.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [expand_env(v, env) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v, env) for k, v in value.items()}
    return value


def load_config(
    path: str | Path | None = None, environ: MutableMapping[str, str] | None = None
) -> AppConfig:
    """Lê o config.yaml. Sem `path`, usa ./config.yaml se existir; senão, os padrões.

    Um `path` explícito que não existe é erro (não cai silenciosamente nos padrões)."""
    if path is None:
        candidate = Path(DEFAULT_CONFIG_FILE)
        if not candidate.is_file():
            return AppConfig()
        file = candidate
    else:
        file = Path(path).expanduser()
        if not file.is_file():
            raise ConfigError(f"Arquivo de configuração não encontrado: {file}")
    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Não foi possível ler {file}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{file}: o conteúdo precisa ser um mapa YAML.")
    try:
        return AppConfig.model_validate(expand_env(raw, environ))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(f"{file}: configuração inválida — {problems}") from None


def apply_api_keys(config: AppConfig, environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Exporta chaves do config como `<PROVEDOR>_API_KEY` (sem sobrescrever o ambiente)."""
    env = os.environ if environ is None else environ
    exported: list[str] = []
    for provider, key in config.llm.api_keys.items():
        name = f"{provider.upper()}_API_KEY"
        if key and name not in env:
            env[name] = key
            exported.append(name)
    return exported


class BehaviorManager:
    """Comportamento efetivo = padrões do config.yaml + ajustes feitos por `/behavior`.

    Os ajustes ficam na memória (SQLite); o config.yaml nunca é reescrito."""

    FIELDS = ("tone", "verbosity", "language")

    def __init__(self, defaults: BehaviorConfig, memory: MemoryEngine) -> None:
        self.defaults = defaults
        self.memory = memory

    def get(self) -> BehaviorConfig:
        overrides = self.memory.list_long_term(BEHAVIOR_NAMESPACE)
        return self.defaults.model_copy(
            update={k: v for k, v in overrides.items() if k in (*self.FIELDS, "rules")}
        )

    def set_field(self, name: str, value: str) -> None:
        if name not in self.FIELDS:
            raise ValueError(f"Campo desconhecido: '{name}' (use {', '.join(self.FIELDS)}).")
        if not value.strip():
            raise ValueError("O valor não pode ser vazio.")
        self.memory.set_long_term(name, value.strip(), BEHAVIOR_NAMESPACE)

    def add_rule(self, rule: str) -> list[str]:
        if not rule.strip():
            raise ValueError("A regra não pode ser vazia.")
        rules = [*self.get().rules, rule.strip()]
        self.memory.set_long_term("rules", rules, BEHAVIOR_NAMESPACE)
        return rules

    def remove_rule(self, number: int) -> str:
        rules = list(self.get().rules)
        if not 1 <= number <= len(rules):
            raise ValueError(f"Regra {number} não existe (há {len(rules)}).")
        removed = rules.pop(number - 1)
        self.memory.set_long_term("rules", rules, BEHAVIOR_NAMESPACE)
        return removed

    def reset(self) -> None:
        for key in (*self.FIELDS, "rules"):
            self.memory.delete_long_term(key, BEHAVIOR_NAMESPACE)
