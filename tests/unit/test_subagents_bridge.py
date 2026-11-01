"""Testes de BaseSubagent e SubagentRegistry (RF-02, US-08)."""

import pytest
from pydantic import ValidationError

from src.subagents_bridge import (
    AgentNotFoundError,
    AgentResult,
    AgentTask,
    BaseSubagent,
    DuplicateAgentError,
    SubagentInfo,
    SubagentRegistry,
    SubagentRegistryError,
)


class FakeAgent(BaseSubagent):
    """Subagente de teste: nenhuma dependência de implementação real."""

    def __init__(self, name="dev-agent", capabilities=("code", "test")):
        super().__init__(SubagentInfo(name=name, capabilities=list(capabilities)))

    async def execute(self, task: AgentTask) -> AgentResult:
        return AgentResult(task_id=task.task_id, status="success", summary=f"{self.name} ok")


@pytest.fixture
def registry():
    return SubagentRegistry()


def test_base_subagent_is_abstract():
    with pytest.raises(TypeError):
        BaseSubagent(SubagentInfo(name="x", capabilities=["a"]))


async def test_subagent_execute_cancel_health_defaults():
    agent = FakeAgent()
    task = AgentTask(assigned_agent="dev-agent", goal="g")
    result = await agent.execute(task)
    assert result.task_id == task.task_id and result.summary == "dev-agent ok"
    assert await agent.cancel(task.task_id) is None
    assert await agent.health() is True
    assert agent.name == "dev-agent"


def test_register_get_and_contains(registry):
    agent = FakeAgent()
    registry.register(agent)
    assert registry.get("dev-agent") is agent
    assert "dev-agent" in registry and "outro" not in registry
    assert len(registry) == 1
    assert [i.name for i in registry.list_info()] == ["dev-agent"]


def test_get_unknown_raises_not_found(registry):
    with pytest.raises(AgentNotFoundError, match="ghost"):
        registry.get("ghost")
    assert issubclass(AgentNotFoundError, (SubagentRegistryError, LookupError))


def test_duplicate_register_rejected_unless_replace(registry):
    first, second = FakeAgent(), FakeAgent()
    registry.register(first)
    with pytest.raises(DuplicateAgentError):
        registry.register(second)
    assert registry.get("dev-agent") is first
    registry.register(second, replace=True)
    assert registry.get("dev-agent") is second


def test_register_rejects_non_subagent(registry):
    with pytest.raises(TypeError, match="BaseSubagent"):
        registry.register(object())


def test_register_validates_capabilities(registry):
    agent = FakeAgent()
    agent.info = agent.info.model_copy(update={"capabilities": []})  # contorna a validação
    with pytest.raises(ValidationError):
        registry.register(agent)
    assert len(registry) == 0


def test_unregister(registry):
    registry.register(FakeAgent())
    assert registry.unregister("dev-agent").name == "dev-agent"
    assert len(registry) == 0
    with pytest.raises(AgentNotFoundError):
        registry.unregister("dev-agent")


def test_find_by_capability(registry):
    registry.register(FakeAgent("dev-agent", ["code", "test"]))
    registry.register(FakeAgent("research-agent", ["search"]))
    assert [a.name for a in registry.find_by_capability("test")] == ["dev-agent"]
    assert [a.name for a in registry.find_by_capability("search")] == ["research-agent"]
    assert registry.find_by_capability("nada") == []


async def test_resolve_task_to_agent_and_execute(registry):
    registry.register(FakeAgent())
    task = AgentTask(assigned_agent="dev-agent", goal="g")
    result = await registry.resolve(task).execute(task)
    assert result.status.value == "success"
    with pytest.raises(AgentNotFoundError):
        registry.resolve(AgentTask(assigned_agent="ghost", goal="g"))
