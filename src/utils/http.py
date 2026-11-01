"""HTTP mínimo (stdlib) com verificação de certificado sempre ligada."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
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
