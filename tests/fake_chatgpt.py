"""OpenAI falsa para o login pelo plano ChatGPT: autorização, token, JWKS, modelos e Responses (stream)."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from src.auth import AuthManager, ProviderApi
from src.auth.chatgpt import ISSUER, JWKS_URL, REQUIRED_SCOPE, TOKEN_URL, ChatGPTOAuth
from src.auth.responses import ResponsesClient
from src.auth.store import ChatGPTStore
from src.utils import http

CLIENT_ID = "oaiapp_test123"
GOOD_CODE = "good-code"
HS_SECRET = "s" * 48  # só para fabricar access tokens com cara de JWT (>= 32 bytes: sem aviso do PyJWT)
KID = "test-key-1"


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


SIGNING_KEY = _key()
OTHER_KEY = _key()


def jwk_of(private: rsa.RSAPrivateKey) -> dict:
    data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    return {**data, "kid": KID, "alg": "RS256", "use": "sig"}


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class FakeOpenAI:
    """Todos os pontos de rede do fluxo, com comportamentos ajustáveis pelos testes."""

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock = clock or Clock()
        self.authorize_params: dict[str, str] = {}
        self.callback_mode = "ok"  # ok | denied | bad_state | no_code | wrong_client
        self.scope = f"openid profile email offline_access {REQUIRED_SCOPE}"
        self.id_key = SIGNING_KEY
        self.id_overrides: dict = {}
        self.nonce_override: str | None = None
        self.token_status = 200
        self.token_error = ""
        self.refresh_mode = "ok"  # ok | invalid | down
        self.refresh_count = 0
        self.issued = 0
        self.valid_access: set[str] = set()
        self.refresh_tokens: set[str] = set()
        self.token_calls: list[dict] = []
        self.requests: list[dict] = []
        self.stream_mode = "ok"  # ok | usage_limit | forbidden | failed_event | truncated | down
        self.reply_text = "olá do plano"
        self.models = [
            {"id": "gpt-5", "visibility": "list"},
            {"id": "gpt-5-codex", "visibility": "list"},
            {"id": "internal-x", "visibility": "hide"},
        ]
        self.jwks_ok = True
        self.jwks_calls = 0

    # ------------------------------------------------------------ navegador
    def browser(self, url: str) -> bool:
        params = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
        self.authorize_params = params
        redirect = params["redirect_uri"]
        if self.callback_mode == "denied":
            query = {"error": "access_denied", "state": params["state"]}
        else:
            query = {
                "code": GOOD_CODE,
                "state": "tampered" if self.callback_mode == "bad_state" else params["state"],
                "client_id": CLIENT_ID,
            }
            if self.callback_mode == "no_code":
                query.pop("code")
            if self.callback_mode == "wrong_client":
                query["client_id"] = "dynamic_agent_client"
        target = f"{redirect}?{urllib.parse.urlencode(query)}"
        threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=10).read(), daemon=True).start()
        return True

    # ---------------------------------------------------------------- tokens
    def _new_access(self) -> str:
        self.issued += 1
        token = jwt.encode({"sub": "u", "n": self.issued, "iat": int(time.time())}, HS_SECRET, algorithm="HS256")
        self.valid_access.add(token)
        return token

    def _new_refresh(self) -> str:
        self.issued += 1
        token = f"rt_refresh_{self.issued}_" + "r" * 24
        self.refresh_tokens.add(token)
        return token

    def _id_token(self, client_id: str) -> str:
        claims = {
            "iss": ISSUER,
            "aud": client_id,
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "nonce": self.nonce_override or self.authorize_params.get("nonce", ""),
            "email": "a@b.c",
        }
        claims.update(self.id_overrides)
        return jwt.encode(claims, self.id_key, algorithm="RS256", headers={"kid": KID})

    def post(self, url: str, *, form=None, json_body=None, headers=None, timeout=30) -> http.HttpResponse:
        assert url == TOKEN_URL, url
        self.token_calls.append(dict(form))
        if self.token_status != 200:
            return http.HttpResponse(self.token_status, json.dumps({"error": self.token_error}))
        if form["grant_type"] == "authorization_code":
            assert form["code"] == GOOD_CODE
            assert form["client_id"] == CLIENT_ID
            assert form["code_verifier"]
            body = {
                "access_token": self._new_access(),
                "refresh_token": self._new_refresh(),
                "id_token": self._id_token(form["client_id"]),
                "expires_in": 3600,
                "scope": self.scope,
            }
            return http.HttpResponse(200, json.dumps(body))
        # refresh
        self.refresh_count += 1
        if self.refresh_mode == "down":
            raise http.HttpError("sem rede")
        if self.refresh_mode == "invalid" or form["refresh_token"] not in self.refresh_tokens:
            return http.HttpResponse(400, json.dumps({"error": "invalid_grant"}))
        self.refresh_tokens.discard(form["refresh_token"])  # rotação: o antigo morre
        body = {"access_token": self._new_access(), "refresh_token": self._new_refresh(), "expires_in": 3600}
        return http.HttpResponse(200, json.dumps(body))

    # ------------------------------------------------------------------- GET
    def get(self, url: str, headers=None, timeout=15, **_: object) -> http.HttpResponse:
        self.requests.append({"url": url, "headers": dict(headers or {})})
        if url == JWKS_URL:
            self.jwks_calls += 1
            if not self.jwks_ok:
                return http.HttpResponse(500, "boom")
            return http.HttpResponse(200, json.dumps({"keys": [jwk_of(SIGNING_KEY)]}))
        if url.endswith("/v1/models"):
            if (headers or {}).get("Authorization", "").removeprefix("Bearer ") not in self.valid_access:
                return http.HttpResponse(401, "{}")
            return http.HttpResponse(200, json.dumps({"data": self.models}))
        raise AssertionError(url)

    # ------------------------------------------------------------- Responses
    def stream(self, url: str, *, json_body, headers=None, timeout=120):
        self.requests.append({"url": url, "body": json_body, "headers": dict(headers or {})})
        token = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        if self.stream_mode == "down":
            raise http.HttpError("sem rede")
        if token not in self.valid_access:
            return 401, iter(['{"error": {"code": "invalid_token"}}'])
        if self.stream_mode == "usage_limit":
            return 429, iter([json.dumps({"error": {"code": "subscription_sharing_usage_limit_exceeded"}})])
        if self.stream_mode == "forbidden":
            return 403, iter(['{"error": {"code": "consent_required", "message": "x"}}'])
        text = self.reply_text
        events = [
            {"type": "response.created", "response": {}},
            {"type": "response.output_text.delta", "delta": text[:3]},
            {"type": "response.output_text.delta", "delta": text[3:]},
        ]
        if self.stream_mode == "failed_event":
            events.append({"type": "response.failed", "response": {"error": {"code": "server_error", "message": "m"}}})
        elif self.stream_mode != "truncated":
            events.append({"type": "response.completed", "response": {"output": []}})
        lines = []
        for event in events:
            lines += [f"event: {event['type']}", f"data: {json.dumps(event)}", ""]
        return 200, iter(lines)


class FakeCodex(FakeOpenAI):
    """Fluxo de código de dispositivo + backend do Codex."""

    DEVICE_ID = "deviceauth_test"
    USER_CODE = "ABCD-EFGHI"

    def __init__(self, clock: Clock | None = None) -> None:
        super().__init__(clock)
        self.pending_polls = 2  # respostas "pendente" antes de liberar
        self.polls = 0
        self.usercode_status = 200
        self.poll_error: tuple[int, str] | None = None
        self.account_id = "acct-123"
        self.token_form_calls: list[dict] = []
        self.models = [{"slug": "gpt-5", "visibility": "list"}, {"slug": "gpt-5-codex"}, {"slug": "hid", "visibility": "hide"}]

    def post(self, url, *, form=None, json_body=None, headers=None, timeout=30):
        from src.auth import codex_device as cd

        if url == cd.USERCODE_URL:
            if self.usercode_status != 200:
                return http.HttpResponse(self.usercode_status, "{}")
            assert json_body == {"client_id": cd.CLIENT_ID}
            body = {"device_auth_id": self.DEVICE_ID, "user_code": self.USER_CODE, "interval": "1",
                    "expires_at": "2099-01-01T00:00:00+00:00"}
            return http.HttpResponse(200, json.dumps(body))
        if url == cd.POLL_URL:
            self.polls += 1
            assert json_body == {"device_auth_id": self.DEVICE_ID, "user_code": self.USER_CODE}
            if self.poll_error:
                status, code = self.poll_error
                return http.HttpResponse(status, json.dumps({"error": {"code": code}}))
            if self.polls <= self.pending_polls:
                return http.HttpResponse(403, json.dumps({"error": {"code": "deviceauth_authorization_pending"}}))
            return http.HttpResponse(200, json.dumps({"authorization_code": GOOD_CODE, "code_verifier": "ver-123",
                                                      "code_challenge": "x"}))
        assert url == TOKEN_URL, url
        if form["client_id"] != cd.CLIENT_ID:  # fluxo oficial (cliente emitido)
            return super().post(url, form=form, json_body=json_body, headers=headers, timeout=timeout)
        self.token_form_calls.append(dict(form))
        if form["grant_type"] == "authorization_code":
            assert form["code"] == GOOD_CODE and form["code_verifier"] == "ver-123"
            assert form["client_id"] == cd.CLIENT_ID and form["redirect_uri"] == cd.REDIRECT_URI
            access = self._new_access()
            body = {"access_token": access, "refresh_token": self._new_refresh(), "expires_in": 3600,
                    "id_token": self._id_token(cd.CLIENT_ID)}
            return http.HttpResponse(200, json.dumps(body))
        assert "resource" not in form  # o refresh do Codex não leva `resource`
        return super().post(url, form=form, json_body=json_body, headers=headers, timeout=timeout)

    def _id_token(self, client_id: str) -> str:
        from src.auth import codex_device as cd

        if client_id != cd.CLIENT_ID:
            return super()._id_token(client_id)
        claims = {"iss": ISSUER, "aud": client_id, "exp": int(time.time()) + 3600,
                  "https://api.openai.com/auth": {"chatgpt_account_id": self.account_id}}
        return jwt.encode(claims, self.id_key, algorithm="RS256", headers={"kid": KID})

    def get(self, url, headers=None, timeout=15, **kw):
        if "backend-api/codex/models" in url:
            self.requests.append({"url": url, "headers": dict(headers or {})})
            token = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
            if token not in self.valid_access:
                return http.HttpResponse(401, "{}")
            return http.HttpResponse(200, json.dumps({"models": self.models}))
        return super().get(url, headers=headers, timeout=timeout, **kw)


def make_device(directory: Path, fake: FakeCodex):
    from src.auth.codex_device import CodexDeviceAuth

    async def no_sleep(_: float) -> None:
        return None

    return CodexDeviceAuth(
        ChatGPTStore(directory / "codex.json"), post=fake.post, get=fake.get,
        open_browser=lambda url: True, clock=fake.clock, sleep=no_sleep,
    )


def make_codex_auth(root: Path, fake: FakeCodex) -> AuthManager:
    from src.auth.codex_device import CodexBackendClient

    directory = root / "auth-home"
    device = make_device(directory, fake)
    return AuthManager(
        directory, environ={}, codex=device,
        codex_client=CodexBackendClient(device.account_id, stream=fake.stream, get=fake.get),
        chatgpt=make_oauth(directory, fake), responses=ResponsesClient(stream=fake.stream, get=fake.get),
    )


def make_oauth(directory: Path, fake: FakeOpenAI, **kwargs) -> ChatGPTOAuth:
    return ChatGPTOAuth(
        ChatGPTStore(directory / "chatgpt.json"),
        post=fake.post,
        get=fake.get,
        open_browser=fake.browser,
        ports=(0,),
        login_timeout=10,
        clock=fake.clock,
        **kwargs,
    )


def make_chatgpt_auth(root: Path, fake: FakeOpenAI, *, api: ProviderApi | None = None, environ=None) -> AuthManager:
    directory = root / "auth-home"
    return AuthManager(
        directory,
        environ={} if environ is None else environ,
        api=api,
        chatgpt=make_oauth(directory, fake),
        responses=ResponsesClient(stream=fake.stream, get=fake.get),
    )
