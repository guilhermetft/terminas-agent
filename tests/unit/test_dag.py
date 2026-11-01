"""Testes do modelo de DAG, validador de ciclos e execução concorrente (RF-01, RF-03)."""

import asyncio

import pytest
from pydantic import ValidationError

from src.core import CycleError, TaskDAG, TaskNode, find_cycle, run_dag


def node(node_id, *deps, agent=None):
    return TaskNode(id=node_id, description=f"tarefa {node_id}", depends_on=list(deps), agent=agent)


def dag(*nodes, goal="meta"):
    return TaskDAG(goal=goal, nodes=list(nodes))


# ---------------------------------------------------------------- TaskNode
def test_task_node_defaults():
    n = TaskNode(id="a", description="x")
    assert n.depends_on == [] and n.agent is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"id": "a", "description": "x", "depends_on": ["a"]},
        {"id": "a", "description": "x", "depends_on": ["b", "b"]},
        {"id": "", "description": "x"},
        {"id": "a", "description": "  "},
        {"id": "a", "description": "x", "extra": 1},
    ],
)
def test_task_node_invalid_rejected(kwargs):
    with pytest.raises(ValidationError):
        TaskNode(**kwargs)


# ------------------------------------------------------------ validação
def test_valid_dag_and_lookup():
    d = dag(node("a"), node("b", "a"))
    assert d.get("b").depends_on == ["a"]
    assert d.dependents("a") == ["b"]
    assert d.dependents("b") == []
    with pytest.raises(KeyError):
        d.get("zzz")


def test_empty_dag_and_empty_goal_rejected():
    with pytest.raises(ValidationError):
        TaskDAG(goal="meta", nodes=[])
    with pytest.raises(ValidationError):
        TaskDAG(goal="", nodes=[node("a")])


def test_duplicate_ids_rejected():
    with pytest.raises(ValidationError, match="duplicados"):
        dag(node("a"), node("a"))


def test_missing_dependency_rejected():
    with pytest.raises(ValidationError, match="inexistentes"):
        dag(node("a", "ghost"))


def test_two_node_cycle_rejected():
    with pytest.raises(ValidationError, match="Ciclo de dependências"):
        dag(node("a", "b"), node("b", "a"))


def test_long_cycle_rejected_with_path():
    nodes = [node("a", "d"), node("b", "a"), node("c", "b"), node("d", "c"), node("ok")]
    cycle = find_cycle(nodes)
    assert cycle[0] == cycle[-1] and set(cycle) == {"a", "b", "c", "d"}
    with pytest.raises(ValidationError, match="Ciclo"):
        TaskDAG(goal="g", nodes=nodes)


def test_cycle_error_exposes_cycle():
    err = CycleError(["a", "b", "a"])
    assert err.cycle == ["a", "b", "a"] and "a -> b -> a" in str(err)


def test_find_cycle_none_for_acyclic_and_diamond():
    assert find_cycle([node("a"), node("b", "a"), node("c", "a"), node("d", "b", "c")]) is None


def test_find_cycle_ignores_unknown_dependencies():
    assert find_cycle([node("a", "ghost")]) is None


# ------------------------------------------------- ordem, lotes, prontos
def test_topological_order_and_levels_for_diamond():
    d = dag(node("a"), node("b", "a"), node("c", "a"), node("d", "b", "c"))
    order = d.topological_order()
    assert order[0] == "a" and order[-1] == "d" and set(order) == {"a", "b", "c", "d"}
    assert d.levels() == [["a"], ["b", "c"], ["d"]]


def test_levels_independent_nodes_share_one_batch():
    assert dag(node("a"), node("b"), node("c")).levels() == [["a", "b", "c"]]


def test_ready_progression():
    d = dag(node("a"), node("b"), node("c", "a", "b"))
    assert [n.id for n in d.ready(set())] == ["a", "b"]
    assert [n.id for n in d.ready({"a"})] == ["b"]
    assert [n.id for n in d.ready({"a", "b"})] == ["c"]
    assert d.ready({"a", "b", "c"}) == []


# ---------------------------------------------------------------- run_dag
async def test_run_dag_passes_dependency_results():
    d = dag(node("a"), node("b"), node("c", "a", "b"))

    async def runner(n, deps):
        return f"{n.id}:{sorted(deps.items())}"

    results = await run_dag(d, runner)
    assert results["a"] == "a:[]"
    assert results["c"] == "c:[('a', 'a:[]'), ('b', 'b:[]')]"


async def test_run_dag_respects_dependency_order():
    d = dag(node("a"), node("b", "a"), node("c", "b"))
    log = []

    async def runner(n, deps):
        log.append(f"start {n.id}")
        await asyncio.sleep(0.01)
        log.append(f"end {n.id}")

    await run_dag(d, runner)
    assert log == ["start a", "end a", "start b", "end b", "start c", "end c"]


async def test_run_dag_max_concurrency_limits_parallelism():
    d = dag(*(node(f"n{i}") for i in range(6)))
    running = peak = 0

    async def runner(n, deps):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1

    await run_dag(d, runner, max_concurrency=2)
    assert peak == 2


async def test_run_dag_propagates_first_error_and_cancels_rest():
    d = dag(node("bad"), node("slow"), node("after", "bad"))
    finished = []

    async def runner(n, deps):
        if n.id == "bad":
            raise RuntimeError("falhou")
        if n.id == "slow":
            await asyncio.sleep(5)
            finished.append("slow")

    with pytest.raises(RuntimeError, match="falhou"):
        await asyncio.wait_for(run_dag(d, runner), timeout=2)
    assert finished == []
