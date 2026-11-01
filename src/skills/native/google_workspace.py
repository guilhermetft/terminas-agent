"""google-workspace: Agenda, Gmail e Drive via servidor MCP configurado."""

from __future__ import annotations

from typing import Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.guardrails import ActionRequest
from src.skills.base import Skill, SkillError, SkillResult

DEFAULT_SERVER = "google-workspace"
# (serviço, ação) -> nome da ferramenta no servidor MCP. Sobrescreva em
# `ctx.options["google_tools"]` se o seu servidor usar nomes diferentes.
DEFAULT_TOOLS: dict[tuple[str, str], str] = {
    ("calendar", "list_events"): "list_events",
    ("calendar", "create_event"): "create_event",
    ("gmail", "search"): "search_threads",
    ("gmail", "read"): "get_thread",
    ("drive", "search"): "search_files",
    ("drive", "read"): "read_file_content",
}
WRITE_ACTIONS = {("calendar", "create_event")}


class GoogleWorkspaceParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: Literal["calendar", "gmail", "drive"]
    action: Literal["list_events", "create_event", "search", "read"]
    arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _supported(self) -> Self:
        if (self.service, self.action) not in DEFAULT_TOOLS:
            raise ValueError(f"Ação '{self.action}' não existe para '{self.service}'.")
        return self


class GoogleWorkspace(Skill):
    name: ClassVar[str] = "google-workspace"
    description: ClassVar[str] = (
        "Google Agenda (listar/criar eventos), Gmail (buscar/ler) e Drive (buscar/ler) via MCP."
    )
    category: ClassVar[str] = "Integração Google / MCP"
    Params: ClassVar[type[BaseModel]] = GoogleWorkspaceParams

    def action(self, params: GoogleWorkspaceParams) -> ActionRequest:
        write = (params.service, params.action) in WRITE_ACTIONS
        op = "create_event" if write else "read"
        return ActionRequest.tool_call(
            f"{self.name}.{op}", agent="master", service=params.service, action=params.action
        )

    async def run(self, params: GoogleWorkspaceParams) -> SkillResult:
        manager = self.ctx.require("mcp", "Gerenciador MCP")
        server = self.ctx.options.get("google_server", DEFAULT_SERVER)
        tools = {**DEFAULT_TOOLS, **self.ctx.options.get("google_tools", {})}
        if server not in manager.servers:
            raise SkillError(f"Servidor MCP '{server}' não está conectado.")
        tool = tools[(params.service, params.action)]
        result = await manager.call_tool(server, tool, params.arguments)
        if result.is_error:
            raise SkillError(f"{params.service}.{params.action}: {result.text or 'erro no servidor MCP'}")
        return SkillResult.success(result.text, structured=result.structured, tool=tool)
