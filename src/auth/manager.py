"""AuthManager: quem está logado, qual provedor/modelo está ativo e como entrar e sair."""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.auth.providers import (
    PROVIDERS,
    ProviderSpec,
    choose_default_model,
    get_provider,
    provider_for_model,
)
from src.auth.secrets import mask
from src.auth.store import AuthState, CredentialStore
from src.auth.validation import ProviderApi

Source = Literal["store", "env"]


class NotAuthenticatedError(RuntimeError):
    """Nenhum provedor de IA está conectado."""


@dataclass(frozen=True)
class Credential:
    provider: ProviderSpec
    value: str
    source: Source  # "store" = ~/.terminas/.env (via /login); "env" = variável de ambiente externa


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
    removed: bool  # havia credencial gravada pelo /login
    still_in_environment: bool  # a variável de ambiente externa continua definida


class AuthManager:
    def __init__(
        self,
        auth_dir: str | Path,
        *,
        environ: MutableMapping[str, str] | None = None,
        api: ProviderApi | None = None,
        preferred_model: str | None = None,
    ) -> None:
        self.directory = Path(auth_dir).expanduser()
        self.store = CredentialStore(self.directory / ".env")
        self.state = AuthState(self.directory / "auth.json")
        self.environ = os.environ if environ is None else environ
        self.api = api or ProviderApi()
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

    def credential(self, spec: ProviderSpec) -> Credential | None:
        stored = self.store.get(spec.env_var)
        from_env = self.environ.get(spec.env_var)
        if from_env:
            return Credential(spec, from_env, "store" if from_env == stored else "env")
        if stored:
            return Credential(spec, stored, "store")
        return None

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
                    masked_key=mask(cred.value) if cred else None,
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
        return (await self.api.check(spec, cred.value)).models

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
        self.state.write(state)
        message = check.message or f"{spec.label} conectado."
        return LoginResult(True, message, spec, state["models"][spec.id], self.store.warning)

    def logout(self, provider: str | None = None) -> list[LogoutOutcome]:
        """Remove a credencial de um provedor (ou de todos, com None)."""
        specs = PROVIDERS if provider is None else (get_provider(provider),)
        outcomes = []
        for spec in specs:
            stored = self.store.get(spec.env_var)
            removed = self.store.remove(spec.env_var)
            if removed and self.environ.get(spec.env_var) == stored:
                del self.environ[spec.env_var]
            outcomes.append(LogoutOutcome(spec, removed, bool(self.environ.get(spec.env_var))))
        state = self.state.read()
        if state["active"] in {o.provider.id for o in outcomes}:
            state["active"] = None
            self.state.write(state)
        return outcomes
