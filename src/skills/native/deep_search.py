"""deep-search: busca na web, raspagem de páginas e síntese de fontes."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import ssl
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import ClassVar, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.guardrails import ActionRequest
from src.skills.base import Skill, SkillError, SkillResult

USER_AGENT = "TerminasAgent/0.1 (+local)"
MAX_DOWNLOAD_BYTES = 2_000_000
DEFAULT_EXTRACT_CHARS = 4000
_SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg"}
Summarizer = Callable[[str, list["PageExtract"]], Awaitable[str]]


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""


@dataclass(frozen=True)
class PageExtract:
    url: str
    title: str
    text: str


class Fetcher(Protocol):
    async def get(self, url: str) -> str: ...


class SearchBackend(Protocol):
    async def search(self, query: str, max_results: int) -> list[SearchResult]: ...


# ---------------------------------------------------------------------------- segurança
def validate_url(url: str) -> str:
    """Só http/https e nunca hosts locais/privados (evita SSRF por prompt injection).

    Limitação: checa o host literal; não resolve DNS (não impede DNS rebinding)."""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SkillError(f"URL não permitida (apenas http/https): {url}")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise SkillError(f"Host local não permitido: {host}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return parsed.geturl()
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        raise SkillError(f"Endereço interno não permitido: {host}")
    return parsed.geturl()


# ---------------------------------------------------------------------------- HTML -> texto
class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self._parts.append(data)

    @property
    def text(self) -> str:
        joined = "".join(self._parts)
        lines = (re.sub(r"[ \t]+", " ", line).strip() for line in joined.splitlines())
        return "\n".join(line for line in lines if line)


def extract_text(html_text: str, url: str = "", max_chars: int = DEFAULT_EXTRACT_CHARS) -> PageExtract:
    parser = _TextExtractor()
    parser.feed(html_text)
    parser.close()
    text = parser.text
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + " […]"
    return PageExtract(url=url, title=" ".join(parser.title.split()), text=text)


# ---------------------------------------------------------------------------- backends
def _ssl_context() -> ssl.SSLContext | None:
    """Verificação de certificado sempre ligada. Usa o bundle do `certifi` se existir, porque
    o repositório de certificados do SO (ex.: Python no Windows) pode não ter a cadeia completa."""
    try:
        import certifi
    except ImportError:
        return None  # contexto padrão do Python
    return ssl.create_default_context(cafile=certifi.where())


class UrllibFetcher:
    """GET simples com limite de tamanho e timeout (executado em thread)."""

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout

    def _get(self, url: str) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(  # noqa: S310
            request, timeout=self.timeout, context=_ssl_context()
        ) as response:
            raw = response.read(MAX_DOWNLOAD_BYTES)
            charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")

    async def get(self, url: str) -> str:
        try:
            return await asyncio.to_thread(self._get, validate_url(url))
        except SkillError:
            raise
        except Exception as exc:
            raise SkillError(f"Falha ao baixar {url}: {exc}") from exc


class DuckDuckGoBackend:
    """Busca via endpoint HTML do DuckDuckGo (melhor esforço; o layout pode mudar)."""

    def __init__(self, fetcher: Fetcher | None = None) -> None:
        self.fetcher = fetcher or UrllibFetcher()

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        page = await self.fetcher.get(url)
        results = parse_duckduckgo(page, max_results)
        lowered = page.lower()
        if not results and ("anomaly" in lowered or "challenge" in lowered):
            raise SkillError(
                "O DuckDuckGo bloqueou a requisição automatizada (verificação anti-bot). "
                "Configure outro backend em ctx.options['search_backend'], por exemplo "
                "SearxngBackend('https://sua-instancia-searxng')."
            )
        return results


class SearxngBackend:
    """Busca via API JSON de uma instância SearXNG (`/search?format=json`), sem chave de API.

    A instância precisa ter o formato `json` habilitado em `search.formats` (settings.yml)."""

    def __init__(self, base_url: str, fetcher: Fetcher | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.fetcher = fetcher or UrllibFetcher()

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        import json

        url = f"{self.base_url}/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
        try:
            payload = json.loads(await self.fetcher.get(url))
        except json.JSONDecodeError:
            raise SkillError(
                "A instância SearXNG não devolveu JSON (habilite 'json' em search.formats)."
            ) from None
        results = [
            SearchResult(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(item.get("content", "")),
            )
            for item in payload.get("results", [])
            if item.get("url")
        ]
        return results[:max_results]


_RESULT = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r'(?:(?:(?!class="result__a").)*?class="result__snippet"[^>]*>(?P<snippet>.*?)</a>)?',
    re.DOTALL,
)


def _strip_tags(fragment: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", "", fragment or "").split())


def parse_duckduckgo(page: str, max_results: int) -> list[SearchResult]:
    import html as html_lib

    results: list[SearchResult] = []
    for match in _RESULT.finditer(page):
        href = html_lib.unescape(match.group("href"))
        if href.startswith("//"):
            href = "https:" + href
        redirect = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg")
        url = redirect[0] if redirect else href
        results.append(
            SearchResult(
                title=html_lib.unescape(_strip_tags(match.group("title"))),
                url=url,
                snippet=html_lib.unescape(_strip_tags(match.group("snippet") or "")),
            )
        )
        if len(results) >= max_results:
            break
    return results


async def default_summarizer(query: str, extracts: list[PageExtract]) -> str:
    """Sem LLM: junta as fontes numeradas. Injete um summarizer (LLM) para síntese real."""
    if not extracts:
        return "Nenhuma fonte pôde ser lida."
    parts = [f"[{i}] {e.title or e.url}\n{e.url}\n{e.text}" for i, e in enumerate(extracts, 1)]
    return f"Fontes para: {query}\n\n" + "\n\n".join(parts)


class DeepSearchParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["search", "fetch", "research"]
    query: str | None = None
    url: str | None = None
    max_results: int = Field(default=5, ge=1, le=10)
    depth: int = Field(default=3, ge=1, le=5, description="Páginas lidas em 'research'.")
    max_chars: int = Field(default=DEFAULT_EXTRACT_CHARS, ge=200, le=20_000)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.op in {"search", "research"} and not (self.query and self.query.strip()):
            raise ValueError(f"'{self.op}' exige 'query'.")
        if self.op == "fetch" and not self.url:
            raise ValueError("'fetch' exige 'url'.")
        return self


class DeepSearch(Skill):
    name: ClassVar[str] = "deep-search"
    description: ClassVar[str] = (
        "Pesquisa na web: search (resultados), fetch (ler uma página) e research "
        "(buscar, ler as melhores fontes e sintetizar)."
    )
    category: ClassVar[str] = "Pesquisa & Web"
    Params: ClassVar[type[BaseModel]] = DeepSearchParams

    def action(self, params: DeepSearchParams) -> ActionRequest:
        return ActionRequest.tool_call(
            f"{self.name}.{params.op}", agent="master", query=params.query, url=params.url
        )

    @property
    def _fetcher(self) -> Fetcher:
        return self.ctx.options.get("fetcher") or UrllibFetcher()

    @property
    def _backend(self) -> SearchBackend:
        return self.ctx.options.get("search_backend") or DuckDuckGoBackend(self._fetcher)

    async def _read(self, url: str, max_chars: int) -> PageExtract:
        return extract_text(await self._fetcher.get(validate_url(url)), url, max_chars)

    async def run(self, params: DeepSearchParams) -> SkillResult:
        if params.op == "fetch":
            page = await self._read(str(params.url), params.max_chars)
            return SkillResult.success(page.text, url=page.url, title=page.title)

        results = await self._backend.search(str(params.query), params.max_results)
        listing = [{"title": r.title, "url": r.url, "snippet": r.snippet} for r in results]
        if params.op == "search":
            lines = [f"{i}. {r.title}\n   {r.url}\n   {r.snippet}" for i, r in enumerate(results, 1)]
            return SkillResult.success("\n".join(lines) or "Sem resultados.", results=listing)

        readable = [r for r in results if _is_safe(r.url)][: params.depth]
        pages = await asyncio.gather(
            *(self._read(r.url, params.max_chars) for r in readable), return_exceptions=True
        )
        extracts = [p for p in pages if isinstance(p, PageExtract) and p.text]
        failed = [r.url for r, p in zip(readable, pages, strict=True) if not isinstance(p, PageExtract)]
        summarize: Summarizer = self.ctx.options.get("summarizer") or default_summarizer
        summary = await summarize(str(params.query), extracts)
        return SkillResult.success(
            summary,
            results=listing,
            sources=[e.url for e in extracts],
            failed=failed,
        )


def _is_safe(url: str) -> bool:
    try:
        validate_url(url)
    except SkillError:
        return False
    return True
