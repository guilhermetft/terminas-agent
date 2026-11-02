"""AuthManager: quem está logado, qual provedor/modelo está ativo e como entrar e sair."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Callable, Iterable, MutableMapping, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.auth.chatgpt import ChatGPTAuthError, ChatGPTOAuth, Tokens, UrlHandler
from src.auth.providers import (
    CODEX,
    PROVIDERS,
    ProviderSpec,
    choose_default_model,
    get_provider,
    provider_for_model,
)
from src.auth.secrets import mask
from src.auth.responses import ResponsesClient, TokenRejectedError
from src.auth.store import AuthState, ChatGPTStore, CredentialStore
from src.auth.validation import ProviderApi

Source = Literal["store", "env", "chatgpt"]


class NotAuthenticatedError(RuntimeError):
    """Nenhum provedor de IA está conectado."""


@dataclass(frozen=True)
class Credential:
    provider: ProviderSpec
    value: str
    # "store" = ~/.terminas/.env (via /login); "env" = variável de ambiente externa;
    # "chatgpt" = login pelo plano ChatGPT (value fica vazio: o token nunca sai do gerente)
    source: Source


@dataclass(frozen=True)
class ProviderStatus:
    spec: ProviderSpec
    logged_in: bool
    source: Source | None
    masked_key: str | None
    active: bool
    model: str | None  # id do modelo (sem prefixo do LiteLLM), só se logado


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    message: str
    provider: ProviderSpec
    model: str | None = None
    warning: str | None = None  # ex.: não deu para restringir as permissões do arquivo


@dataclass(frozen=True)
class LogoutOutcome:
    provider: ProviderSpec
    removed: bool  # havia credencial gravada pelo /login (chave de API ou tokens do ChatGPT)
    still_in_environment: bool  # a variável de ambiente externa continua definida


class AuthManager:
    def __init__(
        self,
        auth_dir: str | Path,
        *,
        environ: MutableMapping[str, str] | None = None,
        api: ProviderApi | None = None,
        preferred_model: str | None = None,
        chatgpt: ChatGPTOAuth | None = None,
        responses: ResponsesClient | None = None,
    ) -> None:
        self.directory = Path(auth_dir).expanduser()
        self.store = CredentialStore(self.directory / ".env")
        self.state = AuthState(self.directory / "auth.json")
        self.environ = os.environ if environ is None else environ
        self.api = api or ProviderApi()
        self.chatgpt = chatgpt or ChatGPTOAuth(ChatGPTStore(self.directory / "chatgpt.json"))
        self.chatgpt_store = self.chatgpt.store
        self.responses = responses or ResponsesClient()
        self._refresh_lock = threading.Lock()
        self._preferred_model = preferred_model  # ex.: llm.default_model do config.yaml

    # ------------------------------------------------------------------ credenciais
    def load_into_environment(self) -> list[str]:
        """Disponibiliza as chaves gravadas ao LiteLLM, sem sobrescrever variáveis já definidas."""
        stored = self.store.read()
        loaded = []
        for spec in PROVIDERS:
            if spec.env_var in stored and spec.env_var not in self.environ:
                self.environ[spec.env_var] = stored[spec.env_var]
                loaded.append(spec.env_var)
        return loaded

    def _api_key(self, spec: ProviderSpec) -> Credential | None:
        stored = self.store.get(spec.env_var)
        from_env = self.environ.get(spec.env_var)
        if from_env:
            return Credential(spec, from_env, "store" if from_env == stored else "env")
        if stored:
            return Credential(spec, stored, "store")
        return None

    def uses_chatgpt(self, spec: ProviderSpec) -> bool:
        """O Codex entra pelo plano ChatGPT quando há tokens e (é a forma escolhida ou não há chave)."""
        if spec.id != CODEX.id or not self.chatgpt_store.has_tokens():
            return False
        mode = self.state.read()["modes"].get(spec.id)
        return mode == "chatgpt" or self._api_key(spec) is None

    def credential(self, spec: ProviderSpec) -> Credential | None:
        if self.uses_chatgpt(spec):
            return Credential(spec, "", "chatgpt")
        return self._api_key(spec)

    @property
    def is_authenticated(self) -> bool:
        return any(self.credential(spec) for spec in PROVIDERS)

    # --------------------------------------------------------------- provedor / modelo
    def active_provider(self) -> ProviderSpec | None:
        saved = self.state.read()["active"]
        if saved:
            try:
                spec = get_provider(saved)
            except ValueError:
                spec = None
            if spec and self.credential(spec):
                return spec
        return next((spec for spec in PROVIDERS if self.credential(spec)), None)

    def model_id(self, spec: ProviderSpec) -> str:
        saved = self.state.read()["models"].get(spec.id)
        if isinstance(saved, str) and saved:
            return saved
        if self._preferred_model and provider_for_model(self._preferred_model) is spec:
            return self._preferred_model.split("/", 1)[1]
        return spec.default_model

    def active_model(self) -> str | None:
        """Modelo no formato do LiteLLM (ex.: 'anthropic/claude-sonnet-5-5'), ou None sem login."""
        spec = self.active_provider()
        return None if spec is None else spec.litellm_model(self.model_id(spec))

    def status(self) -> list[ProviderStatus]:
        active = self.active_provider()
        rows = []
        for spec in PROVIDERS:
            cred = self.credential(spec)
            rows.append(
                ProviderStatus(
                    spec=spec,
                    logged_in=cred is not None,
                    source=cred.source if cred else None,
                    masked_key=mask(cred.value) if cred and cred.source != "chatgpt" else None,
                    active=bool(cred) and spec is active,
                    model=self.model_id(spec) if cred else None,
                )
            )
        return rows

    def set_active(self, spec: ProviderSpec, model_id: str | None = None) -> None:
        if self.credential(spec) is None:
            raise NotAuthenticatedError(f"{spec.label} não está conectado. Use /login {spec.id}.")
        state = self.state.read()
        state["active"] = spec.id
        if model_id:
            state["models"][spec.id] = model_id
        self.state.write(state)

    async def list_models(self, spec: ProviderSpec) -> list[str]:
        cred = self.credential(spec)
        if cred is None:
            raise NotAuthenticatedError(f"{spec.label} não está conectado. Use /login {spec.id}.")
        if cred.source == "chatgpt":
            return await self._call_chatgpt(lambda token: self.responses.list_models(token))
        return (await self.api.check(spec, cred.value)).models

    # ------------------------------------------------------------- plano ChatGPT
    def _valid_token_sync(self, *, force: bool = False) -> str:
        with self._refresh_lock:
            tokens = self.chatgpt.stored_tokens()
            if tokens is None:
                raise ChatGPTAuthError("Sessão ChatGPT ausente. Use /login codex.")
            if force or self.chatgpt.needs_refresh(tokens):
                tokens = self.chatgpt.refresh()
            return tokens.access_token

    async def _call_chatgpt(self, action: Callable[[str], object]):  # noqa: ANN202
        """Executa `action(token)` com um token válido; renova sozinho (e tenta de novo 1x num 401)."""
        token = await asyncio.to_thread(self._valid_token_sync)
        try:
            return await action(token)  # type: ignore[misc]
        except TokenRejectedError:
            token = await asyncio.to_thread(self._valid_token_sync, force=True)
            return await action(token)  # type: ignore[misc]

    async def complete_chatgpt(self, messages: Iterable[Mapping[str, str]]) -> str:
        model = self.model_id(CODEX)
        batch = list(messages)
        return await self._call_chatgpt(lambda token: self.responses.complete(token, model, batch))

    # ---------------------------------------------------------------- login / logout
    async def login_api_key(self, provider: str | ProviderSpec, key: str) -> LoginResult:
        """Valida a chave com o provedor e só então grava. Passa a ser o provedor ativo."""
        spec = provider if isinstance(provider, ProviderSpec) else get_provider(provider)
        key = key.strip()
        check = await self.api.check(spec, key)
        if not check.ok:
            return LoginResult(False, check.message, spec)

        self.store.set(spec.env_var, key)
        self.environ[spec.env_var] = key  # vale já, sem reiniciar

        state = self.state.read()
        saved = state["models"].get(spec.id)
        if not (isinstance(saved, str) and saved and (not check.models or saved in check.models)):
            state["models"][spec.id] = choose_default_model(spec, check.models)
        state["active"] = spec.id
        state["modes"][spec.id] = "api_key"
        self.state.write(state)
        message = check.message or f"{spec.label} conectado."
        return LoginResult(True, message, spec, state["models"][spec.id], self.store.warning)

    async def login_chatgpt(self, on_url: UrlHandler) -> LoginResult:
        """Entra com o plano ChatGPT pelo navegador (só o Codex). Nada é gravado se falhar."""
        spec = CODEX
        try:
            tokens = await self.chatgpt.login(on_url)
        except ChatGPTAuthError as exc:
            hint = " Você pode usar uma chave de API: /login codex key." if exc.offer_api_key else ""
            return LoginResult(False, f"{exc}{hint}", spec)
        models = await self._models_after_login(tokens)
        state = self.state.read()
        saved = state["models"].get(spec.id)
        if not (isinstance(saved, str) and saved and (not models or saved in models)):
            state["models"][spec.id] = choose_default_model(spec, models)
        state["active"] = spec.id
        state["modes"][spec.id] = "chatgpt"
        self.state.write(state)
        return LoginResult(
            True, "Codex conectado pelo plano ChatGPT.", spec, state["models"][spec.id], self.chatgpt_store.warning
        )

    async def _models_after_login(self, tokens: Tokens) -> list[str]:
        try:
            return await self.responses.list_models(tokens.access_token)
        except ChatGPTAuthError:
            return []  # login vale mesmo sem a lista; cai no modelo padrão

    def logout(self, provider: str | None = None) -> list[LogoutOutcome]:
        """Remove a credencial de um provedor (ou de todos, com None)."""
        specs = PROVIDERS if provider is None else (get_provider(provider),)
        outcomes = []
        for spec in specs:
            stored = self.store.get(spec.env_var)
            removed = self.store.remove(spec.env_var)
            if removed and self.environ.get(spec.env_var) == stored:
                del self.environ[spec.env_var]
            if spec.id == CODEX.id:
                removed = self.chatgpt_store.clear_tokens() or removed
            outcomes.append(LogoutOutcome(spec, removed, bool(self.environ.get(spec.env_var))))
        state = self.state.read()
        for outcome in outcomes:
            state["modes"].pop(outcome.provider.id, None)
        if state["active"] in {o.provider.id for o in outcomes}:
            state["active"] = None
        self.state.write(state)
        return outcomes
