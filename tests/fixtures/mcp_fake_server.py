"""Servidor MCP falso (stdio) para testes. Simula as ferramentas de um servidor Google."""

import sys
import time

from mcp.server.mcpserver import MCPServer

server = MCPServer("fake")


@server.tool()
def add(a: int, b: int) -> int:
    """Soma dois inteiros."""
    return a + b


@server.tool()
def explode() -> str:
    """Sempre falha."""
    raise RuntimeError("explodiu de propósito")


@server.tool()
def list_events(time_min: str = "", time_max: str = "") -> str:
    """Lista eventos da agenda."""
    return f"09:00 Reunião de alinhamento ({time_min[:10]})\n14:00 Dentista"


@server.tool()
def create_event(title: str) -> str:
    """Cria um evento."""
    return f"evento criado: {title}"


@server.tool()
def search_threads(query: str) -> str:
    """Busca e-mails."""
    return f"2 conversas para '{query}'"


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "hang":
        time.sleep(60)  # nunca responde ao handshake
    server.run("stdio")
