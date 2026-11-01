"""MasterEngine: planeja metas e executa o DAG despachando nós aos subagentes externos."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from src.core.dag import TaskDAG, TaskNode, run_dag
from src.core.planner import PlannerEngine
from src.subagents_bridge.registry import SubagentRegistry
from src.subagents_bridge.schemas import AgentTask, TaskContext, TaskStatus
from src.subagents_bridge.telemetry import SwarmTelemetry, run_tracked


class NodeState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class NodeOutcome:
    state: NodeState
    summary: str = ""
    error: str | None = None
    duration: float = 0.0
    agent: str | None = None


@dataclass(frozen=True)
class RunEvent:
    node_id: str
    state: NodeState
    outcome: NodeOutcome | None = None


EventHandler = Callable[[RunEvent], None]


@dataclass
class RunReport:
    dag: TaskDAG
    outcomes: dict[str, NodeOutcome] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return all(o.state is NodeState.DONE for o in self.outcomes.values())

    def counts(self) -> dict[str, int]:
        counts = {state.value: 0 for state in NodeState}
        for outcome in self.outcomes.values():
            counts[outcome.state.value] += 1
        return counts


class MasterEngine:
    def __init__(
        self,
        planner: PlannerEngine,
        subagents: SubagentRegistry,
        telemetry: SwarmTelemetry,
        *,
        max_concurrency: int = 5,
        project_path: str | Path | None = None,
    ) -> None:
        self.planner = planner
        self.subagents = subagents
        self.telemetry = telemetry
        self.max_concurrency = max_concurrency
        self.project_path = None if project_path is None else str(project_path)

    # ------------------------------------------------------------------ planejar
    async def plan(self, goal: str, *, context: str | None = None) -> TaskDAG:
        agents = [info.name for info in self.subagents.list_info()]
        return await self.planner.create_dag(goal, context=context, agents=agents)

    # ------------------------------------------------------------------- executar
    async def run(self, dag: TaskDAG, on_event: EventHandler | None = None) -> RunReport:
        """Executa o DAG: independentes em paralelo; falha/skip propaga como `skipped`."""
        report = RunReport(dag)

        def emit(node_id: str, state: NodeState, outcome: NodeOutcome | None = None) -> None:
            if on_event is not None:
                on_event(RunEvent(node_id, state, outcome))

        for node in dag.nodes:
            emit(node.id, NodeState.PENDING)

        async def runner(node: TaskNode, deps: dict[str, NodeOutcome]) -> NodeOutcome:
            blocked = [d for d, o in deps.items() if o.state is not NodeState.DONE]
            if blocked:
                outcome = NodeOutcome(
                    NodeState.SKIPPED,
                    error=f"dependência não concluída: {', '.join(blocked)}",
                    agent=node.agent,
                )
            else:
                emit(node.id, NodeState.RUNNING)
                outcome = await self._execute(node, dag)
            report.outcomes[node.id] = outcome
            emit(node.id, outcome.state, outcome)
            return outcome

        await run_dag(dag, runner, max_concurrency=self.max_concurrency)
        return report

    async def _execute(self, node: TaskNode, dag: TaskDAG) -> NodeOutcome:
        if node.agent is None or node.agent not in self.subagents:
            who = node.agent or "master"
            return NodeOutcome(
                NodeState.SKIPPED, error=f"nenhum executor disponível para '{who}'", agent=node.agent
            )
        agent = self.subagents.get(node.agent)
        task = AgentTask(
            assigned_agent=node.agent,
            goal=node.description,
            context=TaskContext(project_path=self.project_path),
        )
        started = time.perf_counter()
        try:
            result = await run_tracked(agent, task, self.telemetry)
        except Exception as exc:  # falha de um subagente não derruba o plano inteiro
            return NodeOutcome(
                NodeState.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                duration=time.perf_counter() - started,
                agent=node.agent,
            )
        duration = time.perf_counter() - started
        if result.status is TaskStatus.SUCCESS:
            return NodeOutcome(NodeState.DONE, summary=result.summary, duration=duration, agent=node.agent)
        return NodeOutcome(
            NodeState.FAILED,
            summary=result.summary,
            error=result.error or result.status.value,
            duration=duration,
            agent=node.agent,
        )

    # ------------------------------------------------------------------ síntese
    @staticmethod
    def synthesize(report: RunReport) -> str:
        """Resumo determinístico (sem LLM): uma linha por nó, na ordem topológica."""
        marks = {
            NodeState.DONE: "✔", NodeState.FAILED: "✖", NodeState.SKIPPED: "⊘",
            NodeState.RUNNING: "●", NodeState.PENDING: "○",
        }  # fmt: skip
        lines = []
        for node_id in report.dag.topological_order():
            outcome = report.outcomes.get(node_id)
            node = report.dag.get(node_id)
            if outcome is None:
                lines.append(f"{marks[NodeState.PENDING]} {node_id}: {node.description}")
                continue
            detail = outcome.summary if outcome.state is NodeState.DONE else outcome.error
            lines.append(f"{marks[outcome.state]} {node_id}: {node.description}" + (f" — {detail}" if detail else ""))
        counts = report.counts()
        lines.append(
            f"\n{counts['done']} concluída(s), {counts['failed']} com falha, {counts['skipped']} ignorada(s)."
        )
        return "\n".join(lines)

    # ----------------------------------------------------------------- cancelar
    async def cancel_all(self) -> int:
        """Pede a cada subagente que cancele suas tarefas em andamento. Retorna quantas."""
        cancelled = 0
        for info in self.subagents.list_info():
            agent = self.subagents.get(info.name)
            for task_id in self.telemetry.active_tasks(info.name):
                await agent.cancel(task_id)
                cancelled += 1
        return cancelled
