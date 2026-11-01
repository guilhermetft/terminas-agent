"""Montagem dos serviços: plugins, chaves de API, aprovador e encerramento."""

import os

from src.memory.config import AppConfig, LLMConfig
from src.rag import HashingEmbedder
from src.ui.services import start_services, stop_services
from tests.helpers import FakeLLM, make_config

PLUGIN = '''
from pydantic import BaseModel
from src.skills import Skill, SkillResult

class Hi(Skill):
    name = "hi-plugin"
    description = "Oi"
    category = "teste"
    class Params(BaseModel):
        pass
    async def run(self, params):
        return SkillResult.success("oi")
'''


async def start(config, root, **kwargs):
    return await start_services(config, llm=FakeLLM(), embedder=HashingEmbedder(), workdir=root, **kwargs)


async def test_services_wire_everything_together(tmp_path):
    services = await start(make_config(tmp_path), tmp_path)
    try:
        assert len(services.skills) == 9 and services.skill_ctx.skills is services.skills
        assert services.skill_ctx.guardrails is services.guardrails
        assert services.engine.subagents is services.subagents and services.engine.telemetry is services.telemetry
        assert services.skill_ctx.memory is services.memory and services.skill_ctx.rag is services.rag
        assert services.mcp is None and services.mcp_errors == {} and services.plugins is None
        assert (tmp_path / ".terminas" / "memory.sqlite").is_file()
    finally:
        await stop_services(services)


async def test_approver_is_shared_with_the_skills(tmp_path):
    services = await start(make_config(tmp_path), tmp_path)
    try:

        async def approver(decision):
            raise AssertionError("não usado")

        services.set_approver(approver)
        assert services.skill_ctx.approver is approver
        services.set_approver(None)
        assert services.skill_ctx.approver is None
    finally:
        await stop_services(services)


async def test_plugins_are_discovered_and_failures_reported(tmp_path):
    folder = tmp_path / "plugins"
    folder.mkdir()
    (folder / "hi.py").write_text(PLUGIN, encoding="utf-8")
    (folder / "bad.py").write_text("raise RuntimeError('quebrou')\n", encoding="utf-8")
    services = await start(make_config(tmp_path, plugins_dir="plugins"), tmp_path)
    try:
        assert "hi-plugin" in services.skills and len(services.skills) == 10
        assert services.plugins.loaded == ["hi-plugin"] and "quebrou" in services.plugins.errors["bad.py"]
    finally:
        await stop_services(services)


async def test_vault_dir_and_api_keys_come_from_config(tmp_path, monkeypatch):
    monkeypatch.delenv("ZZTEST_API_KEY", raising=False)
    config = AppConfig(
        paths=make_config(tmp_path).paths,
        llm=LLMConfig(api_keys={"zztest": "chave-xyz"}),
        skills={"vault_dir": str(tmp_path / "vault"), "plugins_dir": None},
    )
    services = await start(config, tmp_path)
    try:
        assert services.skill_ctx.vault_dir == tmp_path / "vault"
        assert os.environ["ZZTEST_API_KEY"] == "chave-xyz"
    finally:
        await stop_services(services)
        monkeypatch.delenv("ZZTEST_API_KEY", raising=False)


async def test_injected_runner_reaches_the_skills(tmp_path):
    marker = object()
    services = await start(make_config(tmp_path), tmp_path, runner=marker)
    try:
        assert services.skill_ctx.runner is marker
    finally:
        await stop_services(services)


async def test_mcp_listing_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    from src.skills import McpError, McpManager, McpServerConfig

    async def fake_connect_all(self, configs):
        self._sessions["s"] = object()  # parece conectado
        return {"s": None}

    async def broken_list(self, server=None):
        raise McpError("lista quebrada")

    monkeypatch.setattr(McpManager, "connect_all", fake_connect_all)
    monkeypatch.setattr(McpManager, "list_tools", broken_list)
    config = make_config(tmp_path, mcp_servers=[McpServerConfig(name="s", command="x")])
    services = await start(config, tmp_path)
    try:
        assert services.mcp_errors == {"(listagem de ferramentas)": "lista quebrada"}
    finally:
        await stop_services(services)
