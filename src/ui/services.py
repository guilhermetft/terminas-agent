"""Montagem dos serviços do Termina a partir do config.yaml (injeção de dependências)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from src.core.audit import JsonlAuditLog
from src.core.engine import MasterEngine
from src.core.guardrails import Approver, GuardrailEngine
from src.core.planner import LLMClient, PlannerEngine, litellm_client
from src.memory.config import AppConfig, BehaviorManager, apply_api_keys
from src.memory.engine import MemoryEngine
from src.rag.embeddings import Embedder, default_embedder
from src.rag.engine import RAGEngine
from src.skills.base import SKILL_TOOL_RISKS, CommandRunner, SkillContext
from src.skills.mcp_client import McpError, McpManager
from src.skills.registry import PluginReport, SkillsRegistry
from src.subagents_bridge.registry import SubagentRegistry
from src.subagents_bridge.telemetry import SwarmTelemetry


@dataclass
class Services:
    config: AppConfig
    memory: MemoryEngine
    behavior: BehaviorManager
    rag: RAGEngine
    audit: JsonlAuditLog
    guardrails: GuardrailEngine
    skill_ctx: SkillContext
    skills: SkillsRegistry
    subagents: SubagentRegistry
    telemetry: SwarmTelemetry
    engine: MasterEngine
    mcp: McpManager | None = None
    mcp_errors: dict[str, str] = field(default_factory=dict)
    plugins: PluginReport | None = None

    def set_approver(self, approver: Approver | None) -> None:
        """Define quem decide as aprovações HITL (a UI), inclusive nas skills."""
        self.skill_ctx.approver = approver


async def start_services(
    config: AppConfig,
    *,
    llm: LLMClient | None = None,
    embedder: Embedder | None = None,
    runner: CommandRunner | None = None,
    workdir: str | Path | None = None,
    approver: Approver | None = None,
) -> Services:
    """Cria memória, RAG, guardrails, skills (nativas, plugins e MCP) e o engine.

    Deve rodar no mesmo task que `stop_services` (o SDK MCP exige)."""
    apply_api_keys(config)
    root = Path(workdir) if workdir is not None else Path.cwd()

    memory = MemoryEngine(config.memory_db)
    rag = RAGEngine(config.rag_db, embedder or default_embedder(config.models_dir))
    audit = JsonlAuditLog(config.audit_log)
    guardrails = GuardrailEngine(tool_risks=SKILL_TOOL_RISKS, audit=audit)
    subagents, telemetry = SubagentRegistry(), SwarmTelemetry()

    ctx = SkillContext(
        guardrails=guardrails,
        approver=approver,
        workdir=root,
        memory=memory,
        rag=rag,
        subagents=subagents,
        telemetry=telemetry,
        vault_dir=Path(config.skills.vault_dir).expanduser() if config.skills.vault_dir else None,
    )
    if runner is not None:
        ctx.runner = runner
    registry = SkillsRegistry(ctx)
    registry.register_native()

    services = Services(
        config=config,
        memory=memory,
        behavior=BehaviorManager(config.behavior, memory),
        rag=rag,
        audit=audit,
        guardrails=guardrails,
        skill_ctx=ctx,
        skills=registry,
        subagents=subagents,
        telemetry=telemetry,
        engine=MasterEngine(
            PlannerEngine(llm or litellm_client(config.llm.default_model)),
            subagents,
            telemetry,
            max_concurrency=config.limits.max_concurrency,
            project_path=root,
        ),
    )

    if config.skills.plugins_dir:
        services.plugins = registry.discover_plugins(root / config.skills.plugins_dir)
    if config.mcp_servers:
        manager = McpManager()
        services.mcp = manager
        results = await manager.connect_all(config.mcp_servers)
        services.mcp_errors = {name: err for name, err in results.items() if err}
        if manager.servers:
            try:
                await registry.register_mcp_tools(manager)
            except McpError as exc:
                services.mcp_errors["(listagem de ferramentas)"] = str(exc)
    return services


async def stop_services(services: Services) -> None:
    if services.mcp is not None:
        await services.mcp.close()
    services.rag.close()
    services.memory.close()
