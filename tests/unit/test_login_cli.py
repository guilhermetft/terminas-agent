"""Terminal: /login, /logout, /model e o bloqueio do agente sem login (issue #20)."""

import json

import pytest

from src.auth import ProviderApi
from src.ui.cli import ALLOWED_WITHOUT_LOGIN, COMMANDS
from src.ui.services import stop_services
from tests.fake_providers import VALID_KEYS, base_urls
from tests.helpers import (
    FakeLLM,
    ForbiddenLLM,
    build_app,
    make_auth,
    output,
    plan_json,
)

CLAUDE_KEY, CODEX_KEY, GEMINI_KEY = VALID_KEYS["claude"], VALID_KEYS["codex"], VALID_KEYS["gemini"]
ALL_KEYS = list(VALID_KEYS.values())


@pytest.fixture
async def make_app(tmp_path, provider_server):
    created = []

    async def factory(*lines, logged_in=False, secrets=(), env=None, api=None, **kwargs):
        auth = make_auth(
            tmp_path, logged_in=logged_in, environ=env, api=api or ProviderApi(base_urls(provider_server))
        )
        app, services, reader = await build_app(tmp_path, *lines, auth=auth, secrets=secrets, **kwargs)
        created.append(services)
        return app, services, reader

    yield factory
    for services in created:
        await stop_services(services)


def text_of(app) -> str:
    return " ".join(output(app.console).split())


def assert_no_key_anywhere(app, services, *keys):
    """Nenhuma chave em tela, memória, auditoria nem arquivos de dados do Termina."""
    haystacks = [output(app.console), json.dumps(services.memory.get_context(), ensure_ascii=False)]
    audit = services.audit.path
    if audit.exists():
        haystacks.append(audit.read_text(encoding="utf-8"))
    for key in keys:
        for blob in haystacks:
            assert key not in blob
    for db in services.config.data_dir.glob("*.sqlite"):
        raw = db.read_bytes()
        assert all(key.encode() not in raw for key in keys)


# ================================================================ bloqueio sem login
BLOCKED = [
    "quero um relatorio", "/run algo", "/plan algo", "/memory", "/behavior", "/skill quick-notes",
    "/rag status", "/model", "/auto on", "/agents", "/cancel", "/clear", "/approve", "/reject x",
]  # fmt: skip


async def test_agent_is_blocked_without_login(make_app):
    app, services, _ = await make_app(llm=ForbiddenLLM())
    for line in BLOCKED:
        assert await app.handle_line(line) is True
    text = text_of(app)
    assert text.count("O agente está bloqueado") == len(BLOCKED)
    assert "Use /login (Claude, Codex ou Gemini)" in text
    assert services.memory.get_context() == []  # nada foi gravado na memória
    assert services.memory.list_episodes() == [] and ForbiddenLLM.calls == []


async def test_only_help_and_login_commands_work_without_login(make_app):
    app, services, _ = await make_app()
    assert ALLOWED_WITHOUT_LOGIN == {"help", "login", "logout", "status", "skills", "exit", "quit"}
    for spec in COMMANDS:  # todo comando fora da lista está bloqueado
        if spec.name not in ALLOWED_WITHOUT_LOGIN:
            assert await app.handle_line(f"/{spec.name}") is True
    assert text_of(app).count("O agente está bloqueado") == len(COMMANDS) - 6  # 6 liberados (exit/quit contam 1)

    app.console.file.truncate(0)
    app.console.file.seek(0)
    for line in ("/help", "/status", "/skills", "/logout", "/login"):
        assert await app.handle_line(line) is True
    text = text_of(app)
    assert "Comandos" in text and "Provedores de IA" in text and "desconectado" in text
    assert "quick-notes" in text and "Nenhum provedor conectado." in text
    assert "O agente está bloqueado" not in text
    assert await app.handle_line("/exit") is False and await app.handle_line("/quit") is False


async def test_unknown_commands_are_reported_as_unknown_not_as_blocked(make_app):
    app, *_ = await make_app()
    await app.handle_line("/stauts")
    text = text_of(app)
    assert "Comando desconhecido: /stauts" in text and "O agente está bloqueado" not in text


async def test_blocked_message_lists_what_still_works(make_app):
    app, *_ = await make_app()
    await app.handle_line("oi")
    assert "Sem login, só funcionam: /exit, /help, /login, /logout, /skills, /status." in text_of(app)


async def test_login_notice_on_startup_only_when_not_logged_in(make_app):
    anon, *_ = await make_app("/exit")
    await anon.run()
    assert "Nenhum provedor de IA conectado. Use /login" in text_of(anon)
    assert "Model: sem login" in text_of(anon)
    logged, *_ = await make_app("/exit", logged_in=True)
    await logged.run()
    assert "Nenhum provedor de IA conectado" not in text_of(logged)
    assert "Model: anthropic/claude-sonnet-5-5" in text_of(logged)


# =================================================================== chave colada
@pytest.mark.parametrize("logged_in", [False, True])
@pytest.mark.parametrize("key", ALL_KEYS, ids=["claude", "codex", "gemini"])
async def test_a_key_pasted_as_normal_text_is_refused_and_recorded_nowhere(make_app, key, logged_in):
    app, services, _ = await make_app(logged_in=logged_in)
    for line in (key, f"/skill quick-notes {{\"text\": \"minha chave {key}\"}}", f"use esta chave: {key}"):
        assert await app.handle_line(line) is True
    text = text_of(app)
    assert text.count("Isso parece uma chave de API. Não guardei nada.") == 3
    assert "use /login" in text
    assert services.memory.get_context() == []
    assert services.memory.list_long_term("notes") == {}
    assert_no_key_anywhere(app, services, key)


async def test_secrets_in_error_messages_are_redacted_on_screen(make_app):
    app, services, _ = await make_app(logged_in=True)

    async def leaky(cmd):
        raise RuntimeError(f"a API respondeu: chave {CLAUDE_KEY} invalida")

    app._handlers["memory"] = leaky
    await app.handle_line("/memory")
    text = text_of(app)
    assert "[chave anthropic oculta]" in text and CLAUDE_KEY not in text


# ======================================================================= /login
@pytest.mark.parametrize(
    ("name", "label", "model"),
    [("claude", "Claude (Anthropic)", "claude-sonnet-5-5"), ("codex", "Codex (OpenAI)", "gpt-5"),
     ("openai", "Codex (OpenAI)", "gpt-5"), ("gemini", "Gemini (Google)", "gemini-2.5-flash")],
)  # fmt: skip
async def test_login_with_an_api_key_for_each_provider(make_app, tmp_path, name, label, model):
    key = {"claude": CLAUDE_KEY, "codex": CODEX_KEY, "openai": CODEX_KEY, "gemini": GEMINI_KEY}[name]
    app, services, reader = await make_app(secrets=(key,))
    await app.handle_line(f"/login {name} key" if name in {"codex", "openai"} else f"/login {name}")
    text = text_of(app)
    assert f"{label} conectado. Modelo ativo: {model}." in text
    assert ("Login pelo navegador não disponível:" in text) is (name not in {"codex", "openai"})
    assert "Crie uma chave de API em:" in text and "não aparece na tela" in text
    assert reader.secret_prompts == ["Chave de API: "] and reader.prompts == []
    assert services.auth.is_authenticated and str(services.auth.store.path) in text
    assert services.auth.store.read()  # gravou no .env do usuário
    assert_no_key_anywhere(app, services, key)


async def test_login_lifts_the_block_immediately_without_restarting(make_app):
    app, services, _ = await make_app(
        "x", llm=FakeLLM(plan_json(("t1", None))), secrets=(CLAUDE_KEY,)
    )
    await app.handle_line("/plan escrever algo")
    assert "O agente está bloqueado" in text_of(app) and "Plano de Execução" not in text_of(app)
    await app.handle_line("/login claude")
    await app.handle_line("/plan escrever algo")
    assert "Plano de Execução (DAG)" in text_of(app)  # o planner passou a funcionar
    assert services.auth.active_model() == "anthropic/claude-sonnet-5-5"
    app.console.print(app._header())
    assert "Model: anthropic/claude-sonnet-5-5" in text_of(app)


async def test_login_chooser_by_number(make_app):
    app, services, reader = await make_app("2", "2", secrets=(CODEX_KEY,))
    await app.handle_line("/login")
    assert "Qual provedor conectar?" in reader.prompts[0][0] and reader.prompts[0][1] is False  # fora do histórico
    assert "Codex (OpenAI) conectado" in text_of(app) and services.auth.active_model() == "openai/gpt-5"


async def test_login_chooser_by_name_ignores_case(make_app):
    app, *_ = await make_app("Gemini", secrets=(GEMINI_KEY,))
    await app.handle_line("/login")
    assert "Gemini (Google) conectado" in text_of(app)


@pytest.mark.parametrize("answer", ["", "   ", None])
async def test_login_chooser_can_be_cancelled(make_app, answer):
    app, services, reader = await make_app(*([answer] if answer is not None else []))
    await app.handle_line("/login")
    assert "conectado" not in text_of(app).replace("desconectado", "") and reader.secret_prompts == []
    assert not services.auth.is_authenticated


@pytest.mark.parametrize(("answer", "fragment"), [("9", "Número fora da lista"), ("0", "Número fora da lista"), ("llama", "Provedor desconhecido")])
async def test_login_chooser_rejects_bad_choices(make_app, answer, fragment):
    app, services, reader = await make_app(answer)
    await app.handle_line("/login")
    assert fragment in text_of(app) and reader.secret_prompts == [] and not services.auth.is_authenticated


async def test_login_with_unknown_provider_argument(make_app):
    app, _, reader = await make_app()
    await app.handle_line("/login llama")
    assert "Provedor desconhecido: 'llama'. Opções: claude, codex, gemini." in text_of(app)
    assert reader.secret_prompts == []


@pytest.mark.parametrize("secret", [None, "", "   "])
async def test_login_can_be_cancelled_at_the_key_prompt(make_app, secret):
    app, services, _ = await make_app(secrets=(secret,))
    await app.handle_line("/login claude")
    assert "Login cancelado." in text_of(app) and not services.auth.is_authenticated
    assert services.auth.store.read() == {}


async def test_login_with_a_wrong_key_saves_nothing(make_app):
    app, services, _ = await make_app(secrets=("chave-errada-" + "x" * 30,))
    await app.handle_line("/login claude")
    text = text_of(app)
    assert "Claude (Anthropic) recusou a chave (HTTP 401)." in text and "Nada foi salvo." in text
    assert services.auth.store.read() == {} and not services.auth.is_authenticated
    assert "chave-errada" not in text  # nem a chave errada é repetida na tela


async def test_login_with_incomplete_key_and_with_provider_down(make_app):
    app, services, _ = await make_app(secrets=("curta",))
    await app.handle_line("/login gemini")
    assert "A chave parece incompleta" in text_of(app) and not services.auth.is_authenticated

    def offline(url, headers=None, timeout=0):
        from src.utils import http

        raise http.HttpError("sem rede")

    down, services, _ = await make_app(secrets=(CLAUDE_KEY,), api=ProviderApi(getter=offline))
    await down.handle_line("/login claude")
    assert "Não consegui falar com Claude (Anthropic): sem rede" in text_of(down)
    assert not services.auth.is_authenticated and services.auth.store.read() == {}


async def test_login_shows_a_warning_when_permissions_could_not_be_restricted(make_app, monkeypatch):
    monkeypatch.setattr("src.auth.store.restrict_to_owner", lambda *a, **k: "icacls falhou")
    app, _, _ = await make_app(secrets=(CLAUDE_KEY,))
    await app.handle_line("/login claude")
    assert "Atenção: icacls falhou" in text_of(app) and "Claude (Anthropic) conectado" in text_of(app)


async def test_login_survives_a_new_session(make_app, tmp_path, provider_server):
    app, services, _ = await make_app(secrets=(GEMINI_KEY,))
    await app.handle_line("/login gemini")
    from src.auth import AuthManager

    fresh_env: dict = {}
    reopened = AuthManager(services.auth.directory, environ=fresh_env)
    assert reopened.is_authenticated and fresh_env == {}  # lê o .env direto; o ambiente ainda está vazio
    reopened.load_into_environment()
    assert fresh_env["GEMINI_API_KEY"] == GEMINI_KEY  # agora o LiteLLM enxerga a chave
    assert reopened.active_model() == "gemini/gemini-2.5-flash"


# ======================================================================= /logout
async def logged_in_app(make_app, *keys_by_provider):
    providers = [p for p, _ in keys_by_provider]
    app, services, reader = await make_app(secrets=tuple(k for _, k in keys_by_provider))
    for provider in providers:
        await app.handle_line(f"/login {provider} key" if provider == "codex" else f"/login {provider}")
    return app, services, reader


async def test_logout_removes_the_key_and_blocks_the_agent_again(make_app):
    app, services, _ = await logged_in_app(make_app, ("claude", CLAUDE_KEY))
    await app.handle_line("/logout claude")
    text = text_of(app)
    assert "Claude (Anthropic) desconectado" in text and "o agente fica bloqueado até o próximo /login" in text
    assert services.auth.store.read() == {} and "ANTHROPIC_API_KEY" not in services.auth.environ
    assert not services.auth.is_authenticated
    await app.handle_line("oi")
    assert text_of(app).count("O agente está bloqueado") == 1
    assert_no_key_anywhere(app, services, CLAUDE_KEY)


async def test_logout_one_provider_keeps_the_others(make_app):
    app, services, _ = await logged_in_app(make_app, ("claude", CLAUDE_KEY), ("gemini", GEMINI_KEY))
    await app.handle_line("/logout gemini")
    assert services.auth.is_authenticated and services.auth.active_model() == "anthropic/claude-sonnet-5-5"
    assert "bloqueado" not in text_of(app).split("desconectado")[-1]


async def test_logout_all_and_chooser(make_app):
    app, services, _ = await logged_in_app(make_app, ("claude", CLAUDE_KEY), ("codex", CODEX_KEY))
    await app.handle_line("/logout all")
    assert not services.auth.is_authenticated and text_of(app).count("desconectado (credenciais removidas") == 2

    chooser, services, reader = await logged_in_app(make_app, ("claude", CLAUDE_KEY), ("codex", CODEX_KEY))
    reader.lines.extend(["1"])
    await chooser.handle_line("/logout")
    assert "Sair de qual provedor?" in reader.prompts[-1][0] and "ou all" in reader.prompts[-1][0]
    assert [s.logged_in for s in services.auth.status()] == [False, True, False]
    reader.lines.extend(["all"])
    await chooser.handle_line("/logout")
    assert not services.auth.is_authenticated
    reader.lines.extend([""])
    await chooser.handle_line("/logout")  # nada conectado: nem pergunta
    assert text_of(chooser).count("Nenhum provedor conectado.") >= 1


async def test_logout_chooser_cancel_and_unknown_provider(make_app):
    app, services, reader = await logged_in_app(make_app, ("claude", CLAUDE_KEY))
    reader.lines.extend([""])
    await app.handle_line("/logout")
    assert services.auth.is_authenticated  # Enter cancela
    await app.handle_line("/logout llama")
    assert "Provedor desconhecido" in text_of(app) and services.auth.is_authenticated


async def test_logout_cannot_remove_a_key_that_comes_from_the_environment(make_app):
    app, services, _ = await make_app(env={"OPENAI_API_KEY": CODEX_KEY})
    await app.handle_line("/logout codex")
    text = text_of(app)
    assert "A variável OPENAI_API_KEY continua definida no seu ambiente" in text
    assert services.auth.is_authenticated and "desconectado (credenciais removidas" not in text


async def test_logout_of_a_provider_that_was_not_connected(make_app):
    app, *_ = await logged_in_app(make_app, ("claude", CLAUDE_KEY))
    await app.handle_line("/logout gemini")
    assert "Gemini (Google) não estava conectado." in text_of(app)


# ======================================================================== /model
async def test_model_overview_switch_and_set(make_app):
    app, services, _ = await logged_in_app(make_app, ("claude", CLAUDE_KEY), ("gemini", GEMINI_KEY))
    await app.handle_line("/model")
    text = text_of(app)
    assert "Provedores de IA" in text and "Modelo ativo: gemini/gemini-2.5-flash" in text
    assert "Trocar: /model <provedor> [modelo]." in text

    await app.handle_line("/model claude")
    assert "Provedor ativo: Claude (Anthropic) (modelo claude-sonnet-5-5)." in text_of(app)
    assert services.auth.active_model() == "anthropic/claude-sonnet-5-5"

    await app.handle_line("/model claude claude-opus-5-5")
    assert "Agora usando Claude (Anthropic), modelo claude-opus-5-5." in text_of(app)
    assert services.auth.active_model() == "anthropic/claude-opus-5-5"
    assert services.auth.model_id(services.auth.status()[2].spec) == "gemini-2.5-flash"  # outro provedor intacto


async def test_model_list_marks_the_current_model(make_app):
    app, *_ = await logged_in_app(make_app, ("gemini", GEMINI_KEY))
    await app.handle_line("/model gemini list")
    text = text_of(app)
    assert "* gemini-2.5-flash" in text and "gemini-2.5-pro" in text and "embedding-001" not in text


async def test_model_rejects_unknown_models_with_suggestions(make_app):
    app, services, _ = await logged_in_app(make_app, ("claude", CLAUDE_KEY))
    await app.handle_line("/model claude claude-sonet-5-5")
    text = text_of(app)
    assert "Modelo 'claude-sonet-5-5' não existe em Claude (Anthropic)." in text
    assert "Parecidos: claude-sonnet-5-5" in text
    assert services.auth.model_id(services.auth.status()[0].spec) == "claude-sonnet-5-5"


async def test_model_errors_for_unknown_or_disconnected_providers(make_app):
    app, *_ = await logged_in_app(make_app, ("claude", CLAUDE_KEY))
    await app.handle_line("/model llama")
    await app.handle_line("/model gemini")
    await app.handle_line("/model gemini gemini-2.5-pro")
    text = text_of(app)
    assert "Provedor desconhecido: 'llama'" in text
    assert text.count("Gemini (Google) não está conectado. Use /login gemini.") == 2


async def test_model_works_offline_by_accepting_the_name_without_validation(make_app):
    def offline(url, headers=None, timeout=0):
        from src.utils import http

        raise http.HttpError("sem rede")

    app, services, _ = await make_app(env={"GEMINI_API_KEY": GEMINI_KEY}, api=ProviderApi(getter=offline))
    await app.handle_line("/model gemini list")
    await app.handle_line("/model gemini gemini-9-ultra")
    text = text_of(app)
    assert "Não consegui listar os modelos agora (sem rede?)." in text
    assert "Agora usando Gemini (Google), modelo gemini-9-ultra." in text
    assert services.auth.active_model() == "gemini/gemini-9-ultra"


# ===================================================== status, ajuda e autocomplete
async def test_status_shows_providers_with_masked_keys_only(make_app):
    app, services, _ = await logged_in_app(make_app, ("claude", CLAUDE_KEY))
    await app.handle_line("/status")
    text = text_of(app)
    assert "Provedores de IA" in text and "ativo" in text and "(/login)" in text
    assert "sk-a…AAAA" in text and CLAUDE_KEY not in text
    env_app, *_ = await make_app(env={"GEMINI_API_KEY": GEMINI_KEY})
    await env_app.handle_line("/status")
    assert "(variável de ambiente)" in text_of(env_app)


async def test_help_documents_the_new_commands(make_app):
    app, *_ = await make_app()
    await app.handle_line("/help")
    text = "".join(output(app.console).replace("│", " ").split())  # sem bordas nem quebras de linha
    for spec in COMMANDS:
        if spec.name in {"login", "logout", "model"}:
            assert "".join(spec.summary.split()) in text, spec.name


async def test_autocomplete_for_the_new_commands(make_app):
    app, *_ = await make_app()
    assert app.suggestions("/login ") == ["claude", "codex", "gemini"]
    assert app.suggestions("/logout c") == ["claude", "codex"]
    assert app.suggestions("/logout a") == ["all"]
    assert app.suggestions("/model g") == ["gemini"]
    assert "/login" in app.suggestions("/lo") and "/logout" in app.suggestions("/lo")


async def test_provider_status_header_and_table_never_print_full_keys(make_app):
    app, services, _ = await logged_in_app(make_app, ("claude", CLAUDE_KEY), ("codex", CODEX_KEY), ("gemini", GEMINI_KEY))
    for line in ("/status", "/model", "/login", "/logout"):
        await app.handle_line(line)
    assert_no_key_anywhere(app, services, *ALL_KEYS)


@pytest.mark.parametrize(
    ("name", "fragment"),
    [("claude", "Anthropic não permite login de assinatura"),
     ("gemini", "cliente OAuth verificado do Termina")],
)  # fmt: skip
async def test_login_explains_why_there_is_no_browser_login(make_app, name, fragment):
    app, *_ = await make_app(secrets=(None,))
    await app.handle_line(f"/login {name}")
    assert fragment in text_of(app)
