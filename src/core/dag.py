"""Modelos de DAG de tarefas, validação topológica e execução concorrente dos nós."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CycleError(ValueError):
    """O grafo contém um ciclo. `cycle` lista os ids do ciclo (primeiro id repetido no fim)."""

    def __init__(self, cycle: list[str]) -> None:
        self.cycle = cycle
        super().__init__("Ciclo de dependências detectado: " + " -> ".join(cycle))


class TaskNode(BaseModel):
    """Tarefa atômica do plano."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    agent: str | None = None  # subagente ou skill sugerido; None = o próprio Master

    @model_validator(mode="after")
    def _check_dependencies(self) -> Self:
        if self.id in self.depends_on:
            raise ValueError(f"Nó '{self.id}' não pode depender de si mesmo.")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError(f"Nó '{self.id}' tem dependências duplicadas.")
        return self


def find_cycle(nodes: list[TaskNode]) -> list[str] | None:
    """Retorna um ciclo (ex.: ['a', 'b', 'a']) ou None se o grafo for acíclico.

    Ignora dependências para ids inexistentes (validadas separadamente)."""
    known = {n.id for n in nodes}
    deps = {n.id: [d for d in n.depends_on if d in known] for n in nodes}
    white, gray, black = 0, 1, 2
    color = {nid: white for nid in deps}

    def visit(start: str) -> list[str] | None:
        stack = [(start, iter(deps[start]))]
        path = [start]
        color[start] = gray
        while stack:
            node, it = stack[-1]
            for nxt in it:
                if color[nxt] == gray:
                    return path[path.index(nxt) :] + [nxt]
                if color[nxt] == white:
                    color[nxt] = gray
                    path.append(nxt)
                    stack.append((nxt, iter(deps[nxt])))
                    break
            else:
                color[node] = black
                stack.pop()
                path.pop()
        return None

    for nid in deps:
        if color[nid] == white and (cycle := visit(nid)):
            return cycle
    return None


class TaskDAG(BaseModel):
    """Grafo acíclico dirigido de tarefas. A validação ocorre na construção."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1)
    nodes: list[TaskNode] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_graph(self) -> Self:
        ids = [n.id for n in self.nodes]
        if len(set(ids)) != len(ids):
            dup = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"Ids de nós duplicados: {dup}")
        known = set(ids)
        for node in self.nodes:
            missing = [d for d in node.depends_on if d not in known]
            if missing:
                raise ValueError(f"Nó '{node.id}' depende de nós inexistentes: {missing}")
        if cycle := find_cycle(self.nodes):
            raise CycleError(cycle)
        return self

    def get(self, node_id: str) -> TaskNode:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)

    def dependents(self, node_id: str) -> list[str]:
        return [n.id for n in self.nodes if node_id in n.depends_on]

    def ready(self, completed: set[str]) -> list[TaskNode]:
        """Nós ainda não concluídos cujas dependências já foram todas concluídas."""
        return [
            n
            for n in self.nodes
            if n.id not in completed and all(d in completed for d in n.depends_on)
        ]

    def topological_order(self) -> list[str]:
        """Ordem topológica estável (Kahn), respeitando a ordem original dos nós."""
        indegree = {n.id: len(n.depends_on) for n in self.nodes}
        queue = deque(n.id for n in self.nodes if indegree[n.id] == 0)
        order: list[str] = []
        while queue:
            current = queue.popleft()
            order.append(current)
            for child in self.dependents(current):
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        return order

    def levels(self) -> list[list[str]]:
        """Lotes de execução: nós do mesmo lote são independentes e podem rodar em paralelo."""
        completed: set[str] = set()
        batches: list[list[str]] = []
        while len(completed) < len(self.nodes):
            batch = [n.id for n in self.ready(completed)]
            batches.append(batch)
            completed.update(batch)
        return batches


NodeRunner = Callable[[TaskNode, dict[str, Any]], Awaitable[Any]]


async def run_dag(
    dag: TaskDAG, runner: NodeRunner, *, max_concurrency: int | None = None
) -> dict[str, Any]:
    """Executa o DAG: cada nó inicia assim que suas dependências terminam; independentes
    rodam concorrentes no mesmo ciclo do event loop (RF-03).

    `runner(node, dep_results)` recebe os resultados das dependências por id. Se um nó
    falhar, os demais são cancelados e a primeira exceção é propagada.
    """
    semaphore = asyncio.Semaphore(max_concurrency) if max_concurrency else None
    tasks: dict[str, asyncio.Task[Any]] = {}

    async def run_node(node: TaskNode) -> Any:
        dep_results = {dep: await tasks[dep] for dep in node.depends_on}
        if semaphore is None:
            return await runner(node, dep_results)
        async with semaphore:
            return await runner(node, dep_results)

    try:
        async with asyncio.TaskGroup() as group:
            for node in dag.nodes:
                tasks[node.id] = group.create_task(run_node(node))
    except ExceptionGroup as group_error:
        raise group_error.exceptions[0] from None
    return {node_id: task.result() for node_id, task in tasks.items()}
