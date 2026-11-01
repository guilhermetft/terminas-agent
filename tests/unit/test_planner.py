"""Testes do PlannerEngine com LLM mockado (CT-01, CT-02; RF-01, RF-03)."""

import asyncio
import json
import sys
import types

import pytest

from src.core import Planner, PlannerEngine, PlanningError, TaskDAG, litellm_client, run_dag


class FakeLLM:
    """LLM determinístico: devolve respostas pré-definidas e registra as chamadas."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, messages):
        self.calls.append(messages)
        return self.responses.pop(0)


def plan_json(*nodes):
    return json.dumps({"nodes": list(nodes)})


def n(node_id, *deps, agent=None):
    return {"id": node_id, "description": f"passo {node_id}", "depends_on": list(deps), "agent": agent}


# ------------------------------------------------------------------ CT-01
async def test_ct01_sequential_goal_generates_two_dependent_nodes():
    goal = "Crie um script em Python para ler o arquivo dados.csv e em seguida gere os testes unitários"
    llm = FakeLLM(plan_json(n("t1"), n("t2", "t1")))
    dag = await Planner(llm).create_dag(goal)

    assert isinstance(dag, TaskDAG) and dag.goal == goal
    assert [x.id for x in dag.nodes] == ["t1", "t2"]
    assert dag.get("t1").depends_on == []
    assert dag.get("t2").depends_on == ["t1"]
    assert dag.levels() == [["t1"], ["t2"]]
    assert len(llm.calls) == 1


async def test_prompt_contains_goal_context_and_agents():
    llm = FakeLLM(plan_json(n("t1", agent="dev-agent")))
    await PlannerEngine(llm, max_nodes=7).create_dag(
        "  minha meta  ", context="README diz X", agents=["dev-agent", "research-agent"]
    )
    system, user = llm.calls[0]
    assert system["role"] == "system" and user["role"] == "user"
    assert "No máximo 7 tarefas" in system["content"]
    assert "dev-agent, research-agent" in system["content"]
    assert user["content"].startswith("Meta: minha meta")
    assert "README diz X" in user["content"]


async def test_prompt_without_agents_or_context():
    llm = FakeLLM(plan_json(n("t1")))
    await PlannerEngine(llm).create_dag("meta")
    system, user = llm.calls[0]
    assert "deve ser um destes" not in system["content"]
    assert "Contexto relevante" not in user["content"]


async def test_markdown_fenced_json_is_accepted():
    raw = "```json\n" + plan_json(n("t1")) + "\n```"
    dag = await PlannerEngine(FakeLLM(raw)).create_dag("meta")
    assert [x.id for x in dag.nodes] == ["t1"]


async def test_empty_goal_rejected_without_calling_llm():
    llm = FakeLLM()
    with pytest.raises(ValueError, match="vazia"):
        await PlannerEngine(llm).create_dag("   ")
    assert llm.calls == []


# ------------------------------------------------- validação e re-tentativa
@pytest.mark.parametrize(
    ("bad", "fragment"),
    [
        ("isto não é json", "Expecting value"),
        (json.dumps({"tasks": []}), "nodes"),
        (plan_json(n("a", "b"), n("b", "a")), "Ciclo de dependências"),
        (plan_json(n("a", "ghost")), "inexistentes"),
        (plan_json(n("a"), n("a")), "duplicados"),
        (plan_json(n("a", agent="intruso")), "agente desconhecido"),
    ],
)
async def test_invalid_plan_is_retried_with_error_feedback(bad, fragment):
    llm = FakeLLM(bad, plan_json(n("a", agent="dev-agent")))
    dag = await PlannerEngine(llm).create_dag("meta", agents=["dev-agent"])

    assert [x.id for x in dag.nodes] == ["a"]
    assert len(llm.calls) == 2
    feedback = llm.calls[1][-1]["content"]
    assert "Plano inválido" in feedback and fragment in feedback
    assert llm.calls[1][-2] == {"role": "assistant", "content": bad}


async def test_planning_error_after_exhausting_retries():
    cyclic = plan_json(n("a", "b"), n("b", "a"))
    llm = FakeLLM(cyclic, cyclic, cyclic)
    with pytest.raises(PlanningError, match="3 tentativas") as exc:
        await PlannerEngine(llm, max_retries=2).create_dag("meta")
    assert len(llm.calls) == 3
    assert exc.value.__cause__ is not None


async def test_zero_retries_means_single_attempt():
    llm = FakeLLM("lixo")
    with pytest.raises(PlanningError, match="1 tentativas"):
        await PlannerEngine(llm, max_retries=0).create_dag("meta")
    assert len(llm.calls) == 1


async def test_plan_over_max_nodes_rejected():
    big = plan_json(n("a"), n("b"), n("c"))
    llm = FakeLLM(big, plan_json(n("a")))
    dag = await PlannerEngine(llm, max_nodes=2).create_dag("meta")
    assert len(dag.nodes) == 1
    assert "excede o máximo de 2" in llm.calls[1][-1]["content"]


# ------------------------------------------------------------------ CT-02
async def test_ct02_independent_nodes_start_in_same_event_loop_cycle():
    llm = FakeLLM(plan_json(n("A", agent="deep-search"), n("B", agent="deep-search")))
    dag = await PlannerEngine(llm).create_dag("pesquisar tema X e tema Y")
    assert dag.levels() == [["A", "B"]]  # sem dependência mútua

    loop = asyncio.get_running_loop()
    starts = {}

    async def runner(node, deps):
        starts[node.id] = loop.time()
        await asyncio.sleep(0.05)

    began = loop.time()
    await run_dag(dag, runner)
    elapsed = loop.time() - began

    assert abs(starts["A"] - starts["B"]) < 0.05  # < 50 ms
    assert elapsed < 0.09  # paralelo: ~50 ms, não ~100 ms


# ---------------------------------------------------------------- LiteLLM
async def test_litellm_client_calls_acompletion_with_json_mode(monkeypatch):
    captured = {}

    async def acompletion(**kwargs):
        captured.update(kwargs)
        message = types.SimpleNamespace(content='{"nodes": []}')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(acompletion=acompletion))
    client = litellm_client("meu/modelo", temperature=0)
    out = await client([{"role": "user", "content": "oi"}])

    assert out == '{"nodes": []}'
    assert captured["model"] == "meu/modelo"
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["temperature"] == 0


async def test_litellm_client_handles_empty_content(monkeypatch):
    async def acompletion(**kwargs):
        message = types.SimpleNamespace(content=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(acompletion=acompletion))
    assert await litellm_client()([]) == ""


def test_default_llm_is_litellm_client():
    assert callable(PlannerEngine().llm)


async def test_prompt_tells_llm_when_no_agents_exist():
    llm = FakeLLM(plan_json(n("t1")))
    await PlannerEngine(llm).create_dag("meta", agents=[])
    assert "Não há agentes disponíveis" in llm.calls[0][0]["content"]
