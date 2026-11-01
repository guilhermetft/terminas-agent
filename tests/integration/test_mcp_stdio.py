"""Conector MCP de ponta a ponta: servidor real via stdio (SDK oficial `mcp`)."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from src.core import Approval  # noqa: E402
from src.skills import (  # noqa: E402
    McpError,
    McpManager,
    McpServerConfig,
    SkillContext,
    SkillsRegistry,
    build_default_registry,
)

pytestmark = pytest.mark.integration

SERVER = str(Path(__file__).resolve().parents[1] / "fixtures" / "mcp_fake_server.py")


def config(name="fake", *args, timeout=20.0, command=sys.executable):
    return McpServerConfig(name=name, command=command, args=[SERVER, *args], timeout_seconds=timeout)


async def approve(decision):
    return Approval("once")


async def test_connect_list_tools_with_typed_schemas_and_call():
    async with McpManager() as manager:
        await manager.connect(config())
        assert manager.servers == ["fake"]
        tools = {t.name: t for t in await manager.list_tools()}
        assert {"add", "explode", "list_events", "create_event", "search_threads"} <= set(tools)
        add = tools["add"]
        assert add.qualified_name == "mcp.fake.add" and add.description == "Soma dois inteiros."
        assert add.input_schema["required"] == ["a", "b"]
        assert add.input_schema["properties"]["a"]["type"] == "integer"
        assert [t.name for t in await manager.list_tools("fake")] == [t.name for t in tools.values()]

        result = await manager.call_tool("fake", "add", {"a": 2, "b": 3})
        assert result.text == "5" and not result.is_error and result.structured == {"result": 5}

        failed = await manager.call_tool("fake", "explode")
        assert failed.is_error and "explode" in failed.text
        bad_args = await manager.call_tool("fake", "add", {"a": "x"})
        assert bad_args.is_error


async def test_mcp_tools_become_skills_with_schema_validation_and_guardrail():
    async with McpManager() as manager:
        await manager.connect(config())
        registry = SkillsRegistry(SkillContext())
        names = await registry.register_mcp_tools(manager)
        assert "mcp.fake.add" in names and "mcp.fake.add" in registry
        spec = registry.get("mcp.fake.add").spec
        assert spec.source == "mcp" and spec.input_schema["required"] == ["a", "b"]
        assert registry.names(source="mcp") == sorted(names, key=names.index)

        # sem aprovador: ferramenta externa (risco Médio) fica aguardando
        waiting = await registry.invoke("mcp.fake.add", {"a": 1, "b": 2})
        assert waiting.status == "awaiting_approval"

        registry.ctx.approver = approve
        ok = await registry.invoke("mcp.fake.add", {"a": 1, "b": 2})
        assert ok.ok and ok.output == "3" and ok.data["structured"] == {"result": 3}

        invalid = await registry.invoke("mcp.fake.add", {"a": "não-número", "b": 2})
        assert not invalid.ok and "Argumentos inválidos" in invalid.error and "a" in invalid.error
        missing = await registry.invoke("mcp.fake.add", {"a": 1})
        assert not missing.ok and "'b' is a required property" in missing.error
        not_object = await registry.invoke("mcp.fake.add", [1, 2])
        assert not not_object.ok and "objeto" in not_object.error

        server_error = await registry.invoke("mcp.fake.explode", None)
        assert not server_error.ok and "explode" in server_error.error


async def test_google_workspace_skill_end_to_end_through_mcp():
    async with McpManager() as manager:
        await manager.connect(config("google-workspace"))
        registry = build_default_registry(SkillContext(mcp=manager, approver=approve))

        events = await registry.invoke(
            "google-workspace",
            {"service": "calendar", "action": "list_events", "arguments": {"time_min": "2026-11-01T00:00:00"}},
        )
        assert events.ok and "Reunião de alinhamento (2026-11-01)" in events.output
        mail = await registry.invoke(
            "google-workspace", {"service": "gmail", "action": "search", "arguments": {"query": "fatura"}}
        )
        assert mail.output == "2 conversas para 'fatura'"
        created = await registry.invoke(
            "google-workspace",
            {"service": "calendar", "action": "create_event", "arguments": {"title": "Review"}},
        )
        assert created.output == "evento criado: Review"


async def test_connect_all_tolerates_a_broken_server():
    async with McpManager() as manager:
        outcome = await manager.connect_all(
            [config("bom"), config("quebrado", command="programa-inexistente-xyz")]
        )
        assert outcome["bom"] is None
        assert "Falha ao conectar" in outcome["quebrado"]
        assert manager.servers == ["bom"]
        assert (await manager.call_tool("bom", "add", {"a": 1, "b": 1})).text == "2"


async def test_connect_times_out_on_unresponsive_server():
    async with McpManager() as manager:
        with pytest.raises(McpError, match="não respondeu em 1s"):
            await manager.connect(config("lento", "hang", timeout=1.0))
        assert manager.servers == []


async def test_calls_after_close_fail_clearly():
    manager = McpManager()
    await manager.connect(config())
    await manager.close()
    with pytest.raises(McpError, match="não conectado"):
        await manager.call_tool("fake", "add", {"a": 1, "b": 1})
