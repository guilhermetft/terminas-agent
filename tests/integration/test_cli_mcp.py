"""Terminal + MCP real: ferramentas de um servidor viram skills e passam pelo modal HITL."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from src.rag import HashingEmbedder  # noqa: E402
from src.skills import McpServerConfig  # noqa: E402
from src.ui.cli import TerminasApp  # noqa: E402
from src.ui.services import start_services, stop_services  # noqa: E402
from tests.helpers import ForbiddenLLM, ScriptedReader, make_auth, make_config, make_console, output  # noqa: E402

pytestmark = pytest.mark.integration

SERVER = str(Path(__file__).resolve().parents[1] / "fixtures" / "mcp_fake_server.py")


async def test_mcp_tools_work_inside_the_terminal(tmp_path):
    config = make_config(
        tmp_path, mcp_servers=[McpServerConfig(name="fake", command=sys.executable, args=[SERVER])]
    )
    services = await start_services(
        config, llm=ForbiddenLLM(), embedder=HashingEmbedder(), workdir=tmp_path, auth=make_auth(tmp_path)
    )
    try:
        assert services.mcp_errors == {} and services.mcp.servers == ["fake"]
        reader = ScriptedReader("y")
        app = TerminasApp(services, make_console(), reader)
        await app.handle_line("/skills")
        await app.handle_line("/agents")
        await app.handle_line('/skill mcp.fake.add {"a": 20, "b": 22}')
        await app.handle_line('/skill mcp.fake.add {"a": "x"}')
        text = output(app.console)
        assert "mcp.fake.add" in text and "Servidores MCP conectados: fake" in text
        assert "MCP fake: add, explode" in text
        assert "ALERTA DE SEGURANÇA" in text  # ferramenta externa pede aprovação
        assert "\n42" in text  # resultado da chamada real
        assert "Argumentos inválidos" in text  # schema do servidor validado antes de chamar
        assert len(reader.prompts) == 1  # a chamada inválida nem chegou ao modal
    finally:
        await stop_services(services)
