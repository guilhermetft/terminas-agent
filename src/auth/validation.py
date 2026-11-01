"""Validação de chaves de API e listagem de modelos, com uma chamada barata ao provedor."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from src.auth.providers import ProviderSpec
from src.utils import http

Verdict = Literal["ok", "invalid", "rate_limited", "unreachable", "bad_format"]
Getter = Callable[..., http.HttpResponse]

_NON_CHAT_OPENAI = (
    "embedding", "whisper", "tts", "dall-e", "moderation", "image", "audio", "realtime",
    "transcribe", "search", "-codex", "computer-use", "davinci", "babbage", "instruct",
)  # fmt: skip


@dataclass(frozen=True)
class KeyCheck:
    verdict: Verdict
    message: str = ""
    models: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verdict in {"ok", "rate_limited"}


def _models_request(spec: ProviderSpec, key: str, base: str) -> tuple[str, dict[str, str]]:
    if spec.id == "claude":
        return f"{base}/v1/models?limit=100", {"x-api-key": key, "anthropic-version": "2023-06-01"}
    if spec.id == "codex":
        return f"{base}/v1/models", {"Authorization": f"Bearer {key}"}
    return f"{base}/v1beta/models?pageSize=100", {"x-goog-api-key": key}


def _extract_models(spec: ProviderSpec, payload: object) -> list[str]:
    """Ids de modelos de chat (sem prefixo do LiteLLM), ordenados."""
    if not isinstance(payload, dict):
        return []
    if spec.id == "gemini":
        ids = [
            str(m.get("name", "")).removeprefix("models/")
            for m in payload.get("models", [])
            if "generateContent" in (m.get("supportedGenerationMethods") or [])
        ]
    else:
        ids = [str(m.get("id", "")) for m in payload.get("data", []) if isinstance(m, dict)]
    if spec.id == "codex":
        ids = [i for i in ids if i and not any(bad in i for bad in _NON_CHAT_OPENAI)]
    return sorted({i for i in ids if i})


class ProviderApi:
    def __init__(self, base_urls: Mapping[str, str] | None = None, getter: Getter = http.get) -> None:
        self._base_urls = dict(base_urls or {})
        self._get = getter

    def check_sync(self, spec: ProviderSpec, key: str) -> KeyCheck:
        key = key.strip()
        if len(key) < spec.min_key_length or any(c.isspace() for c in key):
            return KeyCheck("bad_format", "A chave parece incompleta (confira se copiou inteira, sem espaços).")
        base = self._base_urls.get(spec.id, spec.api_base)
        url, headers = _models_request(spec, key, base)
        try:
            response = self._get(url, headers=headers, timeout=15)
        except http.HttpError as exc:
            return KeyCheck("unreachable", f"Não consegui falar com {spec.label}: {exc}")
        status = response.status
        # O Gemini responde 400 (API_KEY_INVALID) para chave inválida; os outros, 401/403.
        if status in {401, 403} or (status == 400 and spec.id == "gemini"):
            return KeyCheck("invalid", f"{spec.label} recusou a chave (HTTP {status}).")
        if status == 429:
            return KeyCheck("rate_limited", "Limite de uso atingido, mas a chave foi aceita.")
        if status != 200:
            return KeyCheck("unreachable", f"{spec.label} respondeu HTTP {status}; não consegui validar a chave.")
        try:
            models = _extract_models(spec, response.json())
        except ValueError:
            models = []
        return KeyCheck("ok", "", models)

    async def check(self, spec: ProviderSpec, key: str) -> KeyCheck:
        return await asyncio.to_thread(self.check_sync, spec, key)
