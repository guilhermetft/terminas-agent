"""swarm-inspector: saúde, latência, tokens e controle dos subagentes externos."""

from __future__ import annotations

import asyncio
import time
from typing import ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from src.core.guardrails import ActionRequest
from src.skills.base import Skill, SkillError, SkillResult

HEALTH_TIMEOUT = 2.0


class SwarmInspectorParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["status", "health", "kill", "restart"]
    agent: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.op in {"kill", "restart"} and not self.agent:
            raise ValueError(f"'{self.op}' exige 'agent'.")
        return self


class SwarmInspector(Skill):
    name: ClassVar[str] = "swarm-inspector"
    description: ClassVar[str] = (
        "Inspeciona a rede de subagentes: status e saúde, latência, tokens; "
        "kill cancela tarefas ativas e restart reinicia (se o agente suportar)."
    )
    category: ClassVar[str] = "Orquestração de Agentes"
    Params: ClassVar[type[BaseModel]] = SwarmInspectorParams

    def action(self, params: SwarmInspectorParams) -> ActionRequest:
        return ActionRequest.tool_call(
            f"{self.name}.{params.op}", agent="master", target=params.agent
        )

    def _agent(self, name: str):  # noqa: ANN202
        registry = self.ctx.require("subagents", "Registry de subagentes")
        try:
            return registry.get(name)
        except LookupError:
            raise SkillError(f"Subagente '{name}' não registrado.") from None

    @staticmethod
    async def _probe(agent) -> tuple[bool, float]:  # noqa: ANN001
        started = time.perf_counter()
        try:
            async with asyncio.timeout(HEALTH_TIMEOUT):
                healthy = bool(await agent.health())
        except Exception:  # agente quebrado conta como indisponível
            healthy = False
        return healthy, (time.perf_counter() - started) * 1000

    async def run(self, params: SwarmInspectorParams) -> SkillResult:
        registry = self.ctx.require("subagents", "Registry de subagentes")
        telemetry = self.ctx.telemetry

        if params.op in {"status", "health"}:
            names = [params.agent] if params.agent else [i.name for i in registry.list_info()]
            rows = []
            for name in names:
                agent = self._agent(name)
                healthy, probe_ms = await self._probe(agent)
                row = {
                    "agent": name,
                    "healthy": healthy,
                    "health_check_ms": round(probe_ms, 1),
                    "capabilities": agent.info.capabilities,
                }
                if telemetry is not None and params.op == "status":
                    stats = telemetry.stats(name)
                    row.update(
                        calls=stats.calls,
                        failures=stats.failures,
                        avg_latency_ms=round(stats.avg_latency * 1000, 1),
                        prompt_tokens=stats.prompt_tokens,
                        completion_tokens=stats.completion_tokens,
                        active_tasks=telemetry.active_tasks(name),
                    )
                rows.append(row)
            lines = [
                f"{'OK ' if r['healthy'] else 'OFF'} {r['agent']} ({r['health_check_ms']} ms)"
                + (f" chamadas={r['calls']} tokens={r['prompt_tokens'] + r['completion_tokens']}" if "calls" in r else "")
                for r in rows
            ]
            return SkillResult.success("\n".join(lines) or "Nenhum subagente registrado.", agents=rows)

        agent = self._agent(str(params.agent))
        if params.op == "kill":
            telemetry = self.ctx.require("telemetry", "Telemetria de subagentes")
            task_ids = telemetry.active_tasks(agent.name)
            for task_id in task_ids:
                await agent.cancel(task_id)
            return SkillResult.success(
                f"{len(task_ids)} tarefa(s) cancelada(s) em '{agent.name}'.", cancelled=task_ids
            )

        restart = getattr(agent, "restart", None)
        if not callable(restart):
            raise SkillError(f"Subagente '{agent.name}' não suporta restart.")
        await restart()
        return SkillResult.success(f"Subagente '{agent.name}' reiniciado.")
