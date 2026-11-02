"""Login com ChatGPT por codigo de dispositivo e backend do Codex (issue #23, rota propria)."""

import pytest

from src.auth import authenticated_llm
from src.auth.chatgpt import ChatGPTAuthError, ReloginRequired
from src.auth.codex_device import CLIENT_ID, VERIFICATION_URL, account_id_from
from src.ui.services import stop_services
from tests.fake_chatgpt import FakeCodex, make_codex_auth, make_device
from tests.helpers import build_app, output


@pytest.fixture
def fake():
    return FakeCodex()


@pytest.fixture
def shown():
    return []


# ======================================================================= login
async def test_device_login_polls_until_authorized_and_saves_tokens(tmp_path, fake, shown):
    device = make_device(tmp_path, fake)
    tokens = await device.login(lambda url, code: shown.append((url, code)))
    assert shown == [(VERIFICATION_URL, "ABCD-EFGHI")] and fake.polls == 3  # 2 pendentes + 1 liberado
    saved = device.store.read()
    assert saved["client_id"] == CLIENT_ID and saved["account_id"] == "acct-123"
    assert saved["refresh_token"] == tokens.refresh_token and device.account_id() == "acct-123"


async def test_device_login_survives_a_transient_network_error_while_polling(tmp_path, fake):
    from src.utils import http

    device = make_device(tmp_path, fake)
    real = device._post
    state = {"n": 0}

    def flaky(url, **kw):
        if "deviceauth/token" in url and state["n"] == 0:
            state["n"] += 1
            raise http.HttpError("timeout")
        return real(url, **kw)

    device._post = flaky
    assert (await device.login(lambda *a: None)).access_token


@pytest.mark.parametrize(
    ("status", "code", "fragment"),
    [(410, "", "expirou"), (400, "deviceauth_expired", "expirou"), (403, "access_denied_by_user", "negou"),
     (500, "boom", "HTTP 500")],
)  # fmt: skip
async def test_device_poll_errors_stop_without_saving(tmp_path, fake, status, code, fragment):
    fake.poll_error = (status, code)
    device = make_device(tmp_path, fake)
    with pytest.raises(ChatGPTAuthError, match=fragment):
        await device.login(lambda *a: None)
    assert not device.store.has_tokens()


async def test_device_code_request_failure(tmp_path, fake):
    fake.usercode_status = 500
    with pytest.raises(ChatGPTAuthError, match="não gerou o código"):
        await make_device(tmp_path, fake).login(lambda *a: None)


async def test_device_login_times_out_when_never_authorized(tmp_path, fake):
    import src.auth.codex_device as cd

    fake.pending_polls = 10**6
    device = make_device(tmp_path, fake)
    original = cd.MAX_WAIT
    cd.MAX_WAIT = 0.0
    try:
        with pytest.raises(ChatGPTAuthError, match="expirou"):
            await device.login(lambda *a: None)
    finally:
        cd.MAX_WAIT = original
    assert not device.store.has_tokens()


def test_account_id_is_read_from_jwt_claims():
    import jwt

    token = jwt.encode({"https://api.openai.com/auth": {"chatgpt_account_id": "a1"}}, "s" * 40, algorithm="HS256")
    assert account_id_from("lixo", token) == "a1" and account_id_from("lixo") is None


# ==================================================================== gerente
async def test_manager_device_login_and_inference_through_codex_backend(tmp_path, fake, shown):
    auth = make_codex_auth(tmp_path, fake)
    result = await auth.login_codex_device(lambda url, code: shown.append(code))
    assert result.ok and result.model == "gpt-5" and shown == ["ABCD-EFGHI"]
    spec = auth.active_provider()
    assert auth.chatgpt_mode(spec) == "codex" and auth.state.read()["modes"]["codex"] == "codex"
    reply = await authenticated_llm(auth)([{"role": "user", "content": "oi"}])
    assert reply == "olá do plano"
    call = fake.requests[-1]
    assert call["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert call["headers"]["chatgpt-account-id"] == "acct-123" and call["headers"]["OpenAI-Beta"]
    assert call["body"]["instructions"] and call["body"]["store"] is False and call["body"]["stream"] is True


async def test_codex_models_list_and_refresh_without_resource(tmp_path, fake):
    auth = make_codex_auth(tmp_path, fake)
    await auth.login_codex_device(lambda *a: None)
    assert await auth.list_models(auth.active_provider()) == ["gpt-5", "gpt-5-codex"]
    fake.clock.now += 3700
    await authenticated_llm(auth)([{"role": "user", "content": "x"}])  # renova (o fake exige: sem `resource`)
    assert fake.refresh_count == 1


async def test_codex_refresh_invalid_asks_for_new_login(tmp_path, fake):
    auth = make_codex_auth(tmp_path, fake)
    await auth.login_codex_device(lambda *a: None)
    fake.clock.now += 3700
    fake.refresh_mode = "invalid"
    with pytest.raises(ReloginRequired):
        await authenticated_llm(auth)([{"role": "user", "content": "x"}])
    assert not auth.is_authenticated and auth.codex.account_id() == "acct-123"


async def test_failed_device_login_offers_api_key(tmp_path, fake):
    fake.usercode_status = 500
    result = await make_codex_auth(tmp_path, fake).login_codex_device(lambda *a: None)
    assert not result.ok and "/login codex key" in result.message


async def test_logout_codex_clears_the_device_session(tmp_path, fake):
    auth = make_codex_auth(tmp_path, fake)
    await auth.login_codex_device(lambda *a: None)
    assert auth.logout("codex")[0].removed and not auth.is_authenticated
    assert not auth.codex.store.has_tokens() and auth.codex.account_id() == "acct-123"


async def test_saved_mode_decides_between_official_and_device_flows(tmp_path, fake):
    auth = make_codex_auth(tmp_path, fake)
    await auth.login_codex_device(lambda *a: None)
    assert (await auth.login_chatgpt(lambda url: None)).ok  # oficial também (o navegador falso abre sozinho)
    assert auth.chatgpt_mode(auth.active_provider()) == "chatgpt"  # o último login vence
    state = auth.state.read()
    state["modes"]["codex"] = "codex"
    auth.state.write(state)
    assert auth.chatgpt_mode(auth.active_provider()) == "codex"


# ========================================================================= CLI
@pytest.fixture
async def make_app(tmp_path, fake):
    created = []

    async def factory(*lines, **kwargs):
        app, services, reader = await build_app(tmp_path, *lines, auth=make_codex_auth(tmp_path, fake), **kwargs)
        created.append(services)
        return app, services, reader

    yield factory
    for services in created:
        await stop_services(services)


async def test_cli_login_codex_chatgpt_shows_url_and_code(make_app):
    app, services, reader = await make_app()
    await app.handle_line("/login codex chatgpt")
    text = output(app.console)
    assert "https://auth.openai.com/codex/device" in text and "Código: ABCD-EFGHI" in text
    assert "Codex (OpenAI) conectado pelo ChatGPT. Modelo ativo: gpt-5." in text
    assert reader.secret_prompts == [] and services.auth.is_authenticated
    await app.handle_line("/status")
    assert "plano ChatGPT" in output(app.console)


async def test_cli_menu_option_1_is_the_device_flow(make_app):
    app, services, _ = await make_app("1")
    await app.handle_line("/login codex")
    assert "Código: ABCD-EFGHI" in output(app.console)
    assert services.auth.chatgpt_mode(services.auth.active_provider()) == "codex"


async def test_cli_device_failure_explains_and_saves_nothing(make_app, fake):
    fake.poll_error = (403, "access_denied_by_user")
    app, services, _ = await make_app()
    await app.handle_line("/login codex chatgpt")
    text = output(app.console)
    assert "negou a autorização" in text and "/login codex key" in text and "Nada foi salvo." in text
    assert not services.auth.is_authenticated


async def test_cli_model_accepts_any_name_when_backend_gives_no_list(make_app, fake):
    app, services, _ = await make_app()
    await app.handle_line("/login codex chatgpt")
    fake.models = []
    await app.handle_line("/model codex gpt-5.5")
    assert "modelo gpt-5.5" in output(app.console)
