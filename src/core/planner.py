"""PlannerEngine: transforma uma meta em linguagem natural em um TaskDAG validado via LLM."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from src.core.dag import TaskDAG, TaskNode

ChatMessage = dict[str, str]
LLMClient = Callable[[list[ChatMessage]], Awaitable[str]]
"""Recebe mensagens de chat e devolve o texto bruto da resposta (JSON)."""

DEFAULT_MODEL = "gemini/gemini-2.5-flash"
DEFAULT_MAX_NODES = 20
DEFAULT_MAX_RETRIES = 2

SYSTEM_PROMPT = """\
Você é o Planner do Termina's Agent. Decomponha a meta do usuário em um grafo acíclico \
dirigido (DAG) de tarefas atômicas.

Responda SOMENTE com JSON, sem texto extra, neste formato:
{{"nodes": [{{"id": "t1", "description": "...", "depends_on": [], "agent": null}}]}}

Regras:
- "id": único e curto (t1, t2, ...).
- "depends_on": ids de tarefas que precisam terminar antes; vazio se for independente.
- Tarefas sem dependência mútua devem ficar independentes, para rodarem em paralelo.
- Nunca crie ciclos nem dependa de ids inexistentes.
- "agent": nome do agente que executa a tarefa, ou null para o próprio Master.
- No máximo {max_nodes} tarefas; use o mínimo necessário.
{agents_rule}"""


class PlanningError(RuntimeError):
    """O LLM não produziu um plano válido dentro do limite de tentativas."""


class _PlanSpec(BaseModel):
    """Formato esperado da resposta do LLM (a meta é adicionada pelo engine)."""

    model_config = ConfigDict(extra="forbid")

    nodes: list[TaskNode]


def _extract_json(text: str) -> Any:
    """Faz o parse do JSON, tolerando cercas de código markdown (```json ... ```)."""
    cleaned = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.DOTALL)
    if fenced:
        cleaned = fenced.group(1)
    return json.loads(cleaned)


def litellm_client(model: str = DEFAULT_MODEL, **kwargs: Any) -> LLMClient:
    """Cria um `LLMClient` sobre o LiteLLM (import tardio: só carrega se for usado)."""

    async def call(messages: list[ChatMessage]) -> str:
        import litellm

        response = await litellm.acompletion(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
            **kwargs,
        )
        return response.choices[0].message.content or ""

    return call


class PlannerEngine:
    """Decompõe metas em DAGs. O LLM é injetável (mockável em testes, custo zero de tokens)."""

    def __init__(
        self,
        llm: LLMClient | None = None,
        *,
        model: str = DEFAULT_MODEL,
        max_nodes: int = DEFAULT_MAX_NODES,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self.llm = llm or litellm_client(model)
        self.max_nodes = max_nodes
        self.max_retries = max_retries

    def _messages(
        self, goal: str, context: str | None, agents: Sequence[str] | None
    ) -> list[ChatMessage]:
        agents_rule = (
            f"- \"agent\" deve ser um destes: {', '.join(agents)} (ou null)." if agents else ""
        )
        system = SYSTEM_PROMPT.format(max_nodes=self.max_nodes, agents_rule=agents_rule)
        user = f"Meta: {goal}"
        if context:
            user += f"\n\nContexto relevante:\n{context}"
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def _parse(self, raw: str, goal: str, agents: Sequence[str] | None) -> TaskDAG:
        spec = _PlanSpec.model_validate(_extract_json(raw))
        if len(spec.nodes) > self.max_nodes:
            raise ValueError(f"Plano com {len(spec.nodes)} nós excede o máximo de {self.max_nodes}.")
        if agents is not None:
            allowed = set(agents)
            for node in spec.nodes:
                if node.agent is not None and node.agent not in allowed:
                    raise ValueError(f"Nó '{node.id}' usa agente desconhecido '{node.agent}'.")
        return TaskDAG(goal=goal, nodes=spec.nodes)

    async def create_dag(
        self,
        goal: str,
        *,
        context: str | None = None,
        agents: Sequence[str] | None = None,
    ) -> TaskDAG:
        """Gera e valida o DAG da meta. Se a resposta for inválida (JSON quebrado, ciclo,
        dependência inexistente...), devolve o erro ao LLM e tenta de novo até `max_retries`.
        """
        goal = goal.strip()
        if not goal:
            raise ValueError("A meta não pode ser vazia.")

        messages = self._messages(goal, context, agents)
        last_error: Exception | None = None
        for _ in range(self.max_retries + 1):
            raw = await self.llm(messages)
            try:
                return self._parse(raw, goal, agents)
            except (ValueError, ValidationError) as exc:  # JSONDecodeError é ValueError
                last_error = exc
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": f"Plano inválido: {exc}\nCorrija e responda só com o JSON.",
                    },
                ]
        raise PlanningError(
            f"Não foi possível gerar um plano válido após {self.max_retries + 1} tentativas: "
            f"{last_error}"
        ) from last_error


# Nome usado em docs/4 (CT-01).
Planner = PlannerEngine
