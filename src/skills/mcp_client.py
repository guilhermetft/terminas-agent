"""Cliente MCP (Model Context Protocol): conecta servidores externos e expõe suas ferramentas.

Usa o SDK oficial `mcp` (>= 2). Importado só ao conectar: sem o extra `mcp` o resto do
Termina continua funcionando.

Uso: sempre dentro de um único task, via `async with McpManager() as manager:` (o SDK usa
cancel scopes do anyio, que precisam abrir e fechar no mesmo task).
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class McpError(RuntimeError):
    """Falha de conexão/chamada MCP."""


class McpServerConfig(BaseModel):
    """Servidor MCP local (stdio), como em `mcp_servers` do config.yaml."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None  # None = ambiente padrão seguro do SDK
    cwd: str | None = None
    timeout_seconds: float = Field(default=30.0, gt=0)


class McpTool(BaseModel):
    """Ferramenta exposta por um servidor, com o schema JSON tipado dos argumentos."""

    server: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})

    @property
    def qualified_name(self) -> str:
        return f"mcp.{self.server}.{self.name}"


class McpToolResult(BaseModel):
    text: str
    is_error: bool = False
    structured: Any = None


def load_mcp_configs(config: dict[str, Any]) -> list[McpServerConfig]:
    """Lê `mcp_servers` (lista de objetos) de um config.yaml já carregado."""
    return [McpServerConfig.model_validate(item) for item in config.get("mcp_servers") or []]


class McpManager:
    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._sessions: dict[str, Any] = {}
        self._configs: dict[str, McpServerConfig] = {}

    @property
    def servers(self) -> list[str]:
        return list(self._sessions)

    async def connect(self, config: McpServerConfig) -> None:
        if config.name in self._sessions:
            raise McpError(f"Servidor MCP '{config.name}' já conectado.")
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise McpError('SDK MCP ausente. Instale com: pip install -e ".[mcp]"') from exc

        params = StdioServerParameters(
            command=config.command, args=config.args, env=config.env, cwd=config.cwd
        )
        try:
            async with asyncio.timeout(config.timeout_seconds):
                read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
        except TimeoutError:
            raise McpError(
                f"Servidor MCP '{config.name}' não respondeu em {config.timeout_seconds:g}s."
            ) from None
        except Exception as exc:
            raise McpError(f"Falha ao conectar no servidor MCP '{config.name}': {exc}") from exc
        self._sessions[config.name] = session
        self._configs[config.name] = config

    async def connect_all(self, configs: list[McpServerConfig]) -> dict[str, str | None]:
        """Conecta todos; um servidor quebrado não impede os demais. Retorna erro por servidor."""
        outcome: dict[str, str | None] = {}
        for config in configs:
            try:
                await self.connect(config)
                outcome[config.name] = None
            except McpError as exc:
                outcome[config.name] = str(exc)
        return outcome

    def _session(self, server: str) -> Any:
        try:
            return self._sessions[server]
        except KeyError:
            raise McpError(f"Servidor MCP '{server}' não conectado.") from None

    async def list_tools(self, server: str | None = None) -> list[McpTool]:
        tools: list[McpTool] = []
        for name in [server] if server else list(self._sessions):
            session = self._session(name)
            timeout = self._configs[name].timeout_seconds
            try:
                async with asyncio.timeout(timeout):
                    listed = await session.list_tools()
            except TimeoutError:
                raise McpError(f"Servidor MCP '{name}' não listou ferramentas em {timeout:g}s.") from None
            tools.extend(
                McpTool(
                    server=name,
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=dict(tool.input_schema or {"type": "object"}),
                )
                for tool in listed.tools
            )
        return tools

    async def call_tool(
        self, server: str, tool: str, arguments: dict[str, Any] | None = None
    ) -> McpToolResult:
        session = self._session(server)
        timeout = self._configs[server].timeout_seconds
        try:
            async with asyncio.timeout(timeout):
                result = await session.call_tool(tool, arguments or {})
        except TimeoutError:
            raise McpError(f"Ferramenta '{server}.{tool}' excedeu {timeout:g}s.") from None
        except Exception as exc:
            raise McpError(f"Falha ao chamar '{server}.{tool}': {exc}") from exc
        text = "\n".join(
            part.text for part in result.content if getattr(part, "text", None) is not None
        )
        return McpToolResult(
            text=text, is_error=bool(result.is_error), structured=result.structured_content
        )

    async def close(self) -> None:
        self._sessions.clear()
        self._configs.clear()
        await self._stack.aclose()

    async def __aenter__(self) -> McpManager:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
