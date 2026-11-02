"""Login com o plano ChatGPT (OAuth + PKCE) para apps open-source locais da OpenAI.

Fluxo oficial: registro dinâmico (`client_id=dynamic_agent_client`), Authorization Code com PKCE,
callback em 127.0.0.1, validação do ID token e renovação por refresh token com rotação.
Tudo que fala com a rede é injetável (os testes usam servidores falsos)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import threading
import time
import urllib.parse
import uuid
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt

from src.auth.store import ChatGPTStore
from src.utils import http

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"  # confirmado em openid-configuration (jwks_uri)
REGISTRATION_CLIENT_ID = "dynamic_agent_client"
HOST_ID_PREFIX = "urn:uuid:"  # formato aceito pelo authorize (um UUID puro devolve invalid_authorize_request)
AGENT_NAME = "Termina's Agent"
RESOURCE = "https://api.openai.com/v1"
REQUIRED_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = f"openid profile email offline_access resource.invoke {REQUIRED_SCOPE}"
CALLBACK_PATH = "/auth/callback"
DEFAULT_PORTS = (1455, 0)  # 1455 é o padrão; nos logins seguintes a porta pode variar (0 = qualquer livre)
LOGIN_TIMEOUT = 300.0
REFRESH_MARGIN = 120.0  # renova com 2 min de folga (o access token dura 1 hora)
USAGE_URL = "chatgpt.com/settings/usage"


class ChatGPTAuthError(Exception):
    """Erro de login/renovação com mensagem pronta para o usuário (sem segredos)."""

    def __init__(self, message: str, *, offer_api_key: bool = True) -> None:
        super().__init__(message)
        self.offer_api_key = offer_api_key


class ReloginRequired(ChatGPTAuthError):
    """O refresh token não vale mais: é preciso entrar de novo."""


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str = ""


Poster = Callable[..., http.HttpResponse]
Getter = Callable[..., http.HttpResponse]
UrlHandler = Callable[[str], None]


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make_pkce() -> tuple[str, str]:
    """(code_verifier, code_challenge S256)."""
    verifier = _b64url(secrets.token_bytes(48))
    return verifier, _b64url(hashlib.sha256(verifier.encode()).digest())


def build_authorize_url(
    *, client_id: str, redirect_uri: str, challenge: str, state: str, nonce: str, host_id: str, registering: bool
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "resource": RESOURCE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
    }
    if registering:
        params["agent_name_hint"] = AGENT_NAME
        params["ext_agent_host_id"] = host_id
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


_PAGE = (
    "<!doctype html><meta charset=utf-8><title>Termina</title>"
    "<body style='font-family:sans-serif;margin:3em'><h2>{title}</h2><p>{body}</p></body>"
)


class CallbackServer:
    """Servidor HTTP efêmero em 127.0.0.1 que recebe o redirecionamento do OAuth (uma vez)."""

    def __init__(self, ports: tuple[int, ...] = DEFAULT_PORTS) -> None:
        self.params: dict[str, str] | None = None
        self._done = threading.Event()
        self._cancel = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != CALLBACK_PATH:
                    self.send_error(404)
                    return
                query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                failed = "error" in query
                title = "Não foi possível entrar" if failed else "Pronto! Pode fechar esta aba."
                body = "Volte ao terminal." if failed else "O Termina já recebeu o seu login. Volte ao terminal."
                payload = _PAGE.format(title=title, body=body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                if owner.params is None:
                    owner.params = query
                    owner._done.set()

            def log_message(self, *args: object) -> None:  # silencioso: a URL tem code/state
                return

        last: OSError | None = None
        self._server: HTTPServer | None = None
        for port in ports:
            try:
                self._server = HTTPServer(("127.0.0.1", port), Handler)
                break
            except OSError as exc:
                last = exc
        if self._server is None:
            raise ChatGPTAuthError(f"Não consegui abrir a porta local para receber o login: {last}")
        self.port = self._server.server_address[1]
        self._server.timeout = 0.2

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{CALLBACK_PATH}"

    def wait(self, timeout: float) -> dict[str, str] | None:
        """Bloqueia (use em thread) até o callback, o cancelamento ou o tempo esgotar."""
        assert self._server is not None
        deadline = time.monotonic() + timeout
        while not self._done.is_set() and not self._cancel.is_set() and time.monotonic() < deadline:
            self._server.handle_request()
        return self.params

    def cancel(self) -> None:
        self._cancel.set()

    def close(self) -> None:
        if self._server is not None:
            self._server.server_close()


class ChatGPTOAuth:
    def __init__(
        self,
        store: ChatGPTStore,
        *,
        post: Poster = http.post,
        get: Getter = http.get,
        open_browser: Callable[[str], object] = webbrowser.open,
        ports: tuple[int, ...] = DEFAULT_PORTS,
        login_timeout: float = LOGIN_TIMEOUT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self._post = post
        self._get = get
        self._open_browser = open_browser
        self._ports = ports
        self._login_timeout = login_timeout
        self._clock = clock

    # ---------------------------------------------------------------- identidade local
    def host_id(self) -> str:
        """`ext_agent_host_id`: gerado uma vez e guardado, para o registro ser estável."""
        value = self.store.read().get("ext_agent_host_id")
        if isinstance(value, str) and value.startswith(HOST_ID_PREFIX):
            return value
        # ids antigos (UUID puro) foram recusados pela OpenAI; sem login feito, é seguro trocar
        value = f"{HOST_ID_PREFIX}{uuid.uuid4()}"
        self.store.update(ext_agent_host_id=value)
        return value

    # --------------------------------------------------------------------- login
    def prepare(self) -> tuple[CallbackServer, str, dict[str, str]]:
        server = CallbackServer(self._ports)
        verifier, challenge = make_pkce()
        saved_client = self.store.read().get("client_id")
        registering = not (isinstance(saved_client, str) and saved_client)
        ctx = {
            "verifier": verifier,
            "state": secrets.token_urlsafe(24),
            "nonce": secrets.token_urlsafe(24),
            "client_id": REGISTRATION_CLIENT_ID if registering else str(saved_client),
        }
        url = build_authorize_url(
            client_id=ctx["client_id"],
            redirect_uri=server.redirect_uri,
            challenge=challenge,
            state=ctx["state"],
            nonce=ctx["nonce"],
            host_id=self.host_id(),
            registering=registering,
        )
        return server, url, ctx

    async def login(self, on_url: UrlHandler) -> Tokens:
        """Mostra o link (`on_url`), tenta abrir o navegador e espera o login. Grava ao final."""
        server, url, ctx = self.prepare()
        try:
            on_url(url)
            try:
                self._open_browser(url)
            except Exception:  # noqa: BLE001 - o link já foi mostrado no terminal
                pass
            try:
                params = await asyncio.to_thread(server.wait, self._login_timeout)
            except asyncio.CancelledError:
                server.cancel()
                raise
        finally:
            server.close()
        if params is None:
            raise ChatGPTAuthError("Tempo esgotado esperando o login no navegador.")
        return await asyncio.to_thread(self.complete, params, ctx, server.redirect_uri)

    def complete(self, params: dict[str, str], ctx: dict[str, str], redirect_uri: str) -> Tokens:
        if "error" in params:
            raise self._callback_error(params)
        if not secrets.compare_digest(params.get("state", ""), ctx["state"]):
            raise ChatGPTAuthError("O login voltou com um 'state' inválido e foi descartado.")
        code = params.get("code")
        if not code:
            raise ChatGPTAuthError("O login voltou sem código de autorização.")
        client_id = params.get("client_id") or ctx["client_id"]
        if client_id == REGISTRATION_CLIENT_ID:
            raise ChatGPTAuthError("A OpenAI não devolveu o client_id registrado para o Termina.")
        body = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": ctx["verifier"],
                "resource": RESOURCE,
            }
        )
        scope = str(body.get("scope", ""))
        if REQUIRED_SCOPE not in scope.split():
            raise ChatGPTAuthError(
                "A OpenAI não concedeu a permissão de usar o plano ChatGPT neste app "
                f"(escopo {REQUIRED_SCOPE} ausente)."
            )
        id_token = body.get("id_token")
        if not isinstance(id_token, str):
            raise ChatGPTAuthError("A resposta do login não trouxe ID token.")
        self.validate_id_token(id_token, client_id=client_id, nonce=ctx["nonce"])
        access, refresh = body.get("access_token"), body.get("refresh_token")
        if not isinstance(access, str) or not isinstance(refresh, str):
            raise ChatGPTAuthError("A resposta do login não trouxe os tokens esperados.")
        tokens = Tokens(access, refresh, self._clock() + float(body.get("expires_in", 3600)), scope)
        self._save(client_id, tokens)
        return tokens

    @staticmethod
    def _callback_error(params: dict[str, str]) -> ChatGPTAuthError:
        code = params.get("error", "")
        if code == "access_denied":
            return ChatGPTAuthError("Você negou o consentimento no navegador.")
        detail = params.get("error_description") or code
        return ChatGPTAuthError(f"A OpenAI recusou o login: {detail}")

    # ---------------------------------------------------------------- ID token
    def validate_id_token(self, id_token: str, *, client_id: str, nonce: str) -> dict[str, object]:
        """Assinatura (JWKS), issuer, audience, expiração e nonce."""
        try:
            header = jwt.get_unverified_header(id_token)
            jwk = next((k for k in self._fetch_jwks() if k.get("kid") == header.get("kid")), None)
            if jwk is None:
                raise ChatGPTAuthError("Não achei a chave de assinatura do ID token.")
            claims = jwt.decode(
                id_token,
                jwt.PyJWK(jwk).key,
                algorithms=["RS256"],
                audience=client_id,
                issuer=ISSUER,
                leeway=60,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise ChatGPTAuthError(f"ID token inválido: {exc}") from exc
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise ChatGPTAuthError("ID token com nonce diferente do esperado.")
        return claims

    def _fetch_jwks(self) -> list[dict[str, object]]:
        try:
            response = self._get(JWKS_URL, timeout=15)
            keys = response.json().get("keys") if response.status == 200 else None
        except (http.HttpError, ValueError, AttributeError) as exc:
            raise ChatGPTAuthError(f"Não consegui obter as chaves de assinatura da OpenAI: {exc}") from exc
        if not isinstance(keys, list):
            raise ChatGPTAuthError("Não consegui obter as chaves de assinatura da OpenAI.")
        return [k for k in keys if isinstance(k, dict)]

    # ------------------------------------------------------------------- renovação
    def stored_tokens(self) -> Tokens | None:
        data = self.store.read()
        access, refresh = data.get("access_token"), data.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            return None
        return Tokens(str(access or ""), refresh, float(data.get("expires_at") or 0), str(data.get("scope") or ""))

    def needs_refresh(self, tokens: Tokens) -> bool:
        return not tokens.access_token or tokens.expires_at - self._clock() < REFRESH_MARGIN

    def refresh(self) -> Tokens:
        """Troca o refresh token (que rotaciona: o novo é guardado). Inválido -> ReloginRequired."""
        tokens = self.stored_tokens()
        client_id = self.store.read().get("client_id")
        if tokens is None or not isinstance(client_id, str):
            raise ReloginRequired("Sessão ChatGPT ausente. Use /login codex.")
        try:
            body = self._token_request(
                {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token, "client_id": client_id,
                 "resource": RESOURCE},
                refreshing=True,
            )
        except ReloginRequired:
            self.store.clear_tokens()  # sem laço: o próximo passo é /login
            raise
        access = body.get("access_token")
        if not isinstance(access, str):
            raise ChatGPTAuthError("A renovação não trouxe um novo access token.")
        new = Tokens(
            access,
            str(body.get("refresh_token") or tokens.refresh_token),
            self._clock() + float(body.get("expires_in", 3600)),
            str(body.get("scope") or tokens.scope),
        )
        self._save(client_id, new)
        return new

    def _save(self, client_id: str, tokens: Tokens) -> None:
        self.store.update(
            client_id=client_id,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            expires_at=tokens.expires_at,
            scope=tokens.scope,
        )

    def _token_request(self, form: dict[str, str], *, refreshing: bool = False) -> dict[str, object]:
        try:
            response = self._post(TOKEN_URL, form=form, timeout=30)
        except http.HttpError as exc:
            raise ChatGPTAuthError(f"Não consegui falar com a OpenAI: {exc}", offer_api_key=False) from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status == 200 and isinstance(body, dict):
            return body
        code = str(body.get("error", "")) if isinstance(body, dict) else ""
        if refreshing and (code in {"invalid_grant", "invalid_client"} or response.status in {400, 401}):
            raise ReloginRequired(
                "A sessão do plano ChatGPT expirou ou foi revogada. Use /login codex para entrar de novo."
            )
        suffix = f": {code}" if code else ""
        raise ChatGPTAuthError(f"A OpenAI recusou a troca de tokens (HTTP {response.status}{suffix}).")
