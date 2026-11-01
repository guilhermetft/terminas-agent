"""Autenticação dos provedores de IA (Claude, Codex/OpenAI e Gemini)."""

from src.auth.client import authenticated_llm
from src.auth.manager import (
    AuthManager,
    Credential,
    LoginResult,
    LogoutOutcome,
    NotAuthenticatedError,
    ProviderStatus,
)
from src.auth.providers import (
    CLAUDE,
    CODEX,
    GEMINI,
    PROVIDERS,
    ProviderSpec,
    UnknownProviderError,
    get_provider,
)
from src.auth.secrets import detect_secret, mask, redact
from src.auth.store import CredentialStore
from src.auth.validation import KeyCheck, ProviderApi

__all__ = [
    "CLAUDE",
    "CODEX",
    "GEMINI",
    "PROVIDERS",
    "AuthManager",
    "Credential",
    "CredentialStore",
    "KeyCheck",
    "LoginResult",
    "LogoutOutcome",
    "NotAuthenticatedError",
    "ProviderApi",
    "ProviderSpec",
    "ProviderStatus",
    "UnknownProviderError",
    "authenticated_llm",
    "detect_secret",
    "get_provider",
    "mask",
    "redact",
]
