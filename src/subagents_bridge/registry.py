"""Registro dinâmico de subagentes externos, baseado só na interface `BaseSubagent`."""

from __future__ import annotations

from src.subagents_bridge.base import BaseSubagent
from src.subagents_bridge.schemas import AgentTask, SubagentInfo


class SubagentRegistryError(Exception):
    """Erro base do registry."""


class DuplicateAgentError(SubagentRegistryError):
    """Já existe um subagente com esse nome."""


class AgentNotFoundError(SubagentRegistryError, LookupError):
    """Nenhum subagente registrado com esse nome."""


class SubagentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, BaseSubagent] = {}

    def register(self, agent: BaseSubagent, *, replace: bool = False) -> None:
        """Registra um subagente após validar seus metadados.

        Levanta `TypeError` se não for `BaseSubagent`, `pydantic.ValidationError` se `info`
        for inválido e `DuplicateAgentError` se o nome já existir (salvo `replace=True`).
        """
        if not isinstance(agent, BaseSubagent):
            raise TypeError(f"Esperado BaseSubagent, recebido {type(agent).__name__}.")
        info = SubagentInfo.model_validate(agent.info.model_dump())
        if info.name in self._agents and not replace:
            raise DuplicateAgentError(f"Subagente '{info.name}' já registrado.")
        self._agents[info.name] = agent

    def unregister(self, name: str) -> BaseSubagent:
        try:
            return self._agents.pop(name)
        except KeyError:
            raise AgentNotFoundError(f"Subagente '{name}' não registrado.") from None

    def get(self, name: str) -> BaseSubagent:
        try:
            return self._agents[name]
        except KeyError:
            raise AgentNotFoundError(f"Subagente '{name}' não registrado.") from None

    def __contains__(self, name: object) -> bool:
        return name in self._agents

    def __len__(self) -> int:
        return len(self._agents)

    def list_info(self) -> list[SubagentInfo]:
        return [a.info for a in self._agents.values()]

    def find_by_capability(self, capability: str) -> list[BaseSubagent]:
        return [a for a in self._agents.values() if capability in a.info.capabilities]

    def resolve(self, task: AgentTask) -> BaseSubagent:
        """Retorna o subagente designado para a tarefa (`assigned_agent`)."""
        return self.get(task.assigned_agent)
