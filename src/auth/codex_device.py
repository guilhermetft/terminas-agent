"""Login com ChatGPT por código de dispositivo (sem navegador local, sem porta, sem plano pago exigido
pelo fluxo oficial de terceiros) e cliente do backend do Codex.

O usuário abre uma página da OpenAI, digita um código curto e o Termina recebe os tokens por polling.
Os tokens valem para o backend do Codex (`chatgpt.com/backend-api/codex`), e a conta e o plano é que
decidem o que está liberado. Este fluxo usa o client id público do Codex CLI; a OpenAI não o documenta
para terceiros (ver docs/7, seção 7.9)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any

import jwt

from src.auth.chatgpt import (
    ISSUER,
    ChatGPTAuthError,
    ChatGPTOAuth,
    Tokens,
)
from src.auth.responses import ResponsesClient
from src.utils import http

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
USERCODE_URL = f"{ISSUER}/api/accounts/deviceauth/usercode"
POLL_URL = f"{ISSUER}/api/accounts/deviceauth/token"
VERIFICATION_URL = f"{ISSUER}/codex/device"
REDIRECT_URI = f"{ISSUER}/deviceauth/callback"
BACKEND_URL = "https://chatgpt.com/backend-api/codex"
ACCOUNT_CLAIM = "https://api.openai.com/auth"
DEFAULT_INTERVAL = 5.0
MAX_WAIT = 900.0  # 15 min
DEFAULT_INSTRUCTIONS = "You are a helpful assistant."  # o backend exige `instructions` não vazio

CodeHandler = Callable[[str, str], None]  # (url, código)
Sleeper = Callable[[float], Any]


def _error_code(raw: str) -> str:
    import json

    try:
        error = json.loads(raw).get("error", {})
    except (ValueError, AttributeError):
        return ""
    if isinstance(error, dict):
        return str(error.get("code") or error.get("type") or "")
    return str(error)


def account_id_from(*tokens: str) -> str | None:
    """`chatgpt_account_id` das claims do token (sem validar assinatura: é só um identificador de cabeçalho)."""
    for token in tokens:
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            continue
        info = claims.get(ACCOUNT_CLAIM)
        if isinstance(info, dict) and isinstance(info.get("chatgpt_account_id"), str):
            return info["chatgpt_account_id"]
    return None


class CodexDeviceAuth(ChatGPTOAuth):
    refresh_extra: dict[str, str] = {}  # o refresh do Codex não leva `resource`

    def __init__(self, *args: Any, sleep: Sleeper = asyncio.sleep, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._sleep = sleep

    def account_id(self) -> str | None:
        value = self.store.read().get("account_id")
        return value if isinstance(value, str) and value else None

    async def login(self, on_code: CodeHandler) -> Tokens:  # type: ignore[override]
        """Pede o código, mostra (`on_code`) e espera a autorização. Grava ao final."""
        start = await asyncio.to_thread(self._request_code)
        device_id, user_code = start["device_auth_id"], start["user_code"]
        on_code(VERIFICATION_URL, user_code)
        try:
            self._open_browser(VERIFICATION_URL)
        except Exception:  # noqa: BLE001 - a URL já foi mostrada
            pass
        interval = max(1.0, float(start.get("interval") or DEFAULT_INTERVAL))
        deadline = time.monotonic() + min(MAX_WAIT, self._seconds_left(start))
        while True:
            await self._sleep(interval)
            granted = await asyncio.to_thread(self._poll, device_id, user_code)
            if granted is not None:
                break
            if time.monotonic() > deadline:
                raise ChatGPTAuthError("O código expirou antes de ser autorizado. Rode /login de novo.")
        return await asyncio.to_thread(self._exchange, granted)

    def _seconds_left(self, start: Mapping[str, Any]) -> float:
        raw = start.get("expires_at")
        if isinstance(raw, str):
            try:
                return max(30.0, (datetime.fromisoformat(raw).timestamp() - self._clock()))
            except ValueError:
                pass
        return MAX_WAIT

    def _request_code(self) -> dict[str, str]:
        try:
            response = self._post(USERCODE_URL, json_body={"client_id": CLIENT_ID}, timeout=30)
        except http.HttpError as exc:
            raise ChatGPTAuthError(f"Não consegui falar com a OpenAI: {exc}", offer_api_key=False) from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status != 200 or not isinstance(body, dict):
            raise ChatGPTAuthError(f"A OpenAI não gerou o código de login (HTTP {response.status}).")
        if not isinstance(body.get("device_auth_id"), str) or not isinstance(body.get("user_code"), str):
            raise ChatGPTAuthError("A resposta da OpenAI não trouxe o código de login esperado.")
        return {k: v for k, v in body.items() if isinstance(v, str)}

    def _poll(self, device_id: str, user_code: str) -> dict[str, str] | None:
        """None enquanto pendente; dict com o código de autorização quando liberado."""
        try:
            response = self._post(POLL_URL, json_body={"device_auth_id": device_id, "user_code": user_code}, timeout=30)
        except http.HttpError:
            return None  # falha de rede passageira: tenta no próximo ciclo
        if response.status == 200:
            try:
                body = response.json()
            except ValueError:
                body = {}
            if isinstance(body, dict) and isinstance(body.get("authorization_code"), str) and isinstance(
                body.get("code_verifier"), str
            ):
                return {"code": body["authorization_code"], "verifier": body["code_verifier"]}
            raise ChatGPTAuthError("A autorização voltou sem o código esperado.")
        code = _error_code(response.body)
        if response.status in {403, 404} and code in {"deviceauth_authorization_pending", ""}:
            return None
        if "expired" in code or response.status == 410:
            raise ChatGPTAuthError("O código expirou antes de ser autorizado. Rode /login de novo.")
        if "denied" in code:
            raise ChatGPTAuthError("Você negou a autorização no navegador.")
        raise ChatGPTAuthError(f"A OpenAI recusou a autorização (HTTP {response.status}{': ' + code if code else ''}).")

    def _exchange(self, granted: Mapping[str, str]) -> Tokens:
        body = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": granted["code"],
                "redirect_uri": REDIRECT_URI,
                "client_id": CLIENT_ID,
                "code_verifier": granted["verifier"],
            }
        )
        access, refresh, id_token = body.get("access_token"), body.get("refresh_token"), body.get("id_token")
        if not isinstance(access, str) or not isinstance(refresh, str):
            raise ChatGPTAuthError("A resposta do login não trouxe os tokens esperados.")
        if isinstance(id_token, str):
            self.validate_id_token(id_token, client_id=CLIENT_ID, nonce=None)
        tokens = Tokens(access, refresh, self._clock() + float(body.get("expires_in", 3600)), str(body.get("scope", "")))
        self._save(CLIENT_ID, tokens)
        account = account_id_from(*(t for t in (id_token, access) if isinstance(t, str)))
        if account:
            self.store.update(account_id=account)
        return tokens


class CodexBackendClient(ResponsesClient):
    """Responses API do backend do Codex: cabeçalho de conta e `instructions` sempre presente."""

    def __init__(self, account_id: Callable[[], str | None], **kwargs: Any) -> None:
        super().__init__(base_url=BACKEND_URL, **kwargs)
        self._account_id = account_id

    default_instructions = DEFAULT_INSTRUCTIONS

    def extra_headers(self) -> dict[str, str]:
        headers = {"OpenAI-Beta": "responses=experimental", "originator": "terminas"}
        account = self._account_id()
        if account:
            headers["chatgpt-account-id"] = account
        return headers

    def parse_models(self, payload: object) -> list[str]:
        items: Iterable[Any] = []
        if isinstance(payload, dict):
            items = payload.get("models") or payload.get("data") or []
        ids = set()
        for item in items:
            if isinstance(item, dict) and item.get("visibility", "list") == "list":
                name = item.get("slug") or item.get("id")
                if isinstance(name, str) and name:
                    ids.add(name)
        return sorted(ids)

    models_path = "/models?client_version=1.0.0"
