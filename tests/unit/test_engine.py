"""MasterEngine: planejamento com agentes reais, execução do DAG e síntese."""

import asyncio

import pytest

from src.core import MasterEngine, NodeState, PlannerEngine, TaskDAG, TaskNode
from src.subagents_bridge import SubagentRegistry, SwarmTelemetry
from tests.helpers import FakeLLM, Worker, plan_json


def make_engine(*workers, llm=None, **kwargs):
    registry, telemetry = SubagentRegistry(), SwarmTelemetry()
    for worker in workers:
        registry.register(worker)
    return MasterEngine(PlannerEngine(llm or FakeLLM()), registry, telemetry, **kwargs), registry, telemetry


def dag(*nodes):
    return TaskDAG(goal="meta", nodes=[TaskNode(id=i, description=f"passo {i}", depends_on=list(d), agent=a) for i, a, *d in nodes])


async def test_plan_tells_the_llm_which_agents_exist():
    llm = FakeLLM(plan_json(("t1", "dev-agent")))
    engine, *_ = make_engine(Worker("dev-agent"), llm=llm)
    plan = await engine.plan("fazer algo", context="contexto extra")
    assert plan.get("t1").agent == "dev-agent"
    system, user = llm.calls[0]
    assert "dev-agent" in system["content"] and "contexto extra" in user["content"]


async def test_plan_without_agents_forces_null_agent():
    llm = FakeLLM(plan_json(("t1", None)))
    engine, *_ = make_engine(llm=llm)
    await engine.plan("meta")
    assert "Não há agentes disponíveis" in llm.calls[0][0]["content"]


async def test_run_success_records_telemetry_and_emits_events():
    worker = Worker("dev-agent")
    engine, _, telemetry = make_engine(worker)
    events = []
    report = await engine.run(dag(("a", "dev-agent"), ("b", "dev-agent", "a")), events.append)

    assert report.success and report.counts()["done"] == 2
    assert report.outcomes["a"].summary == "feito: passo a" and report.outcomes["a"].duration >= 0
    assert [t.goal for t in worker.tasks] == ["passo a", "passo b"]
    stats = telemetry.stats("dev-agent")
    assert stats.calls == 2 and stats.total_tokens == 30
    by_node = {n: [e.state for e in events if e.node_id == n] for n in "ab"}
    assert by_node["a"] == [NodeState.PENDING, NodeState.RUNNING, NodeState.DONE]
    assert by_node["b"][-1] is NodeState.DONE
    done_a = next(i for i, e in enumerate(events) if e.node_id == "a" and e.state is NodeState.DONE)
    running_b = next(i for i, e in enumerate(events) if e.node_id == "b" and e.state is NodeState.RUNNING)
    assert done_a < running_b  # dependente só começa depois


async def test_independent_nodes_run_in_parallel():
    engine, *_ = make_engine(Worker("w", delay=0.1))
    loop = asyncio.get_running_loop()
    started = loop.time()
    report = await engine.run(dag(("a", "w"), ("b", "w"), ("c", "w")))
    assert report.success and loop.time() - started < 0.25  # ~0,1s, não 0,3s


async def test_max_concurrency_is_respected():
    worker = Worker("w", delay=0.05)
    engine, *_ = make_engine(worker, max_concurrency=1)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await engine.run(dag(("a", "w"), ("b", "w"), ("c", "w")))
    assert loop.time() - started >= 0.14  # serializado


async def test_failure_skips_dependents_but_not_independent_branches():
    engine, *_ = make_engine(Worker("ok"), Worker("bad", mode="fail"))
    report = await engine.run(dag(("a", "bad"), ("b", "ok", "a"), ("c", "ok"), ("d", "ok", "b")))
    assert {k: v.state for k, v in report.outcomes.items()} == {
        "a": NodeState.FAILED, "b": NodeState.SKIPPED, "c": NodeState.DONE, "d": NodeState.SKIPPED,
    }  # fmt: skip
    assert report.outcomes["a"].error == "não deu" and report.outcomes["a"].summary == "parcial"
    assert "dependência não concluída: a" in report.outcomes["b"].error
    assert not report.success and report.counts() == {"pending": 0, "running": 0, "done": 1, "failed": 1, "skipped": 2}


async def test_agent_exception_becomes_failed_node():
    engine, *_ = make_engine(Worker("boom", mode="raise"))
    report = await engine.run(dag(("a", "boom")))
    assert report.outcomes["a"].state is NodeState.FAILED
    assert report.outcomes["a"].error == "RuntimeError: quebrou"


async def test_nodes_without_executor_are_skipped_honestly():
    engine, *_ = make_engine(Worker("w"))
    report = await engine.run(dag(("a", None), ("b", "fantasma")))
    assert report.outcomes["a"].error == "nenhum executor disponível para 'master'"
    assert report.outcomes["b"].error == "nenhum executor disponível para 'fantasma'"
    assert report.counts()["skipped"] == 2


async def test_synthesize_lists_every_node_in_order():
    engine, *_ = make_engine(Worker("ok"), Worker("bad", mode="fail"))
    plan = dag(("a", "ok"), ("b", "bad", "a"), ("c", None, "b"))
    text = MasterEngine.synthesize(await engine.run(plan))
    lines = text.splitlines()
    assert lines[0] == "✔ a: passo a — feito: passo a"
    assert lines[1] == "✖ b: passo b — não deu"
    assert lines[2].startswith("⊘ c: passo c — dependência não concluída: b")
    assert "1 concluída(s), 1 com falha, 1 ignorada(s)." in text


def test_synthesize_marks_nodes_that_never_ran():
    from src.core import RunReport

    text = MasterEngine.synthesize(RunReport(dag(("a", None))))
    assert text.startswith("○ a: passo a")


async def test_cancel_all_reaches_every_active_task():
    one, two = Worker("one"), Worker("two")
    engine, _, telemetry = make_engine(one, two)
    telemetry.record_start("one", "t1")
    telemetry.record_start("one", "t2")
    telemetry.record_start("two", "t3")
    assert await engine.cancel_all() == 3
    assert one.cancelled == ["t1", "t2"] and two.cancelled == ["t3"]
    assert await make_engine()[0].cancel_all() == 0


async def test_run_can_be_cancelled_midway():
    worker = Worker("slow", delay=5)
    engine, *_ = make_engine(worker)
    task = asyncio.create_task(engine.run(dag(("a", "slow"))))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
