"""Interface abstrata que todo subagente externo implementa para ser plugado no Termina."""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.subagents_bridge.schemas import AgentResult, AgentTask, SubagentInfo


class BaseSubagent(ABC):
    """Contrato de um subagente. O Termina só conhece esta interface, nunca a implementação."""

    def __init__(self, info: SubagentInfo) -> None:
        self.info = info

    @property
    def name(self) -> str:
        return self.info.name

    @abstractmethod
    async def execute(self, task: AgentTask) -> AgentResult:
        """Executa a tarefa e devolve um `AgentResult` com o mesmo `task_id`."""

    async def cancel(self, task_id: str) -> None:
        """Cancela uma tarefa em andamento. Padrão: não faz nada."""

    async def health(self) -> bool:
        """Verifica se o subagente está operacional (usado pela skill `swarm-inspector`)."""
        return True
