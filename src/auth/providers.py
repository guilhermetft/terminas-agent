"""Provedores de IA suportados e como falar com cada um."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    env_var: str  # variável lida pelo LiteLLM e gravada no .env
    litellm_prefix: str
    default_model: str  # modelo preferido (id puro, sem prefixo do LiteLLM)
    model_preferences: tuple[str, ...]  # trechos de id, do mais ao menos desejado, p/ escolher entre os da API
    key_url: str  # onde o usuário cria a chave
    api_base: str
    aliases: tuple[str, ...] = ()
    min_key_length: int = 20
    # Por que nao ha login por link (OAuth) neste provedor; mostrado pelo /login (ver docs/7).
    oauth_note: str = ""

    def litellm_model(self, model_id: str) -> str:
        return f"{self.litellm_prefix}/{model_id}"


CLAUDE = ProviderSpec(
    id="claude",
    label="Claude (Anthropic)",
    env_var="ANTHROPIC_API_KEY",
    litellm_prefix="anthropic",
    default_model="claude-sonnet-5-5",
    model_preferences=("sonnet",),
    key_url="https://platform.claude.com/ (Claude Console > API keys)",
    api_base="https://api.anthropic.com",
    aliases=("anthropic",),
    oauth_note=(
        "A Anthropic não permite login de assinatura (Claude.ai) em aplicativos de terceiros; "
        "por isso o Termina usa chave de API do Claude Console."
    ),
)
CODEX = ProviderSpec(
    id="codex",
    label="Codex (OpenAI)",
    env_var="OPENAI_API_KEY",
    litellm_prefix="openai",
    default_model="gpt-5",
    model_preferences=("gpt-5", "gpt-4.1", "gpt-4o"),
    key_url="https://platform.openai.com/api-keys",
    api_base="https://api.openai.com",
    aliases=("openai", "gpt"),
    oauth_note=(
        "A OpenAI não documenta o uso do login ChatGPT por aplicativos de terceiros; "
        "por isso o Termina usa chave de API."
    ),
)
GEMINI = ProviderSpec(
    id="gemini",
    label="Gemini (Google)",
    env_var="GEMINI_API_KEY",
    litellm_prefix="gemini",
    default_model="gemini-2.5-flash",
    model_preferences=("gemini-2.5-flash", "flash", "gemini"),
    key_url="https://aistudio.google.com/apikey",
    api_base="https://generativelanguage.googleapis.com",
    aliases=("google",),
    oauth_note=(
        "O login do Google exige um cliente OAuth verificado do Termina, que ainda não existe; "
        "por isso o Termina usa chave de API."
    ),
)

PROVIDERS: tuple[ProviderSpec, ...] = (CLAUDE, CODEX, GEMINI)


class UnknownProviderError(ValueError):
    pass


def get_provider(name: str) -> ProviderSpec:
    """Resolve 'claude', 'anthropic', 'codex', 'openai', 'gemini', 'google' (sem diferenciar maiúsculas)."""
    wanted = name.strip().lower()
    for spec in PROVIDERS:
        if wanted == spec.id or wanted in spec.aliases:
            return spec
    options = ", ".join(spec.id for spec in PROVIDERS)
    raise UnknownProviderError(f"Provedor desconhecido: '{name}'. Opções: {options}.")


def provider_for_model(litellm_model: str) -> ProviderSpec | None:
    """'anthropic/claude-x' -> CLAUDE. None se o prefixo não for de um provedor conhecido."""
    prefix = litellm_model.split("/", 1)[0].lower()
    return next((s for s in PROVIDERS if s.litellm_prefix == prefix), None)


def choose_default_model(spec: ProviderSpec, available: list[str]) -> str:
    """Escolhe o modelo padrão entre os que a API do provedor listou.

    Usa a ordem de `model_preferences`; entre empates, o id maior (costuma ser o mais novo).
    Sem lista (ou sem correspondência), cai em `spec.default_model`."""
    if spec.default_model in available:
        return spec.default_model
    for fragment in spec.model_preferences:
        matches = sorted(m for m in available if fragment in m)
        if matches:
            return matches[-1]
    return spec.default_model
