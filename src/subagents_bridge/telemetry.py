"""Telemetria da rede de subagentes: chamadas, tokens, latência e tarefas em andamento."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from src.subagents_bridge.base import BaseSubagent
from src.subagents_bridge.schemas import AgentResult, AgentTask, TaskStatus


@dataclass
class AgentStats:
    calls: int = 0
    successes: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_latency: float = 0.0
    active: set[str] = field(default_factory=set)

    @property
    def avg_latency(self) -> float:
        finished = self.successes + self.failures
        return self.total_latency / finished if finished else 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class SwarmTelemetry:
    def __init__(self) -> None:
        self._stats: dict[str, AgentStats] = {}

    def stats(self, agent: str) -> AgentStats:
        return self._stats.setdefault(agent, AgentStats())

    def all_stats(self) -> dict[str, AgentStats]:
        return dict(self._stats)

    def active_tasks(self, agent: str) -> list[str]:
        return sorted(self.stats(agent).active)

    def record_start(self, agent: str, task_id: str) -> None:
        stats = self.stats(agent)
        stats.calls += 1
        stats.active.add(task_id)

    def record_end(
        self, agent: str, task_id: str, result: AgentResult | None, latency: float
    ) -> None:
        stats = self.stats(agent)
        stats.active.discard(task_id)
        stats.total_latency += latency
        if result is not None and result.status is TaskStatus.SUCCESS:
            stats.successes += 1
        else:
            stats.failures += 1
        if result is not None and result.token_usage is not None:
            stats.prompt_tokens += result.token_usage.prompt_tokens
            stats.completion_tokens += result.token_usage.completion_tokens


async def run_tracked(
    agent: BaseSubagent, task: AgentTask, telemetry: SwarmTelemetry
) -> AgentResult:
    """Executa a tarefa registrando início, fim, latência e tokens (mesmo se falhar)."""
    telemetry.record_start(agent.name, task.task_id)
    started = time.perf_counter()
    result: AgentResult | None = None
    try:
        result = await agent.execute(task)
        return result
    finally:
        telemetry.record_end(agent.name, task.task_id, result, time.perf_counter() - started)
