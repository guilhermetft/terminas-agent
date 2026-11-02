"""Login pelo plano ChatGPT (issue #23): OAuth + PKCE, renovação, Responses API, CLI e vazamentos."""

import base64
import hashlib
import json
import os
import stat

import pytest

from src.auth import AuthManager, authenticated_llm, redact
from src.auth.chatgpt import (
    REGISTRATION_CLIENT_ID,
    ChatGPTAuthError,
    ReloginRequired,
    build_authorize_url,
    make_pkce,
)
from src.auth.responses import build_payload, collect_text
from src.ui.services import stop_services
from tests.fake_chatgpt import CLIENT_ID, OTHER_KEY, FakeOpenAI, make_chatgpt_auth, make_oauth
from tests.helpers import FakeLLM, build_app, output, plan_json


@pytest.fixture
def fake():
    return FakeOpenAI()


@pytest.fixture
def seen():
    urls: list[str] = []
    return urls


# ==================================================================== PKCE / URL
def test_pkce_challenge_is_s256_of_the_verifier():
    verifier, challenge = make_pkce()
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert len(verifier) >= 43 and make_pkce()[0] != verifier


def test_authorize_url_registers_on_first_use_and_reuses_client_id_later():
    common = dict(redirect_uri="http://127.0.0.1:1455/auth/callback", challenge="c", state="s", nonce="n", host_id="h")
    first = build_authorize_url(client_id=REGISTRATION_CLIENT_ID, registering=True, **common)
    later = build_authorize_url(client_id="oaiapp_x", registering=False, **common)
    assert "client_id=dynamic_agent_client" in first and "agent_name_hint=" in first and "ext_agent_host_id=h" in first
    assert "client_id=oaiapp_x" in later and "ext_agent_host_id" not in later
    for url in (first, later):
        assert "code_challenge_method=S256" in url and "chatgpt.tokens.use.direct" in url
        assert "resource=https%3A%2F%2Fapi.openai.com%2Fv1" in url and "127.0.0.1" in url


# ======================================================================== login
async def test_login_full_flow_saves_tokens_and_ids(tmp_path, fake, seen):
    oauth = make_oauth(tmp_path, fake)
    tokens = await oauth.login(seen.append)
    assert seen and seen[0].startswith("https://auth.openai.com/api/accounts/authorize?")
    assert fake.authorize_params["client_id"] == REGISTRATION_CLIENT_ID and fake.authorize_params["ext_agent_host_id"]
    assert fake.authorize_params["redirect_uri"].startswith("http://127.0.0.1:")
    saved = oauth.store.read()
    assert saved["client_id"] == CLIENT_ID and saved["refresh_token"] == tokens.refresh_token
    assert saved["ext_agent_host_id"] == fake.authorize_params["ext_agent_host_id"]
    assert fake.token_calls[0]["code_verifier"] and "client_secret" not in fake.token_calls[0]


async def test_second_login_reuses_client_id_and_host_id(tmp_path, fake, seen):
    oauth = make_oauth(tmp_path, fake)
    await oauth.login(seen.append)
    host = oauth.store.read()["ext_agent_host_id"]
    oauth.store.clear_tokens()  # como num /logout
    await oauth.login(seen.append)
    assert fake.authorize_params["client_id"] == CLIENT_ID and "ext_agent_host_id" not in fake.authorize_params
    assert oauth.store.read()["ext_agent_host_id"] == host == oauth.host_id()


@pytest.mark.parametrize(
    ("mode", "fragment"),
    [("denied", "negou o consentimento"), ("bad_state", "'state' inválido"),
     ("no_code", "sem código"), ("wrong_client", "client_id registrado")],
)  # fmt: skip
async def test_callback_failures_save_nothing(tmp_path, fake, seen, mode, fragment):
    fake.callback_mode = mode
    oauth = make_oauth(tmp_path, fake)
    with pytest.raises(ChatGPTAuthError, match=fragment):
        await oauth.login(seen.append)
    assert not oauth.store.has_tokens() and fake.token_calls == []


async def test_missing_scope_is_rejected(tmp_path, fake, seen):
    fake.scope = "openid profile"
    oauth = make_oauth(tmp_path, fake)
    with pytest.raises(ChatGPTAuthError, match="chatgpt.tokens.use.direct"):
        await oauth.login(seen.append)
    assert not oauth.store.has_tokens()


@pytest.mark.parametrize(
    ("tweak", "fragment"),
    [
        ({"id_overrides": {"iss": "https://evil.example"}}, "Invalid issuer"),
        ({"id_overrides": {"aud": "outro-app"}}, "Audience"),
        ({"id_overrides": {"exp": 1}}, "expired"),
        ({"nonce_override": "outro-nonce"}, "nonce"),
        ({"id_key": OTHER_KEY}, "Signature"),
        ({"jwks_ok": False}, "chaves de assinatura"),
    ],
)
async def test_id_token_validation(tmp_path, fake, seen, tweak, fragment):
    for name, value in tweak.items():
        setattr(fake, name, value)
    oauth = make_oauth(tmp_path, fake)
    with pytest.raises(ChatGPTAuthError, match=fragment):
        await oauth.login(seen.append)
    assert not oauth.store.has_tokens()


async def test_login_times_out_without_callback(tmp_path, fake, seen):
    oauth = make_oauth(tmp_path, fake)
    oauth._open_browser = lambda url: None  # ninguém abre o link
    oauth._login_timeout = 0.4
    with pytest.raises(ChatGPTAuthError, match="Tempo esgotado"):
        await oauth.login(seen.append)


async def test_login_survives_a_browser_that_fails_to_open(tmp_path, fake, seen):
    oauth = make_oauth(tmp_path, fake)

    def broken(url):
        fake.browser(url)  # o usuário abre o link impresso à mão
        raise RuntimeError("sem navegador")

    oauth._open_browser = broken
    assert (await oauth.login(seen.append)).access_token


async def test_token_endpoint_error_is_reported(tmp_path, fake, seen):
    fake.token_status, fake.token_error = 500, "server_error"
    with pytest.raises(ChatGPTAuthError, match="HTTP 500: server_error"):
        await make_oauth(tmp_path, fake).login(seen.append)


# ===================================================================== renovação
async def test_refresh_rotates_the_refresh_token(tmp_path, fake, seen):
    oauth = make_oauth(tmp_path, fake)
    first = await oauth.login(seen.append)
    fake.clock.now += 3500  # perto de vencer
    assert oauth.needs_refresh(oauth.stored_tokens())
    second = oauth.refresh()
    assert second.access_token != first.access_token and second.refresh_token != first.refresh_token
    assert oauth.store.read()["refresh_token"] == second.refresh_token  # o novo foi guardado
    fake.clock.now += 3500
    assert oauth.refresh().refresh_token not in {first.refresh_token, second.refresh_token}  # segue rotacionando


async def test_refresh_invalid_clears_tokens_but_keeps_client_id(tmp_path, fake, seen):
    oauth = make_oauth(tmp_path, fake)
    await oauth.login(seen.append)
    fake.refresh_mode = "invalid"
    with pytest.raises(ReloginRequired, match="/login codex"):
        oauth.refresh()
    saved = oauth.store.read()
    assert not oauth.store.has_tokens() and saved["client_id"] == CLIENT_ID and saved["ext_agent_host_id"]
    with pytest.raises(ReloginRequired):  # sem tokens: não tenta de novo, não faz laço
        oauth.refresh()
    assert fake.refresh_count == 1


# ================================================================ Responses API
def test_payload_moves_system_to_instructions_and_drops_forbidden_fields():
    payload = build_payload(
        [{"role": "system", "content": "regras"}, {"role": "user", "content": "oi"},
         {"role": "assistant", "content": "ola"}, {"role": "user", "content": "e agora?"}],
        "gpt-5",
    )  # fmt: skip
    assert payload["instructions"] == "regras" and payload["store"] is False and payload["stream"] is True
    assert [m["role"] for m in payload["input"]] == ["user", "assistant", "user"]
    for forbidden in ("temperature", "max_output_tokens", "top_p", "previous_response_id", "messages"):
        assert forbidden not in payload
    assert "instructions" not in build_payload([{"role": "user", "content": "x"}], "m")


def test_collect_text_needs_completed_event():
    done = ['data: {"type": "response.output_text.delta", "delta": "ab"}', "",
            'data: {"type": "response.completed", "response": {}}']  # fmt: skip
    assert collect_text(done) == "ab"
    from_final = ['data: {"type": "response.completed", "response": {"output": [{"content": '
                  '[{"type": "output_text", "text": "xyz"}]}]}}']  # fmt: skip
    assert collect_text(from_final) == "xyz"
    with pytest.raises(ChatGPTAuthError, match="antes de response.completed"):
        collect_text(done[:1])
    with pytest.raises(ChatGPTAuthError, match="não devolveu"):
        collect_text([])


# ======================================================================= gerente
async def logged_manager(tmp_path, fake, seen, **kwargs):
    auth = make_chatgpt_auth(tmp_path, fake, **kwargs)
    result = await auth.login_chatgpt(seen.append)
    assert result.ok, result.message
    return auth


async def test_manager_login_sets_mode_model_and_status(tmp_path, fake, seen):
    auth = await logged_manager(tmp_path, fake, seen)
    spec = auth.active_provider()
    assert spec.id == "codex" and auth.uses_chatgpt(spec) and auth.is_authenticated
    assert auth.active_model() == "openai/gpt-5" and auth.state.read()["modes"]["codex"] == "chatgpt"
    row = next(s for s in auth.status() if s.spec.id == "codex")
    assert row.logged_in and row.source == "chatgpt" and row.masked_key is None and row.active
    assert await auth.list_models(spec) == ["gpt-5", "gpt-5-codex"]  # só visibility == "list"


async def test_authenticated_llm_uses_responses_api_with_bearer(tmp_path, fake, seen):
    auth = await logged_manager(tmp_path, fake, seen)
    reply = await authenticated_llm(auth)([{"role": "system", "content": "s"}, {"role": "user", "content": "oi"}])
    assert reply == "olá do plano"
    call = fake.requests[-1]
    assert call["url"] == "https://api.openai.com/v1/responses"
    assert call["headers"]["Authorization"].startswith("Bearer ") and call["body"]["instructions"] == "s"


async def test_token_is_renewed_after_an_hour_without_a_new_login(tmp_path, fake, seen):
    auth = await logged_manager(tmp_path, fake, seen)
    llm = authenticated_llm(auth)
    await llm([{"role": "user", "content": "1"}])
    assert fake.refresh_count == 0
    fake.clock.now += 3700  # mais de uma hora depois
    assert await llm([{"role": "user", "content": "2"}]) == "olá do plano"
    assert fake.refresh_count == 1


async def test_a_401_triggers_one_refresh_and_a_retry(tmp_path, fake, seen):
    auth = await logged_manager(tmp_path, fake, seen)
    fake.valid_access.clear()  # o servidor revogou o access token
    fake.valid_access.add("placeholder")
    # o refresh emite um token novo e válido; a primeira chamada (401) é repetida
    assert await authenticated_llm(auth)([{"role": "user", "content": "x"}]) == "olá do plano"
    assert fake.refresh_count == 1


async def test_failed_refresh_asks_for_a_new_login_and_blocks_without_loop(tmp_path, fake, seen):
    auth = await logged_manager(tmp_path, fake, seen)
    fake.clock.now += 3700
    fake.refresh_mode = "invalid"
    llm = authenticated_llm(auth)
    with pytest.raises(ReloginRequired, match="/login codex"):
        await llm([{"role": "user", "content": "x"}])
    assert not auth.is_authenticated  # bloqueado até novo /login
    with pytest.raises(Exception, match="/login"):
        await llm([{"role": "user", "content": "x"}])
    assert fake.refresh_count == 1


@pytest.mark.parametrize(
    ("mode", "fragment", "offers_key"),
    [("usage_limit", "chatgpt.com/settings/usage", True), ("forbidden", "não autorizou", True)],
)
async def test_official_errors_have_clear_messages(tmp_path, fake, seen, mode, fragment, offers_key):
    auth = await logged_manager(tmp_path, fake, seen)
    fake.stream_mode = mode
    with pytest.raises(ChatGPTAuthError, match=fragment) as info:
        await authenticated_llm(auth)([{"role": "user", "content": "x"}])
    assert info.value.offer_api_key is offers_key
    assert auth.is_authenticated  # erro de uso não desloga nem entra em laço de login


@pytest.mark.parametrize("mode", ["failed_event", "truncated", "down"])
async def test_stream_failures_raise_chatgpt_errors(tmp_path, fake, seen, mode):
    auth = await logged_manager(tmp_path, fake, seen)
    fake.stream_mode = mode
    with pytest.raises(ChatGPTAuthError):
        await authenticated_llm(auth)([{"role": "user", "content": "x"}])


async def test_failed_login_returns_message_and_offers_api_key(tmp_path, fake, seen):
    fake.callback_mode = "denied"
    auth = make_chatgpt_auth(tmp_path, fake)
    result = await auth.login_chatgpt(seen.append)
    assert not result.ok and "negou o consentimento" in result.message and "/login codex key" in result.message
    assert not auth.is_authenticated


async def test_logout_codex_erases_tokens_and_blocks(tmp_path, fake, seen):
    auth = await logged_manager(tmp_path, fake, seen)
    outcome = auth.logout("codex")[0]
    assert outcome.removed and not auth.is_authenticated and auth.active_model() is None
    saved = auth.chatgpt_store.read()
    assert "refresh_token" not in saved and "access_token" not in saved and saved["ext_agent_host_id"]
    assert "chatgpt" not in auth.state.read()["modes"].values()


async def test_api_key_and_chatgpt_coexist_and_mode_decides(tmp_path, fake, seen):
    auth = await logged_manager(tmp_path, fake, seen)
    key = "sk-proj-" + "B" * 40
    auth.store.set("OPENAI_API_KEY", key)
    assert auth.uses_chatgpt(auth.active_provider())  # modo gravado = chatgpt
    state = auth.state.read()
    state["modes"]["codex"] = "api_key"
    auth.state.write(state)
    assert not auth.uses_chatgpt(auth.active_provider())
    assert auth.credential(auth.active_provider()).source == "store"


# ====================================================================== CLI
@pytest.fixture
async def make_app(tmp_path, fake):
    created = []

    async def factory(*lines, **kwargs):
        auth = make_chatgpt_auth(tmp_path, fake)
        app, services, reader = await build_app(tmp_path, *lines, auth=auth, **kwargs)
        created.append(services)
        return app, services, reader

    yield factory
    for services in created:
        await stop_services(services)


def screen(app) -> str:
    return output(app.console)


async def test_cli_login_codex_chatgpt_then_plan_without_api_key(make_app, tmp_path, fake):
    llm = FakeLLM(plan_json(("t1", None)))
    app, services, reader = await make_app(llm=llm)
    await app.handle_line("/login codex chatgpt")
    text = screen(app)
    assert "https://auth.openai.com/api/accounts/authorize?" in text and "127.0.0.1" in text
    assert "Codex (OpenAI) conectado pelo plano ChatGPT. Modelo ativo: gpt-5." in text
    assert reader.secret_prompts == [] and services.auth.is_authenticated
    await app.handle_line("/status")
    assert "plano ChatGPT" in screen(app)


async def test_cli_login_codex_menu_lets_choose_between_chatgpt_and_key(make_app):
    app, services, reader = await make_app("1")
    await app.handle_line("/login codex")
    text = screen(app)
    assert "Entrar com ChatGPT" in text and "Chave de API da OpenAI" in text
    assert "Como entrar?" in reader.prompts[0][0] and services.auth.is_authenticated


async def test_cli_login_codex_menu_can_be_cancelled(make_app):
    app, services, _ = await make_app("")
    await app.handle_line("/login codex")
    assert not services.auth.is_authenticated


async def test_cli_login_codex_rejects_unknown_method(make_app):
    app, *_ = await make_app()
    await app.handle_line("/login codex xyz")
    assert "Uso: /login codex [chatgpt|key]." in screen(app)


async def test_cli_consent_denied_explains_and_offers_api_key(make_app, fake):
    fake.callback_mode = "denied"
    app, services, _ = await make_app()
    await app.handle_line("/login codex chatgpt")
    text = screen(app)
    assert "negou o consentimento" in text and "/login codex key" in text and "Nada foi salvo." in text
    assert not services.auth.is_authenticated


async def test_cli_logout_codex_removes_the_session(make_app):
    app, services, _ = await make_app()
    await app.handle_line("/login codex chatgpt")
    await app.handle_line("/logout codex")
    text = screen(app)
    assert "Codex (OpenAI) desconectado" in text and "o agente fica bloqueado" in text
    assert not services.auth.is_authenticated
    await app.handle_line("/plan x")
    assert "bloqueado" in screen(app)


async def test_cli_model_lists_chatgpt_models(make_app):
    app, *_ = await make_app()
    await app.handle_line("/login codex chatgpt")
    await app.handle_line("/model codex list")
    text = screen(app)
    assert "* gpt-5" in text and "gpt-5-codex" in text and "internal-x" not in text
    await app.handle_line("/model codex gpt-5-codex")
    assert "modelo gpt-5-codex" in screen(app)


# ============================================================= segredos / arquivo
def test_redact_and_detect_jwt_tokens():
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1In0.abcdefghijklmnop"
    assert "eyJ" not in redact(f"token={token}!") and "[chave jwt oculta]" in redact(token)
    from src.auth import detect_secret

    assert detect_secret(f"cole {token}") == "jwt" and detect_secret("eyJ sozinho") is None


async def test_no_token_leaks_to_screen_history_memory_or_audit(make_app, tmp_path, fake):
    app, services, _ = await make_app()
    await app.handle_line("/login codex chatgpt")
    await app.handle_line("/status")
    await app.handle_line("/model")
    saved = services.auth.chatgpt_store.read()
    secrets_ = [saved["access_token"], saved["refresh_token"]]
    haystack = [screen(app)]
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.name != "chatgpt.json" and path.suffix not in {".sqlite", ".db"}:
            try:
                haystack.append(path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
    blob = "\n".join(haystack)
    for secret in secrets_:
        assert secret not in blob
    await app.handle_line(f"cole isto: {secrets_[0]}")  # token colado como texto é recusado
    assert secrets_[0] not in screen(app)


@pytest.mark.real_permissions
async def test_credentials_file_is_owner_only(tmp_path, fake, seen):
    oauth = make_oauth(tmp_path, fake)
    await oauth.login(seen.append)
    assert oauth.store.warning is None
    if os.name != "nt":
        assert stat.S_IMODE(oauth.store.path.stat().st_mode) == 0o600
    assert json.loads(oauth.store.path.read_text(encoding="utf-8"))["refresh_token"]


def test_auth_manager_signature_still_works_without_chatgpt(tmp_path):
    auth = AuthManager(tmp_path / "a", environ={"OPENAI_API_KEY": "sk-proj-" + "B" * 40})
    assert auth.is_authenticated and not auth.uses_chatgpt(auth.active_provider())
