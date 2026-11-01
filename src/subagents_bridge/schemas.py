"""Contratos A2A (Agent-to-Agent): payloads tipados entre o Termina e subagentes externos.

Validação estrita: campos desconhecidos são rejeitados e payload inválido levanta
`pydantic.ValidationError` (CT-07), nunca é aceito silenciosamente.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

A2A_TASK_SCHEMA = "https://terminas.dev/schemas/a2a-task.json"
A2A_RESULT_SCHEMA = "https://terminas.dev/schemas/a2a-result.json"

MAX_STEPS_LIMIT = 15  # teto estrito de passos por subagente (docs/2, restrição 3)
DEFAULT_TIMEOUT_SECONDS = 120


def new_task_id() -> str:
    return f"tsk-{uuid.uuid4().hex[:8]}"


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)


class RagChunk(_Contract):
    """Trecho recuperado pelo RAG, com citação de arquivo e linhas."""

    file: str = Field(min_length=1)
    lines: str = Field(pattern=r"^\d+(-\d+)?$", description='Ex.: "10-25" ou "7".')
    content: str


class TaskContext(_Contract):
    project_path: str | None = None
    relevant_rag_chunks: list[RagChunk] = Field(default_factory=list)


class TaskConstraints(_Contract):
    max_steps: int = Field(default=MAX_STEPS_LIMIT, ge=1, le=MAX_STEPS_LIMIT)
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0)
    allowed_skills: list[str] = Field(default_factory=list)


class AgentTask(_Contract):
    """Payload de despacho (Master -> Subagente externo)."""

    schema_uri: Literal["https://terminas.dev/schemas/a2a-task.json"] = Field(
        default=A2A_TASK_SCHEMA, alias="$schema"
    )
    task_id: str = Field(default_factory=new_task_id, min_length=1)
    parent_task_id: str | None = None
    assigned_agent: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    context: TaskContext = Field(default_factory=TaskContext)
    constraints: TaskConstraints = Field(default_factory=TaskConstraints)

    def to_wire(self) -> dict[str, Any]:
        """Dict JSON-serializável com a chave `$schema`, pronto para o barramento."""
        return self.model_dump(mode="json", by_alias=True)


class TaskStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class Artifact(_Contract):
    type: str = Field(min_length=1)
    path: str = Field(min_length=1)


class TokenUsage(_Contract):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    model: str = Field(min_length=1)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class AgentResult(_Contract):
    """Payload de retorno (Subagente externo -> Master).

    `task_id` e `status` são obrigatórios. `success` não pode carregar `error`;
    qualquer outro status exige `error` preenchido.
    """

    schema_uri: Literal["https://terminas.dev/schemas/a2a-result.json"] = Field(
        default=A2A_RESULT_SCHEMA, alias="$schema"
    )
    task_id: str = Field(min_length=1)
    status: TaskStatus
    summary: str = ""
    artifacts: list[Artifact] = Field(default_factory=list)
    token_usage: TokenUsage | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _status_matches_error(self) -> Self:
        if self.status is TaskStatus.SUCCESS and self.error:
            raise ValueError("status 'success' não pode ter 'error' preenchido.")
        if self.status is not TaskStatus.SUCCESS and not self.error:
            raise ValueError(f"status '{self.status.value}' exige 'error' preenchido.")
        return self

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


# Nome usado em docs/4 (CT-07).
AgentTaskResult = AgentResult


class SubagentInfo(_Contract):
    """Metadados de um subagente externo registrado (nome, capacidades, modelos, schema)."""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    description: str = ""
    capabilities: list[str] = Field(min_length=1)
    recommended_models: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _capabilities_valid(self) -> Self:
        if any(not c.strip() for c in self.capabilities):
            raise ValueError("capabilities não pode conter itens vazios.")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities não pode conter itens duplicados.")
        return self
