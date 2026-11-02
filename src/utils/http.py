"""HTTP mínimo (stdlib) com verificação de certificado sempre ligada."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

USER_AGENT = "TerminasAgent/0.1 (+local)"
MAX_BYTES = 2_000_000


class HttpError(Exception):
    """Falha de rede (DNS, conexão, tempo esgotado). Respostas 4xx/5xx NÃO são erro aqui."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: str

    def json(self) -> Any:
        return json.loads(self.body)


def ssl_context() -> ssl.SSLContext | None:
    """Verificação de certificado sempre ligada. Usa o bundle do `certifi` se existir, porque
    o repositório de certificados do SO (ex.: Python no Windows) pode não ter a cadeia completa."""
    try:
        import certifi
    except ImportError:
        return None  # contexto padrão do Python
    return ssl.create_default_context(cafile=certifi.where())


def get(
    url: str, headers: Mapping[str, str] | None = None, timeout: float = 15.0, max_bytes: int = MAX_BYTES
) -> HttpResponse:
    """GET síncrono. Retorna a resposta mesmo em 4xx/5xx; levanta `HttpError` se não houver resposta."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(  # noqa: S310 - esquema http(s) validado por quem chama
            request, timeout=timeout, context=ssl_context()
        ) as response:
            raw = response.read(max_bytes)
            charset = response.headers.get_content_charset() or "utf-8"
            return HttpResponse(response.status, raw.decode(charset, errors="replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read(max_bytes)
        return HttpResponse(exc.code, raw.decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise HttpError(str(reason)) from exc


def _open(request: urllib.request.Request, timeout: float):  # noqa: ANN202
    return urllib.request.urlopen(request, timeout=timeout, context=ssl_context())  # noqa: S310


def post(
    url: str,
    *,
    form: Mapping[str, str] | None = None,
    json_body: object | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    max_bytes: int = MAX_BYTES,
) -> HttpResponse:
    """POST síncrono (formulário ou JSON). Retorna a resposta mesmo em 4xx/5xx; `HttpError` sem resposta."""
    merged = {"User-Agent": USER_AGENT, **(headers or {})}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        merged["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(json_body).encode()
        merged["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=merged, method="POST")
    try:
        with _open(request, timeout) as response:
            return HttpResponse(response.status, response.read(max_bytes).decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, exc.read(max_bytes).decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HttpError(str(getattr(exc, "reason", exc))) from exc


def post_stream(
    url: str,
    *,
    json_body: object,
    headers: Mapping[str, str] | None = None,
    timeout: float = 120.0,
) -> tuple[int, Iterator[str]]:
    """POST JSON e devolve (status, linhas). Em 4xx/5xx o corpo vem como uma única 'linha'."""
    merged = {"User-Agent": USER_AGENT, "Content-Type": "application/json", **(headers or {})}
    request = urllib.request.Request(url, data=json.dumps(json_body).encode(), headers=merged, method="POST")
    try:
        response = _open(request, timeout)
    except urllib.error.HTTPError as exc:
        return exc.code, iter([exc.read(MAX_BYTES).decode("utf-8", errors="replace")])
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HttpError(str(getattr(exc, "reason", exc))) from exc

    def lines() -> Iterator[str]:
        try:
            with response:
                for raw in response:
                    yield raw.decode("utf-8", errors="replace").rstrip("\r\n")
        except (TimeoutError, OSError) as exc:
            raise HttpError(str(exc)) from exc

    return response.status, lines()
