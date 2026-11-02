"""Cliente de LLM que usa sempre o provedor e o modelo ativos no momento da chamada."""

from __future__ import annotations

from src.auth.manager import AuthManager, NotAuthenticatedError
from src.core.planner import ChatMessage, LLMClient, litellm_client


def authenticated_llm(auth: AuthManager) -> LLMClient:
    """`/login`, `/logout` e `/model` passam a valer na hora, sem recriar o planner."""

    async def call(messages: list[ChatMessage]) -> str:
        model = auth.active_model()
        if model is None:
            raise NotAuthenticatedError("Nenhum provedor de IA conectado. Use /login.")
        spec = auth.active_provider()
        if spec is not None and auth.uses_chatgpt(spec):
            return await auth.complete_chatgpt(messages)  # Responses API, plano ChatGPT
        return await litellm_client(model)(messages)

    return call
