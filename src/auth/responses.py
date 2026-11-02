"""Cliente da Responses API da OpenAI para o acesso pelo plano ChatGPT.

Só `POST /v1/responses` com `store: false` e `stream: true`; sem `temperature`, `max_output_tokens`,
`top_p`, `previous_response_id` etc. Mensagens `system` viram `instructions`; o histórico vai em `input`."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.auth.chatgpt import USAGE_URL, ChatGPTAuthError
from src.utils import http

BASE_URL = "https://api.openai.com/v1"
Streamer = Callable[..., tuple[int, Iterable[str]]]
Getter = Callable[..., http.HttpResponse]

USAGE_LIMIT_CODE = "subscription_sharing_usage_limit_exceeded"


class UsageLimitError(ChatGPTAuthError):
    """Limite de uso do plano ChatGPT atingido (HTTP 429)."""


class AccessDeniedError(ChatGPTAuthError):
    """Sem consentimento/escopo, ou conta não elegível para o acesso pelo plano."""


class TokenRejectedError(ChatGPTAuthError):
    """HTTP 401: o access token não vale (o gerente tenta renovar uma vez)."""

    def __init__(self) -> None:
        super().__init__("A OpenAI recusou o access token do plano ChatGPT.")


def build_payload(messages: Iterable[Mapping[str, str]], model: str, default_instructions: str | None = None) -> dict[str, Any]:
    system = [m["content"] for m in messages if m.get("role") == "system"]
    items = [{"role": m["role"], "content": m["content"]} for m in messages if m.get("role") != "system"]
    payload: dict[str, Any] = {"model": model, "input": items, "store": False, "stream": True}
    if system:
        payload["instructions"] = "\n\n".join(system)
    if not system and default_instructions:
        payload["instructions"] = default_instructions
    return payload


def _error_info(raw: str) -> tuple[str, str]:
    try:
        error = json.loads(raw).get("error", {})
    except (ValueError, AttributeError):
        return "", ""
    if isinstance(error, dict):
        return str(error.get("code") or error.get("type") or ""), str(error.get("message") or "")
    return "", str(error)


def raise_for_status(status: int, raw: str) -> None:
    code, message = _error_info(raw)
    if status == 401:
        raise TokenRejectedError
    if status == 429 and code in {USAGE_LIMIT_CODE, "usage_limit_reached"}:
        raise UsageLimitError(
            f"O limite de uso do seu plano ChatGPT acabou. Veja {USAGE_URL}, espere liberar ou use uma chave de API."
        )
    if status == 429:
        raise ChatGPTAuthError("A OpenAI limitou as requisições (HTTP 429). Tente de novo em instantes.", offer_api_key=False)
    if status == 403 or code in {"consent_required", "insufficient_scope", "not_eligible", "usage_not_included"}:
        raise AccessDeniedError(
            "O plano ChatGPT não autorizou este app (consentimento ou escopo ausente, ou conta não elegível). "
            "Use uma chave de API."
        )
    detail = f": {message or code}" if (message or code) else ""
    raise ChatGPTAuthError(f"A OpenAI respondeu HTTP {status}{detail}.", offer_api_key=False)


def collect_text(lines: Iterable[str]) -> str:
    """Lê o stream SSE até `response.completed` e devolve o texto gerado."""
    chunks: list[str] = []
    final: str | None = None
    for raw in lines:
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except ValueError:
            continue
        kind = event.get("type", "")
        if kind == "response.output_text.delta":
            chunks.append(str(event.get("delta", "")))
        elif kind in {"response.failed", "error", "response.incomplete"}:
            body = event.get("response", {}).get("error") or event.get("error") or {}
            code = str(body.get("code", "")) if isinstance(body, dict) else ""
            if code == USAGE_LIMIT_CODE:
                raise_for_status(429, json.dumps({"error": body}))
            message = str(body.get("message", "")) if isinstance(body, dict) else ""
            raise ChatGPTAuthError(f"A resposta falhou: {message or code or kind}.", offer_api_key=False)
        elif kind == "response.completed":
            final = _output_text(event.get("response", {}))
            break
    text = "".join(chunks)
    if final is not None:
        return text or final
    if chunks:
        raise ChatGPTAuthError("A resposta terminou antes de response.completed.", offer_api_key=False)
    raise ChatGPTAuthError("A OpenAI não devolveu resposta.", offer_api_key=False)


def _output_text(response: Mapping[str, Any]) -> str:
    parts = []
    for item in response.get("output", []) or []:
        for content in (item.get("content") or []) if isinstance(item, dict) else []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                parts.append(str(content.get("text", "")))
    return "".join(parts)


class ResponsesClient:
    default_instructions: str | None = None
    models_path = "/models"

    def extra_headers(self) -> dict[str, str]:
        return {}

    def parse_models(self, payload: object) -> list[str]:
        data = payload.get("data", []) if isinstance(payload, dict) else []
        return sorted({str(m["id"]) for m in data if isinstance(m, dict) and m.get("visibility") == "list" and m.get("id")})

    def __init__(self, *, stream: Streamer = http.post_stream, get: Getter = http.get, base_url: str = BASE_URL) -> None:
        self._stream = stream
        self._get = get
        self._base = base_url

    def complete_sync(self, token: str, model: str, messages: Iterable[Mapping[str, str]]) -> str:
        try:
            status, lines = self._stream(
                f"{self._base}/responses",
                json_body=build_payload(list(messages), model, self.default_instructions),
                headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream", **self.extra_headers()},
                timeout=120,
            )
            if status != 200:
                raise_for_status(status, "".join(lines))
            return collect_text(lines)
        except http.HttpError as exc:
            raise ChatGPTAuthError(f"Não consegui falar com a OpenAI: {exc}", offer_api_key=False) from exc

    async def complete(self, token: str, model: str, messages: Iterable[Mapping[str, str]]) -> str:
        return await asyncio.to_thread(self.complete_sync, token, model, list(messages))

    def list_models_sync(self, token: str) -> list[str]:
        headers = {"Authorization": f"Bearer {token}", **self.extra_headers()}
        try:
            response = self._get(f"{self._base}{self.models_path}", headers=headers, timeout=15)
        except http.HttpError as exc:
            raise ChatGPTAuthError(f"Não consegui falar com a OpenAI: {exc}", offer_api_key=False) from exc
        if response.status != 200:
            raise_for_status(response.status, response.body)
        try:
            return self.parse_models(response.json())
        except ValueError:
            return []

    async def list_models(self, token: str) -> list[str]:
        return await asyncio.to_thread(self.list_models_sync, token)
