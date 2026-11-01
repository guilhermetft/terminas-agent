"""Autenticação: segredos, provedores, armazenamento, validação e AuthManager (issue #20)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.auth import (
    CLAUDE,
    CODEX,
    GEMINI,
    PROVIDERS,
    AuthManager,
    CredentialStore,
    NotAuthenticatedError,
    ProviderApi,
    UnknownProviderError,
    authenticated_llm,
    detect_secret,
    get_provider,
    mask,
    redact,
)
from src.auth.providers import choose_default_model, provider_for_model
from src.auth.store import AuthState, parse_env, restrict_to_owner
from src.utils import http
from tests.fake_providers import (
    BAD_JSON_KEY,
    RATE_LIMITED_KEY,
    SERVER_ERROR_KEY,
    VALID_KEYS,
    base_urls,
)

ANTHROPIC = VALID_KEYS["claude"]
OPENAI = VALID_KEYS["codex"]
GOOGLE = VALID_KEYS["gemini"]


# ====================================================================== segredos
@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (f"minha chave e {ANTHROPIC} ok", "anthropic"),
        (OPENAI, "openai"),
        ("sk-" + "z" * 48, "openai"),
        (GOOGLE, "gemini"),
        ("ghp_" + "a1" * 20, "github"),
    ],
)
def test_detect_secret_recognizes_provider_keys(text, kind):
    assert detect_secret(text) == kind


@pytest.mark.parametrize(
    "text",
    [
        "", "ola, tudo bem?", "/plan escrever testes", "sk-learn", "sk-curta",
        "task-management-system-with-a-very-long-name-here", "ask-me-anything-about-the-whole-system-prompt",
        "AIza", "AIzaCurta", "https://example.com/sk-ant", "/skill quick-notes {\"text\": \"ideia\"}",
    ],
)  # fmt: skip
def test_detect_secret_has_no_false_positives_on_normal_text(text):
    assert detect_secret(text) is None


def test_redact_hides_every_key_and_keeps_the_rest():
    text = f"a={ANTHROPIC} b={OPENAI} c={GOOGLE} fim"
    out = redact(text)
    assert out == "a=[chave anthropic oculta] b=[chave openai oculta] c=[chave gemini oculta] fim"
    assert ANTHROPIC not in out and redact("sem segredo") == "sem segredo"


def test_anthropic_keys_are_not_mislabeled_as_openai():
    assert detect_secret(ANTHROPIC) == "anthropic" and redact(ANTHROPIC) == "[chave anthropic oculta]"


def test_mask_shows_only_the_edges():
    assert mask(ANTHROPIC) == "sk-a…AAAA"
    assert mask("curta") == "****" and mask("  123456789012  ") == "****"
    assert ANTHROPIC not in mask(ANTHROPIC)


# ==================================================================== provedores
def test_providers_are_claude_codex_gemini_with_distinct_env_vars():
    assert [p.id for p in PROVIDERS] == ["claude", "codex", "gemini"]
    assert {p.env_var for p in PROVIDERS} == {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"}
    assert CLAUDE.litellm_model("x") == "anthropic/x" and CODEX.litellm_model("x") == "openai/x"
    assert GEMINI.litellm_model("x") == "gemini/x"


@pytest.mark.parametrize(
    ("name", "expected"),
    [("claude", CLAUDE), ("Anthropic", CLAUDE), ("CODEX", CODEX), ("openai", CODEX), ("gpt", CODEX),
     ("gemini", GEMINI), ("google", GEMINI), ("  gemini ", GEMINI)],
)  # fmt: skip
def test_get_provider_accepts_aliases(name, expected):
    assert get_provider(name) is expected


def test_get_provider_unknown_lists_the_options():
    with pytest.raises(UnknownProviderError, match="claude, codex, gemini"):
        get_provider("llama")


def test_provider_for_model_uses_the_litellm_prefix():
    assert provider_for_model("anthropic/claude-sonnet-5-5") is CLAUDE
    assert provider_for_model("OpenAI/gpt-5") is CODEX
    assert provider_for_model("gemini/gemini-2.5-flash") is GEMINI
    assert provider_for_model("ollama/llama3") is None and provider_for_model("semprefixo") is None


def test_choose_default_model():
    assert choose_default_model(CLAUDE, ["claude-haiku-5-5", "claude-sonnet-5-5"]) == "claude-sonnet-5-5"
    assert choose_default_model(CLAUDE, ["claude-haiku-5-5", "claude-sonnet-6", "claude-sonnet-5-9"]) == "claude-sonnet-6"
    assert choose_default_model(CODEX, ["gpt-4o", "gpt-5-mini", "gpt-5.1"]) == "gpt-5.1"
    assert choose_default_model(GEMINI, ["gemini-2.5-pro", "gemini-3-flash"]) == "gemini-3-flash"
    assert choose_default_model(GEMINI, ["modelo-estranho"]) == "gemini-2.5-flash"  # sem correspondência
    assert choose_default_model(CLAUDE, []) == "claude-sonnet-5-5"  # sem lista (offline)


# ===================================================================== armazenamento
def test_parse_env_handles_quotes_comments_and_export():
    text = '# comentario\n\nA=1\nB="dois"\nC=\'tres\'\nexport D = quatro\nINVALIDA\n=semnome\nE=x=y\n'
    assert parse_env(text) == {"A": "1", "B": "dois", "C": "tres", "D": "quatro", "E": "x=y"}


def test_store_roundtrip_preserves_other_lines_and_never_duplicates(tmp_path):
    store = CredentialStore(tmp_path / "home" / ".env")
    assert store.read() == {} and store.get("X") is None and store.remove("X") is False
    store.set("ANTHROPIC_API_KEY", ANTHROPIC)
    store.set("OPENAI_API_KEY", OPENAI)
    store.set("ANTHROPIC_API_KEY", "nova-chave-" + "z" * 20)  # substitui, sem duplicar
    text = store.path.read_text(encoding="utf-8")
    assert text.startswith("# Credenciais do Termina's Agent") and text.count("ANTHROPIC_API_KEY") == 1
    assert store.read() == {"ANTHROPIC_API_KEY": "nova-chave-" + "z" * 20, "OPENAI_API_KEY": OPENAI}

    store.path.write_text(text + "# minha nota\nOUTRA=valor\n", encoding="utf-8")
    store.set("GEMINI_API_KEY", GOOGLE)
    assert store.remove("OPENAI_API_KEY") is True and store.remove("OPENAI_API_KEY") is False
    final = store.path.read_text(encoding="utf-8")
    assert "# minha nota" in final and "OUTRA=valor" in final and "OPENAI_API_KEY" not in final
    assert store.read()["GEMINI_API_KEY"] == GOOGLE


def test_store_collapses_duplicate_entries_on_set(tmp_path):
    store = CredentialStore(tmp_path / ".env")
    store.path.write_text('K="a"\nK="b"\nOUTRA=1\n', encoding="utf-8")
    store.set("K", "novo")
    assert store.path.read_text(encoding="utf-8").count("K=") == 1 and store.read()["K"] == "novo"


@pytest.mark.parametrize(("name", "value"), [("COM ESPACO", "x"), ("OK", "linha1\nlinha2"), ("A-B", "x"), ("OK", "a\rb")])
def test_store_rejects_invalid_names_and_multiline_values(tmp_path, name, value):
    store = CredentialStore(tmp_path / ".env")
    with pytest.raises(ValueError):
        store.set(name, value)
    assert not store.path.exists()


def test_store_write_is_atomic_and_leaves_no_temp_files(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path / ".env")
    store.set("A_KEY", "um-valor-" + "a" * 20)
    before = store.path.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("disco cheio")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.set("A_KEY", "outro")
    monkeypatch.undo()
    assert store.path.read_text(encoding="utf-8") == before  # arquivo original intacto
    assert [p.name for p in tmp_path.iterdir()] == [".env"]  # sem sobras .tmp


@pytest.mark.real_permissions
@pytest.mark.skipif(os.name == "nt", reason="modos POSIX")
def test_store_files_are_private_on_posix(tmp_path):
    store = CredentialStore(tmp_path / "home" / ".env")
    store.set("A_KEY", "um-valor-" + "a" * 20)
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    assert store.warning is None


@pytest.mark.real_permissions
@pytest.mark.skipif(os.name != "nt", reason="ACL do Windows")
def test_store_file_acl_is_limited_to_the_current_user_on_windows(tmp_path):
    store = CredentialStore(tmp_path / "home" / ".env")
    store.set("A_KEY", "um-valor-" + "a" * 20)
    assert store.warning is None
    listing = subprocess.run(["icacls", str(store.path)], capture_output=True, text=True).stdout
    principals = [line.split(":(")[0].split()[-1].split("\\")[-1] for line in listing.splitlines() if ":(" in line]
    assert principals and all(p.lower() == os.environ["USERNAME"].lower() for p in principals), listing


def test_restrict_to_owner_never_raises_and_reports_problems(tmp_path, monkeypatch):
    target = tmp_path / "f"
    target.write_text("x", encoding="utf-8")
    if os.name == "nt":
        monkeypatch.setenv("USERNAME", "")
        assert "descobrir o usuário" in restrict_to_owner(target)
        monkeypatch.setenv("USERNAME", "alguem")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 5, "", "negado"))
        assert "icacls falhou" in restrict_to_owner(target)
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", 1)))
    else:
        monkeypatch.setattr(os, "chmod", lambda *a, **k: (_ for _ in ()).throw(PermissionError("nao pode")))
    assert "não consegui" in restrict_to_owner(target) or "icacls" in restrict_to_owner(target)
    monkeypatch.undo()
    assert restrict_to_owner(target) is None
    assert restrict_to_owner(tmp_path, directory=True) is None


def test_auth_state_is_tolerant_to_garbage(tmp_path):
    state = AuthState(tmp_path / "sub" / "auth.json")
    assert state.read() == {"active": None, "models": {}}
    state.write({"active": "claude", "models": {"claude": "m"}})
    assert state.read() == {"active": "claude", "models": {"claude": "m"}}
    for garbage in ("não é json", "[1, 2]", '{"active": 5, "models": "x"}'):
        state.path.write_text(garbage, encoding="utf-8")
        assert state.read() == {"active": None, "models": {}}


# ======================================================================== validação
@pytest.fixture
def api(provider_server):
    return ProviderApi(base_urls(provider_server))


@pytest.mark.parametrize(
    ("spec", "key", "expected"),
    [
        (CLAUDE, ANTHROPIC, ["claude-haiku-5-5", "claude-opus-5-5", "claude-sonnet-5-5"]),
        (CODEX, OPENAI, ["gpt-4.1", "gpt-4o", "gpt-5"]),  # sem embedding/whisper/dall-e/tts/realtime/codex
        (GEMINI, GOOGLE, ["gemini-2.5-flash", "gemini-2.5-pro"]),  # só generateContent, sem "models/"
    ],
)
def test_valid_keys_are_accepted_and_chat_models_listed(api, spec, key, expected):
    check = api.check_sync(spec, key)
    assert check.ok and check.verdict == "ok" and check.models == expected


@pytest.mark.parametrize("spec", PROVIDERS, ids=lambda s: s.id)
def test_wrong_key_is_rejected_for_every_provider(api, spec):
    check = api.check_sync(spec, "chave-errada-" + "x" * 30)
    assert not check.ok and check.verdict == "invalid" and "recusou a chave" in check.message


def test_keys_travel_in_headers_never_in_the_url(api, provider_server):
    for spec, key in ((CLAUDE, ANTHROPIC), (CODEX, OPENAI), (GEMINI, GOOGLE)):
        api.check_sync(spec, key)
    assert len(provider_server.requests) == 3
    for request in provider_server.requests:
        assert not any(key in request["path"] for key in VALID_KEYS.values())
    headers = [{k.lower(): v for k, v in r["headers"].items()} for r in provider_server.requests]
    assert headers[0]["x-api-key"] == ANTHROPIC and headers[0]["anthropic-version"] == "2023-06-01"
    assert headers[1]["authorization"] == f"Bearer {OPENAI}"
    assert headers[2]["x-goog-api-key"] == GOOGLE


def test_rate_limit_counts_as_valid_but_other_errors_do_not(api):
    limited = api.check_sync(CODEX, RATE_LIMITED_KEY)
    assert limited.ok and limited.verdict == "rate_limited" and limited.models == []
    broken = api.check_sync(CODEX, SERVER_ERROR_KEY)
    assert not broken.ok and broken.verdict == "unreachable" and "HTTP 500" in broken.message
    html = api.check_sync(CODEX, BAD_JSON_KEY)
    assert html.ok and html.models == []  # chave aceita; só não deu para listar modelos


def test_bad_format_is_caught_before_any_request(api, provider_server):
    for key in ("", "curta", "com espaco no meio " + "x" * 30, "  "):
        check = api.check_sync(CLAUDE, key)
        assert check.verdict == "bad_format" and not check.ok
    assert provider_server.requests == []


def test_unreachable_provider_is_reported():
    def offline(url, headers=None, timeout=0):
        raise http.HttpError("sem rota para o host")

    check = ProviderApi(getter=offline).check_sync(CLAUDE, ANTHROPIC)
    assert check.verdict == "unreachable" and "Não consegui falar com Claude" in check.message


async def test_async_check_runs_in_a_thread(api):
    assert (await api.check(GEMINI, GOOGLE)).ok


def test_http_get_returns_error_statuses_instead_of_raising(provider_server, monkeypatch):
    url = f"http://127.0.0.1:{provider_server.server_port}/v1/models"
    response = http.get(url, headers={"Authorization": "Bearer errada"})
    assert response.status == 401 and response.json() == {"error": "invalid key"}
    import urllib.error
    import urllib.request

    def offline(*args, **kwargs):
        raise urllib.error.URLError("sem rota")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    with pytest.raises(http.HttpError, match="sem rota"):
        http.get(url)


# ======================================================================== manager
@pytest.fixture
def auth(tmp_path, api):
    return AuthManager(tmp_path / "home", environ={}, api=api)


def test_fresh_install_is_not_authenticated(auth):
    assert not auth.is_authenticated and auth.active_provider() is None and auth.active_model() is None
    assert [s.logged_in for s in auth.status()] == [False, False, False]
    assert all(s.masked_key is None and s.model is None and not s.active for s in auth.status())


def test_environment_variable_counts_as_login(tmp_path):
    env = {"GEMINI_API_KEY": GOOGLE}
    manager = AuthManager(tmp_path / "h", environ=env)
    assert manager.is_authenticated and manager.active_provider() is GEMINI
    assert manager.active_model() == "gemini/gemini-2.5-flash"
    status = {s.spec.id: s for s in manager.status()}
    assert status["gemini"].source == "env" and status["gemini"].active and status["gemini"].masked_key == mask(GOOGLE)
    assert not status["claude"].logged_in and GOOGLE not in repr(manager.status())


def test_load_into_environment_does_not_override_existing_variables(tmp_path):
    store = CredentialStore(tmp_path / "h" / ".env")
    store.set("ANTHROPIC_API_KEY", ANTHROPIC)
    store.set("OPENAI_API_KEY", OPENAI)
    env = {"OPENAI_API_KEY": "do-ambiente-" + "e" * 20}
    manager = AuthManager(tmp_path / "h", environ=env)
    assert manager.load_into_environment() == ["ANTHROPIC_API_KEY"]
    assert env["ANTHROPIC_API_KEY"] == ANTHROPIC and env["OPENAI_API_KEY"].startswith("do-ambiente")
    sources = {s.spec.id: s.source for s in manager.status()}
    assert sources["claude"] == "store" and sources["codex"] == "env"  # o ambiente externo manda


async def test_login_stores_the_key_exports_it_and_activates_the_provider(auth):
    result = await auth.login_api_key("claude", ANTHROPIC)
    assert result.ok and result.provider is CLAUDE and result.model == "claude-sonnet-5-5"
    assert auth.store.read() == {"ANTHROPIC_API_KEY": ANTHROPIC}
    assert auth.environ["ANTHROPIC_API_KEY"] == ANTHROPIC  # vale sem reiniciar
    assert auth.is_authenticated and auth.active_model() == "anthropic/claude-sonnet-5-5"
    assert auth.status()[0].source == "store" and auth.status()[0].active
    assert ANTHROPIC not in repr(result)


async def test_second_login_becomes_active_and_models_are_kept_per_provider(auth):
    await auth.login_api_key("claude", ANTHROPIC)
    await auth.login_api_key(CODEX, OPENAI)
    assert auth.active_provider() is CODEX and auth.active_model() == "openai/gpt-5"
    assert {s.spec.id for s in auth.status() if s.logged_in} == {"claude", "codex"}
    auth.set_active(CLAUDE)
    assert auth.active_model() == "anthropic/claude-sonnet-5-5"
    auth.set_active(CLAUDE, "claude-opus-5-5")
    assert auth.active_model() == "anthropic/claude-opus-5-5" and auth.model_id(CODEX) == "gpt-5"


async def test_failed_login_saves_nothing_and_changes_nothing(auth):
    await auth.login_api_key("claude", ANTHROPIC)
    result = await auth.login_api_key("gemini", "chave-errada-" + "x" * 30)
    assert not result.ok and "recusou" in result.message
    assert not auth.store.get("GEMINI_API_KEY") and "GEMINI_API_KEY" not in auth.environ
    assert auth.active_provider() is CLAUDE
    short = await auth.login_api_key("gemini", "curta")
    assert not short.ok and short.message.startswith("A chave parece incompleta")


async def test_relogin_keeps_a_still_valid_model_choice_and_replaces_a_gone_one(auth):
    await auth.login_api_key("claude", ANTHROPIC)
    auth.set_active(CLAUDE, "claude-opus-5-5")
    await auth.login_api_key("claude", ANTHROPIC)
    assert auth.model_id(CLAUDE) == "claude-opus-5-5"  # ainda existe: mantém
    auth.set_active(CLAUDE, "modelo-que-sumiu")
    await auth.login_api_key("claude", ANTHROPIC)
    assert auth.model_id(CLAUDE) == "claude-sonnet-5-5"  # não existe mais: volta ao padrão


async def test_login_with_rate_limited_key_works_without_model_list(auth):
    # a chave "limitada" não é uma chave válida de nenhum provedor real, só do servidor falso
    result = await auth.login_api_key(CODEX, RATE_LIMITED_KEY)
    assert result.ok and result.model == "gpt-5"  # sem lista: usa o padrão do provedor


async def test_login_reports_permission_warning(auth, monkeypatch):
    monkeypatch.setattr("src.auth.store.restrict_to_owner", lambda *a, **k: "acesso nao restrito")
    result = await auth.login_api_key("claude", ANTHROPIC)
    assert result.ok and result.warning == "acesso nao restrito"


def test_config_default_model_applies_only_to_its_own_provider(tmp_path):
    env = {"GEMINI_API_KEY": GOOGLE, "ANTHROPIC_API_KEY": ANTHROPIC}
    manager = AuthManager(tmp_path / "h", environ=env, preferred_model="gemini/gemini-2.5-pro")
    assert manager.model_id(GEMINI) == "gemini-2.5-pro" and manager.model_id(CLAUDE) == "claude-sonnet-5-5"
    odd = AuthManager(tmp_path / "h", environ=env, preferred_model="ollama/llama3")
    assert odd.model_id(GEMINI) == "gemini-2.5-flash"


async def test_set_active_and_list_models_require_a_login(auth):
    with pytest.raises(NotAuthenticatedError, match="/login gemini"):
        auth.set_active(GEMINI)
    with pytest.raises(NotAuthenticatedError):
        await auth.list_models(GEMINI)
    await auth.login_api_key(GEMINI, GOOGLE)
    assert await auth.list_models(GEMINI) == ["gemini-2.5-flash", "gemini-2.5-pro"]


async def test_active_provider_falls_back_when_the_saved_one_loses_its_key(auth):
    await auth.login_api_key("claude", ANTHROPIC)
    await auth.login_api_key("gemini", GOOGLE)
    assert auth.active_provider() is GEMINI
    del auth.environ["GEMINI_API_KEY"]
    auth.store.remove("GEMINI_API_KEY")
    assert auth.active_provider() is CLAUDE  # cai no primeiro que ainda tem chave
    auth.state.write({"active": "provedor-fantasma", "models": {}})
    assert auth.active_provider() is CLAUDE


async def test_logout_one_provider_all_providers_and_unknown(auth):
    await auth.login_api_key("claude", ANTHROPIC)
    await auth.login_api_key("codex", OPENAI)
    (outcome,) = auth.logout("codex")
    assert outcome.removed and not outcome.still_in_environment
    assert "OPENAI_API_KEY" not in auth.environ and auth.store.get("OPENAI_API_KEY") is None
    assert auth.active_provider() is CLAUDE  # o ativo era codex: sobra o claude
    outcomes = auth.logout()
    assert [o.removed for o in outcomes] == [True, False, False]
    assert not auth.is_authenticated and auth.active_model() is None
    assert auth.store.read() == {}
    with pytest.raises(UnknownProviderError):
        auth.logout("llama")


def test_logout_cannot_remove_an_external_environment_variable(tmp_path):
    env = {"OPENAI_API_KEY": OPENAI}
    manager = AuthManager(tmp_path / "h", environ=env)
    (outcome,) = manager.logout("codex")
    assert not outcome.removed and outcome.still_in_environment
    assert manager.is_authenticated  # continua logado pelo ambiente


# ========================================================================= cliente
async def test_authenticated_llm_refuses_to_run_without_login(tmp_path):
    llm = authenticated_llm(AuthManager(tmp_path / "h", environ={}))
    with pytest.raises(NotAuthenticatedError, match="/login"):
        await llm([{"role": "user", "content": "oi"}])


async def test_authenticated_llm_follows_the_active_model_at_call_time(tmp_path, monkeypatch):
    used = []

    def fake_factory(model):
        async def call(messages):
            used.append(model)
            return "{}"

        return call

    monkeypatch.setattr("src.auth.client.litellm_client", fake_factory)
    env = {"ANTHROPIC_API_KEY": ANTHROPIC, "GEMINI_API_KEY": GOOGLE}
    manager = AuthManager(tmp_path / "h", environ=env)
    llm = authenticated_llm(manager)
    await llm([])
    manager.set_active(GEMINI, "gemini-2.5-pro")  # troca de modelo sem recriar o cliente
    await llm([])
    manager.logout("gemini")
    del env["GEMINI_API_KEY"]
    await llm([])
    assert used == ["anthropic/claude-sonnet-5-5", "gemini/gemini-2.5-pro", "anthropic/claude-sonnet-5-5"]
    assert Path(sys.executable).exists()  # (sanidade do ambiente de teste)


def test_json_that_is_not_an_object_still_validates_the_key():
    list_body = http.HttpResponse(200, "[1, 2, 3]")
    check = ProviderApi(getter=lambda url, headers=None, timeout=0: list_body).check_sync(CLAUDE, ANTHROPIC)
    assert check.ok and check.models == []
