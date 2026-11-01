"""Skills de integração: google-workspace, deep-search, swarm-inspector, daily-briefing, MCP."""

import asyncio
import http.server
import json
import sys
import threading
from datetime import datetime

import pytest
import yaml
from pydantic import ValidationError

from src.core import Approval
from src.memory import MemoryEngine
from src.skills import (
    CommandResult,
    McpError,
    McpManager,
    McpServerConfig,
    McpTool,
    McpToolResult,
    SkillContext,
    SkillsRegistry,
    build_default_registry,
    load_mcp_configs,
)
from src.skills.native import deep_search as ds
from src.subagents_bridge import (
    AgentResult,
    AgentTask,
    BaseSubagent,
    SubagentInfo,
    SubagentRegistry,
    SwarmTelemetry,
)
from src.subagents_bridge.schemas import TokenUsage
from src.subagents_bridge.telemetry import run_tracked


async def approve(decision):
    return Approval("once")


class FakeMcp:
    """Substitui McpManager nas skills (mesma interface usada por elas)."""

    def __init__(self, servers=("google-workspace",), reply="ok", is_error=False):
        self.servers = list(servers)
        self.calls = []
        self.reply, self.is_error = reply, is_error

    async def call_tool(self, server, tool, arguments=None):
        self.calls.append((server, tool, arguments))
        return McpToolResult(text=self.reply, is_error=self.is_error, structured={"n": 1})

    async def list_tools(self, server=None):
        return [McpTool(server="srv", name="ping", description="Ping", input_schema={"type": "object"})]


# =============================================================================== google-workspace
@pytest.mark.parametrize(
    ("service", "action", "tool"),
    [("calendar", "list_events", "list_events"), ("gmail", "search", "search_threads"),
     ("gmail", "read", "get_thread"), ("drive", "search", "search_files"),
     ("drive", "read", "read_file_content")],
)  # fmt: skip
async def test_google_reads_are_low_risk_and_call_the_mapped_tool(tmp_path, service, action, tool):
    mcp = FakeMcp(reply="resultado")
    registry = build_default_registry(SkillContext(workdir=tmp_path, mcp=mcp))  # sem approver
    result = await registry.invoke(
        "google-workspace", {"service": service, "action": action, "arguments": {"q": "x"}}
    )
    assert result.ok and result.output == "resultado" and result.data["tool"] == tool
    assert mcp.calls == [("google-workspace", tool, {"q": "x"})]


async def test_google_create_event_needs_approval(tmp_path):
    mcp = FakeMcp()
    params = {"service": "calendar", "action": "create_event", "arguments": {"title": "Review"}}
    blocked = await build_default_registry(SkillContext(workdir=tmp_path, mcp=mcp)).invoke(
        "google-workspace", params
    )
    assert blocked.status == "awaiting_approval" and mcp.calls == []
    allowed = await build_default_registry(
        SkillContext(workdir=tmp_path, mcp=mcp, approver=approve)
    ).invoke("google-workspace", params)
    assert allowed.ok and mcp.calls[0][1] == "create_event"


async def test_google_custom_server_and_tool_names(tmp_path):
    mcp = FakeMcp(servers=["meu-google"])
    ctx = SkillContext(
        workdir=tmp_path,
        mcp=mcp,
        options={"google_server": "meu-google", "google_tools": {("gmail", "search"): "buscar_emails"}},
    )
    result = await build_default_registry(ctx).invoke(
        "google-workspace", {"service": "gmail", "action": "search", "arguments": {}}
    )
    assert result.ok and mcp.calls[0][:2] == ("meu-google", "buscar_emails")


async def test_google_failures(tmp_path):
    params = {"service": "drive", "action": "search"}
    no_mcp = await build_default_registry(SkillContext(workdir=tmp_path)).invoke("google-workspace", params)
    assert not no_mcp.ok and "Gerenciador MCP" in no_mcp.error
    offline = await build_default_registry(SkillContext(workdir=tmp_path, mcp=FakeMcp(servers=[]))).invoke(
        "google-workspace", params
    )
    assert not offline.ok and "não está conectado" in offline.error
    erroring = await build_default_registry(
        SkillContext(workdir=tmp_path, mcp=FakeMcp(reply="cota excedida", is_error=True))
    ).invoke("google-workspace", params)
    assert not erroring.ok and "cota excedida" in erroring.error
    invalid = await build_default_registry(SkillContext(workdir=tmp_path)).invoke(
        "google-workspace", {"service": "drive", "action": "create_event"}
    )
    assert not invalid.ok and "não existe para 'drive'" in invalid.error


# ====================================================================================== MCP (unit)
def test_mcp_config_validation_and_loading():
    cfg = McpServerConfig(name="google-workspace", command="npx", args=["-y", "x"])
    assert cfg.timeout_seconds == 30 and cfg.env is None
    for bad in ({"name": "bad name", "command": "x"}, {"name": "ok", "command": ""},
                {"name": "ok", "command": "x", "timeout_seconds": 0}, {"name": "ok", "command": "x", "foo": 1}):  # fmt: skip
        with pytest.raises(ValidationError):
            McpServerConfig(**bad)
    configs = load_mcp_configs({"mcp_servers": [{"name": "a", "command": "x"}, {"name": "b", "command": "y"}]})
    assert [c.name for c in configs] == ["a", "b"]
    assert load_mcp_configs({}) == [] and load_mcp_configs({"mcp_servers": None}) == []


def test_config_example_has_valid_mcp_and_skills_sections():
    from pathlib import Path

    data = yaml.safe_load((Path(__file__).resolve().parents[2] / "config.example.yaml").read_text(encoding="utf-8"))
    assert load_mcp_configs(data) == [] and "vault_dir" in data["skills"]


def test_mcp_tool_qualified_name_and_default_schema():
    tool = McpTool(server="srv", name="ping")
    assert tool.qualified_name == "mcp.srv.ping" and tool.input_schema == {"type": "object"}


class _Session:
    def __init__(self, behavior="ok"):
        self.behavior = behavior

    async def list_tools(self):
        if self.behavior == "hang":
            await asyncio.sleep(5)
        from types import SimpleNamespace as NS

        return NS(tools=[NS(name="t", description=None, input_schema=None)])

    async def call_tool(self, name, arguments):
        if self.behavior == "hang":
            await asyncio.sleep(5)
        if self.behavior == "boom":
            raise ConnectionError("pipe quebrado")
        from types import SimpleNamespace as NS

        return NS(content=[NS(text="a"), NS(data=b"img"), NS(text="b")], is_error=False, structured_content=None)


def _manager_with(behavior="ok", timeout=0.2):
    manager = McpManager()
    manager._sessions["s"] = _Session(behavior)
    manager._configs["s"] = McpServerConfig(name="s", command="x", timeout_seconds=timeout)
    return manager


async def test_mcp_manager_session_paths_with_fake_session():
    manager = _manager_with()
    (tool,) = await manager.list_tools("s")
    assert tool.name == "t" and tool.description == "" and tool.input_schema == {"type": "object"}
    result = await manager.call_tool("s", "t")
    assert result.text == "a\nb" and not result.is_error  # ignora conteúdo sem texto
    await manager.close()
    assert manager.servers == []


async def test_mcp_manager_error_paths():
    manager = _manager_with("boom")
    with pytest.raises(McpError, match="pipe quebrado"):
        await manager.call_tool("s", "t")
    with pytest.raises(McpError, match="não conectado"):
        await manager.call_tool("nada", "t")
    with pytest.raises(McpError, match="não conectado"):
        await manager.list_tools("nada")
    hanging = _manager_with("hang", timeout=0.1)
    with pytest.raises(McpError, match="excedeu"):
        await hanging.call_tool("s", "t")
    with pytest.raises(McpError, match="não listou"):
        await hanging.list_tools()


async def test_mcp_manager_connect_errors(monkeypatch):
    manager = _manager_with()
    with pytest.raises(McpError, match="já conectado"):
        await manager.connect(McpServerConfig(name="s", command="x"))
    monkeypatch.setitem(sys.modules, "mcp", None)  # simula SDK ausente
    with pytest.raises(McpError, match=r"pip install -e"):
        await McpManager().connect(McpServerConfig(name="novo", command="x"))
    outcome = await McpManager().connect_all([McpServerConfig(name="novo", command="x")])
    assert "SDK MCP ausente" in outcome["novo"]


async def test_register_mcp_tools_requires_manager_and_uses_context():
    registry = SkillsRegistry(SkillContext())
    with pytest.raises(Exception, match="Gerenciador MCP"):
        await registry.register_mcp_tools()
    registry.ctx.mcp = FakeMcp()
    assert await registry.register_mcp_tools() == ["mcp.srv.ping"]
    assert await registry.register_mcp_tools(FakeMcp()) == ["mcp.srv.ping"]  # re-registro substitui


async def test_mcp_tool_skill_without_manager_or_with_error_result():
    registry = SkillsRegistry(SkillContext(approver=approve))
    registry.ctx.mcp = FakeMcp(reply="falhou lá", is_error=True)
    await registry.register_mcp_tools()
    failed = await registry.invoke("mcp.srv.ping", {})
    assert not failed.ok and failed.error == "falhou lá"
    registry.ctx.mcp = None
    no_manager = await registry.invoke("mcp.srv.ping", {})
    assert not no_manager.ok and "Gerenciador MCP" in no_manager.error


# ===================================================================================== deep-search
DDG_PAGE = """
<div class="result"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=1">Resultado <b>A</b></a>
<a class="result__snippet" href="x">Trecho &amp; um da <b>A</b></a></div>
<div class="result"><a class="result__a" href="https://example.org/b">Resultado B</a></div>
<div class="result"><a class="result__a" href="http://127.0.0.1/admin">Interno</a>
<a class="result__snippet" href="x">nao deve ser lido</a></div>
"""
PAGE_HTML = """<html><head><title> Minha  Página </title><style>x{}</style></head>
<body><nav>menu</nav><h1>Título</h1><p>Primeiro   parágrafo.</p><script>alert(1)</script>
<footer>rodapé</footer><div>Segundo<br>bloco</div></body></html>"""


class FakeFetcher:
    def __init__(self, pages=None, fail=()):
        self.pages, self.fail, self.urls = pages or {}, set(fail), []

    async def get(self, url):
        self.urls.append(url)
        if url in self.fail:
            raise ds.SkillError(f"falha em {url}")
        return self.pages.get(url, PAGE_HTML)


class FakeBackend:
    def __init__(self, results):
        self.results, self.queries = results, []

    async def search(self, query, max_results):
        self.queries.append((query, max_results))
        return self.results[:max_results]


@pytest.mark.parametrize(
    "url", ["https://example.com/x", "http://exemplo.com.br", "https://8.8.8.8/dns"]
)
def test_validate_url_accepts_public_http(url):
    assert ds.validate_url(url).startswith("http")


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://x.com", "javascript:alert(1)", "http://localhost:8080", "http://app.localhost",
     "http://127.0.0.1", "http://10.0.0.5", "http://192.168.1.1", "http://169.254.169.254/latest",
     "http://[::1]/", "http://servico.internal", "https:///sem-host", "nao-e-url"],
)  # fmt: skip
def test_validate_url_blocks_dangerous_targets(url):
    with pytest.raises(ds.SkillError):
        ds.validate_url(url)


def test_extract_text_strips_noise_and_truncates():
    page = ds.extract_text(PAGE_HTML, "https://x")
    assert page.title == "Minha Página"
    assert page.text == "Título\nPrimeiro parágrafo.\nSegundo\nbloco"
    assert "menu" not in page.text and "alert" not in page.text and "rodapé" not in page.text
    long = ds.extract_text("<p>" + "palavra " * 500 + "</p>", max_chars=100)
    assert long.text.endswith("[…]") and len(long.text) <= 110


def test_parse_duckduckgo_results():
    results = ds.parse_duckduckgo(DDG_PAGE, 10)
    assert [r.url for r in results] == ["https://example.com/a", "https://example.org/b", "http://127.0.0.1/admin"]
    assert results[0].title == "Resultado A" and results[0].snippet == "Trecho & um da A"
    assert results[1].snippet == ""
    assert len(ds.parse_duckduckgo(DDG_PAGE, 1)) == 1
    assert ds.parse_duckduckgo("<html></html>", 5) == []


async def test_duckduckgo_backend_uses_fetcher():
    fetcher = FakeFetcher({"https://html.duckduckgo.com/html/?q=termina+agent": DDG_PAGE})
    results = await ds.DuckDuckGoBackend(fetcher).search("termina agent", 2)
    assert len(results) == 2 and fetcher.urls == ["https://html.duckduckgo.com/html/?q=termina+agent"]


def search_registry(tmp_path, backend=None, fetcher=None, **options):
    ctx = SkillContext(workdir=tmp_path, options={"search_backend": backend, "fetcher": fetcher, **options})
    return build_default_registry(ctx)


async def test_deep_search_search_and_fetch_ops(tmp_path):
    backend = FakeBackend(ds.parse_duckduckgo(DDG_PAGE, 10))
    fetcher = FakeFetcher({"https://example.com/a": PAGE_HTML})
    registry = search_registry(tmp_path, backend, fetcher)
    found = await registry.invoke("deep-search", {"op": "search", "query": "termina", "max_results": 2})
    assert backend.queries == [("termina", 2)] and len(found.data["results"]) == 2
    assert "1. Resultado A" in found.output and "https://example.org/b" in found.output
    page = await registry.invoke("deep-search", {"op": "fetch", "url": "https://example.com/a"})
    assert page.ok and page.data["title"] == "Minha Página" and page.output.startswith("Título")
    nothing = await search_registry(tmp_path, FakeBackend([]), fetcher).invoke(
        "deep-search", {"op": "search", "query": "x"}
    )
    assert nothing.output == "Sem resultados."


async def test_deep_search_fetch_blocks_internal_urls(tmp_path):
    registry = search_registry(tmp_path, FakeBackend([]), FakeFetcher())
    result = await registry.invoke("deep-search", {"op": "fetch", "url": "http://169.254.169.254/latest"})
    assert not result.ok and "não permitido" in result.error


async def test_deep_search_research_reads_best_sources_and_tolerates_failures(tmp_path):
    results = [ds.SearchResult("A", "https://a.com"), ds.SearchResult("B", "https://b.com"),
               ds.SearchResult("Interno", "http://10.0.0.1/x"), ds.SearchResult("C", "https://c.com")]  # fmt: skip
    fetcher = FakeFetcher({"https://a.com": PAGE_HTML}, fail=["https://b.com"])
    registry = search_registry(tmp_path, FakeBackend(results), fetcher)
    result = await registry.invoke("deep-search", {"op": "research", "query": "tema", "depth": 3})
    assert result.ok and result.data["sources"] == ["https://a.com", "https://c.com"]
    assert result.data["failed"] == ["https://b.com"]
    assert "http://10.0.0.1/x" not in fetcher.urls  # nunca nem tenta
    assert "Fontes para: tema" in result.output and "[1] Minha Página" in result.output


async def test_deep_search_research_with_custom_summarizer_and_no_sources(tmp_path):
    async def summarizer(query, extracts):
        return f"síntese de {len(extracts)} fonte(s) sobre {query}"

    results = [ds.SearchResult("A", "https://a.com")]
    registry = search_registry(tmp_path, FakeBackend(results), FakeFetcher(), summarizer=summarizer)
    result = await registry.invoke("deep-search", {"op": "research", "query": "tema"})
    assert result.output == "síntese de 1 fonte(s) sobre tema"
    empty = await search_registry(tmp_path, FakeBackend([]), FakeFetcher()).invoke(
        "deep-search", {"op": "research", "query": "tema"}
    )
    assert empty.output == "Nenhuma fonte pôde ser lida."


@pytest.mark.parametrize(
    ("params", "fragment"),
    [({"op": "search"}, "exige 'query'"), ({"op": "research", "query": " "}, "exige 'query'"),
     ({"op": "fetch"}, "exige 'url'"), ({"op": "search", "query": "x", "max_results": 99}, "max_results")],
)  # fmt: skip
async def test_deep_search_validation(tmp_path, params, fragment):
    result = await search_registry(tmp_path, FakeBackend([]), FakeFetcher()).invoke("deep-search", params)
    assert not result.ok and fragment in result.error


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = "<p>olá, mundo — acentuação</p>".encode("latin-1", errors="replace")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=latin-1")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_urllib_fetcher_downloads_and_decodes_charset():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        text = ds.UrllibFetcher(timeout=5)._get(f"http://127.0.0.1:{server.server_port}/")
    finally:
        server.shutdown()
        server.server_close()
    assert "olá, mundo" in text


async def test_urllib_fetcher_get_blocks_local_and_wraps_errors(monkeypatch):
    fetcher = ds.UrllibFetcher()
    with pytest.raises(ds.SkillError, match="não permitido"):
        await fetcher.get("http://localhost:1/")
    monkeypatch.setattr(fetcher, "_get", lambda url: (_ for _ in ()).throw(OSError("sem rede")))
    with pytest.raises(ds.SkillError, match="Falha ao baixar.*sem rede"):
        await fetcher.get("https://example.com")


# ================================================================================ swarm-inspector
class Agent(BaseSubagent):
    def __init__(self, name, healthy=True, restartable=False):
        super().__init__(SubagentInfo(name=name, capabilities=["work"]))
        self.healthy, self.cancelled, self.restarts = healthy, [], 0
        if restartable:
            self.restart = self._restart

    async def _restart(self):
        self.restarts += 1

    async def execute(self, task):
        await asyncio.sleep(0.01)
        return AgentResult(
            task_id=task.task_id,
            status="success",
            token_usage=TokenUsage(prompt_tokens=100, completion_tokens=20, model="m"),
        )

    async def cancel(self, task_id):
        self.cancelled.append(task_id)

    async def health(self):
        if self.healthy == "raises":
            raise ConnectionError("morto")
        if self.healthy == "hangs":
            await asyncio.sleep(10)
        return self.healthy


@pytest.fixture
def swarm(tmp_path):
    agents = SubagentRegistry()
    dev, flaky = Agent("dev-agent", restartable=True), Agent("flaky-agent", healthy="raises")
    agents.register(dev)
    agents.register(flaky)
    telemetry = SwarmTelemetry()
    ctx = SkillContext(workdir=tmp_path, subagents=agents, telemetry=telemetry, approver=approve)
    return build_default_registry(ctx), dev, flaky, telemetry


async def test_telemetry_run_tracked_records_success_failure_and_tokens():
    telemetry, agent = SwarmTelemetry(), Agent("a")
    task = AgentTask(assigned_agent="a", goal="g")
    pending = asyncio.create_task(run_tracked(agent, task, telemetry))
    await asyncio.sleep(0)
    assert telemetry.active_tasks("a") == [task.task_id]
    await pending
    stats = telemetry.stats("a")
    assert (stats.calls, stats.successes, stats.failures) == (1, 1, 0)
    assert stats.total_tokens == 120 and stats.avg_latency > 0 and stats.active == set()

    class Broken(Agent):
        async def execute(self, task):
            raise RuntimeError("quebrou")

    with pytest.raises(RuntimeError):
        await run_tracked(Broken("b"), AgentTask(assigned_agent="b", goal="g"), telemetry)
    assert telemetry.stats("b").failures == 1 and telemetry.stats("b").active == set()
    failed = AgentResult(task_id="t", status="failed", error="e")
    telemetry.record_start("c", "t")
    telemetry.record_end("c", "t", failed, 0.5)
    assert telemetry.stats("c").failures == 1 and set(telemetry.all_stats()) == {"a", "b", "c"}
    assert telemetry.stats("nunca").avg_latency == 0.0


async def test_swarm_status_reports_health_latency_and_tokens(swarm):
    registry, dev, flaky, telemetry = swarm
    await run_tracked(dev, AgentTask(assigned_agent="dev-agent", goal="g"), telemetry)
    status = await registry.invoke("swarm-inspector", {"op": "status"})
    rows = {r["agent"]: r for r in status.data["agents"]}
    assert rows["dev-agent"]["healthy"] is True and rows["flaky-agent"]["healthy"] is False
    assert rows["dev-agent"]["calls"] == 1 and rows["dev-agent"]["prompt_tokens"] == 100
    assert rows["dev-agent"]["capabilities"] == ["work"] and rows["dev-agent"]["health_check_ms"] >= 0
    assert "OK  dev-agent" in status.output and "OFF flaky-agent" in status.output

    health = await registry.invoke("swarm-inspector", {"op": "health", "agent": "dev-agent"})
    assert [r["agent"] for r in health.data["agents"]] == ["dev-agent"] and "calls" not in health.data["agents"][0]


async def test_swarm_health_timeout_counts_as_unhealthy(swarm, monkeypatch):
    registry, dev, flaky, _ = swarm
    flaky.healthy = "hangs"
    monkeypatch.setattr("src.skills.native.swarm_inspector.HEALTH_TIMEOUT", 0.05)
    row = (await registry.invoke("swarm-inspector", {"op": "health", "agent": "flaky-agent"})).data["agents"][0]
    assert row["healthy"] is False


async def test_swarm_kill_cancels_active_tasks_and_restart(swarm):
    registry, dev, flaky, telemetry = swarm
    telemetry.record_start("dev-agent", "tsk-1")
    telemetry.record_start("dev-agent", "tsk-2")
    killed = await registry.invoke("swarm-inspector", {"op": "kill", "agent": "dev-agent"})
    assert killed.ok and killed.data["cancelled"] == ["tsk-1", "tsk-2"] and dev.cancelled == ["tsk-1", "tsk-2"]
    assert "2 tarefa(s)" in killed.output

    restarted = await registry.invoke("swarm-inspector", {"op": "restart", "agent": "dev-agent"})
    assert restarted.ok and dev.restarts == 1
    unsupported = await registry.invoke("swarm-inspector", {"op": "restart", "agent": "flaky-agent"})
    assert not unsupported.ok and "não suporta restart" in unsupported.error


async def test_swarm_errors_and_guardrails(swarm, tmp_path):
    registry, *_ = swarm
    unknown = await registry.invoke("swarm-inspector", {"op": "status", "agent": "fantasma"})
    assert not unknown.ok and "não registrado" in unknown.error
    assert "exige 'agent'" in (await registry.invoke("swarm-inspector", {"op": "kill"})).error

    no_registry = build_default_registry(SkillContext(workdir=tmp_path))
    assert "Registry de subagentes" in (await no_registry.invoke("swarm-inspector", {"op": "status"})).error
    empty = build_default_registry(SkillContext(workdir=tmp_path, subagents=SubagentRegistry()))
    assert (await empty.invoke("swarm-inspector", {"op": "status"})).output == "Nenhum subagente registrado."

    unapproved = build_default_registry(
        SkillContext(workdir=tmp_path, subagents=SubagentRegistry(), telemetry=SwarmTelemetry())
    )
    kill = await unapproved.invoke("swarm-inspector", {"op": "kill", "agent": "x"})
    assert kill.status == "awaiting_approval"  # kill é Médio
    no_telemetry = build_default_registry(
        SkillContext(workdir=tmp_path, subagents=swarm[0].ctx.subagents, approver=approve)
    )
    assert "Telemetria" in (await no_telemetry.invoke("swarm-inspector", {"op": "kill", "agent": "dev-agent"})).error


# ================================================================================ daily-briefing
@pytest.fixture
def briefing(tmp_path):
    runner_output = json.dumps(
        [{"number": 8, "title": "TUI", "labels": [{"name": "enhancement"}]}, {"number": 9, "title": "Docs", "labels": []}]
    )

    class Runner:
        calls = []

        async def run(self, command, *, cwd=None, timeout=60.0):
            Runner.calls.append(command)
            return CommandResult(0, runner_output)

    with MemoryEngine(tmp_path / "m.sqlite") as memory:
        ctx = SkillContext(
            workdir=tmp_path,
            memory=memory,
            mcp=FakeMcp(reply="09:00 Standup\n15:00 Reunião de alinhamento"),
            runner=Runner(),
            clock=lambda: datetime(2026, 11, 1, 7, 30),
        )
        yield build_default_registry(ctx), ctx, Runner


async def test_daily_briefing_morning_consolidates_all_sources(briefing):
    registry, ctx, Runner = briefing
    await registry.invoke("quick-notes", {"text": "pagar boleto", "tags": ["pendente"]})
    await registry.invoke("quick-notes", {"text": "ideia solta"})
    result = await registry.invoke("daily-briefing", {"issue_labels": ["priority: high"]})

    assert result.ok and result.data["sources"] == {"calendar": True, "issues": True, "notes": True}
    out = result.output
    assert out.startswith("# Briefing da manhã — 2026-11-01")
    assert "## Agenda de hoje\n09:00 Standup\n15:00 Reunião de alinhamento" in out
    assert "- #8 TUI [enhancement]\n- #9 Docs" in out
    assert "## Pendências" in out and "pagar boleto" in out and "ideia solta" not in out
    server, tool, args = ctx.mcp.calls[0]
    assert (server, tool) == ("google-workspace", "list_events")
    assert args["time_min"].startswith("2026-11-01T00:00:00") and args["time_max"].startswith("2026-11-01T23:59:59")
    assert "--label" in Runner.calls[0] and "priority: high" in Runner.calls[0]


async def test_daily_briefing_evening_headings(briefing):
    registry, *_ = briefing
    out = (await registry.invoke("daily-briefing", {"period": "evening"})).output
    assert out.startswith("# Fechamento do dia — 2026-11-01")
    assert "## O que ficou pendente\nNenhuma nota encontrada." in out


async def test_daily_briefing_degrades_gracefully_when_sources_fail(tmp_path):
    ctx = SkillContext(workdir=tmp_path, mcp=FakeMcp(reply="sem permissão", is_error=True))
    registry = build_default_registry(ctx)  # sem memória; gh falha
    result = await registry.invoke("daily-briefing", {})
    assert result.ok and result.data["sources"] == {"calendar": False, "issues": False, "notes": False}
    assert result.output.count("_Indisponível:") == 3
    assert "sem permissão" in result.output and "Memória" in result.output


async def test_daily_briefing_without_registry_or_with_missing_skills(tmp_path):
    from src.skills.native import DailyBriefing

    standalone = DailyBriefing(SkillContext(workdir=tmp_path))
    result = await standalone.execute({})
    assert result.ok and result.output.count("indisponível") == 3
    partial = SkillsRegistry(SkillContext(workdir=tmp_path))
    partial.register(DailyBriefing(partial.ctx))
    assert (await partial.invoke("daily-briefing", {})).output.count("indisponível") == 3


async def test_daily_briefing_empty_sections(tmp_path):
    class Runner:
        async def run(self, command, *, cwd=None, timeout=60.0):
            return CommandResult(0, "[]")

    with MemoryEngine(tmp_path / "m.sqlite") as memory:
        ctx = SkillContext(workdir=tmp_path, memory=memory, mcp=FakeMcp(reply=""), runner=Runner())
        out = (await build_default_registry(ctx).invoke("daily-briefing", {})).output
    assert "Sem compromissos." in out and "Nenhuma issue aberta." in out


def test_issue_formatting_passes_through_non_json_sources():
    from src.skills.native.daily_briefing import DailyBriefing

    source = (True, "texto puro", {})
    assert DailyBriefing._format_issues(source) is source
    failed = (False, "erro", {})
    assert DailyBriefing._format_issues(failed) is failed


async def test_mcp_tool_skill_without_jsonschema_defers_validation_to_server(monkeypatch):
    monkeypatch.setitem(sys.modules, "jsonschema", None)  # simula pacote ausente
    registry = SkillsRegistry(SkillContext(approver=approve))
    registry.ctx.mcp = FakeMcp(reply="pong")
    await registry.register_mcp_tools()
    result = await registry.invoke("mcp.srv.ping", {"qualquer": "coisa"})
    assert result.ok and result.output == "pong"


async def test_duckduckgo_antibot_page_gives_clear_error_not_empty_results():
    blocked = "<html><title>DuckDuckGo</title><body>Unfortunately, bots use DuckDuckGo too. anomaly-modal challenge</body></html>"
    with pytest.raises(ds.SkillError, match="anti-bot.*SearxngBackend"):
        await ds.DuckDuckGoBackend(FakeFetcher({"https://html.duckduckgo.com/html/?q=x": blocked})).search("x", 3)
    genuinely_empty = await ds.DuckDuckGoBackend(
        FakeFetcher({"https://html.duckduckgo.com/html/?q=y": "<html>sem resultados</html>"})
    ).search("y", 3)
    assert genuinely_empty == []


async def test_deep_search_surfaces_backend_block_as_failure(tmp_path):
    class Blocked:
        async def search(self, query, max_results):
            raise ds.SkillError("bloqueado")

    result = await search_registry(tmp_path, Blocked(), FakeFetcher()).invoke(
        "deep-search", {"op": "search", "query": "x"}
    )
    assert not result.ok and result.error == "bloqueado"


async def test_searxng_backend_parses_json_api():
    payload = json.dumps(
        {"results": [
            {"title": "A", "url": "https://a.com", "content": "trecho a"},
            {"title": "sem url", "content": "ignorado"},
            {"title": "B", "url": "https://b.com"},
            {"title": "C", "url": "https://c.com", "content": "c"},
        ]}
    )  # fmt: skip
    fetcher = FakeFetcher({"https://searx.local/search?q=termina+agent&format=json": payload})
    results = await ds.SearxngBackend("https://searx.local/", fetcher).search("termina agent", 2)
    assert [(r.title, r.url, r.snippet) for r in results] == [("A", "https://a.com", "trecho a"), ("B", "https://b.com", "")]
    assert (await ds.SearxngBackend("https://searx.local", FakeFetcher({"https://searx.local/search?q=x&format=json": "{}"})).search("x", 5)) == []
    with pytest.raises(ds.SkillError, match="não devolveu JSON"):
        await ds.SearxngBackend("https://searx.local", FakeFetcher({"https://searx.local/search?q=x&format=json": "<html>"})).search("x", 5)


def test_ssl_context_prefers_certifi_and_keeps_verification(monkeypatch):
    import ssl

    context = ds._ssl_context()
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    monkeypatch.setitem(sys.modules, "certifi", None)  # sem certifi: usa o padrão do Python
    assert ds._ssl_context() is None
