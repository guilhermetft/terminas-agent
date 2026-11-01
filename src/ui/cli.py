"""Terminal interativo do Termina: parser de slash commands, loop e modal HITL."""

from __future__ import annotations

import asyncio
import difflib
import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from src import __version__
from src.core.dag import TaskDAG
from src.core.engine import NodeOutcome, NodeState, RunEvent, RunReport
from src.core.guardrails import Approval, GuardrailDecision, GuardrailError
from src.auth import (
    PROVIDERS,
    NotAuthenticatedError,
    ProviderSpec,
    ProviderStatus,
    UnknownProviderError,
    detect_secret,
    get_provider,
    redact,
)
from src.core.planner import PlanningError
from src.skills.base import SkillResult
from src.ui import components
from src.ui.components import theme
from src.ui.prompt import LineReader
from src.ui.services import Services

PROMPT = "terminas > "
DEFAULT_AUTO_MINUTES = 30
MAX_AUTO_MINUTES = 240


# ================================================================================ parser
@dataclass(frozen=True)
class CommandSpec:
    name: str
    usage: str
    summary: str
    aliases: tuple[str, ...] = ()


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("plan", "/plan <meta>", "Gera e mostra o DAG da meta, sem executar."),
    CommandSpec("run", "/run [meta]", "Planeja e executa; sem meta, executa o último plano."),
    CommandSpec("rag", "/rag index <caminho> | ask <pergunta> | status", "Indexa e consulta seus arquivos locais."),
    CommandSpec("skills", "/skills", "Lista skills nativas, plugins e ferramentas MCP."),
    CommandSpec("login", "/login [claude|codex|gemini]", "Conecta um provedor de IA com a sua chave de API."),
    CommandSpec("logout", "/logout [provedor|all]", "Desconecta um provedor de IA."),
    CommandSpec("model", "/model [provedor [modelo|list]]", "Mostra ou troca o provedor e o modelo de IA ativos."),
    CommandSpec("skill", "/skill <nome> [json]", "Executa uma skill diretamente, sem LLM."),
    CommandSpec("status", "/status", "Subagentes, tokens e aprovações pendentes."),
    CommandSpec("agents", "/agents", "Subagentes registrados e ferramentas MCP."),
    CommandSpec("memory", "/memory", "Sessão, preferências, notas e episódios."),
    CommandSpec("behavior", "/behavior [tone|verbosity|language <v> | rule add <t> | rule rm <n> | reset]", "Consulta ou ajusta o comportamento."),
    CommandSpec("auto", "/auto [on [minutos] | off]", "Modo automático: dispensa aprovação de risco Médio por tempo limitado."),
    CommandSpec("approve", "/approve [id]", "Aprova uma ação suspensa (sem id, lista as pendentes)."),
    CommandSpec("reject", "/reject <id>", "Nega uma ação suspensa."),
    CommandSpec("cancel", "/cancel", "Cancela as tarefas ativas dos subagentes."),
    CommandSpec("clear", "/clear", "Limpa a tela e a memória de trabalho."),
    CommandSpec("help", "/help", "Mostra esta ajuda."),
    CommandSpec("exit", "/exit", "Encerra o Termina.", aliases=("quit",)),
)
COMMAND_NAMES = {spec.name for spec in COMMANDS} | {a for spec in COMMANDS for a in spec.aliases}
SUBCOMMANDS: dict[str, list[str]] = {
    "rag": ["index", "ask", "status"],
    "auto": ["on", "off"],
    "behavior": ["show", "tone", "verbosity", "language", "rule", "reset"],
    "login": [p.id for p in PROVIDERS],
    "logout": [*(p.id for p in PROVIDERS), "all"],
    "model": [p.id for p in PROVIDERS],
}
# Sem nenhum provedor de IA conectado, so estes comandos funcionam (ajuda e login).
ALLOWED_WITHOUT_LOGIN = frozenset({"help", "login", "logout", "status", "skills", "exit", "quit"})
_TOKEN = re.compile(r"\"[^\"]*\"|'[^']*'|\S+")


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    args: list[str]
    raw_args: str  # texto após o nome do comando, sem tokenizar


def tokenize(text: str) -> list[str]:
    """Divide em palavras respeitando aspas; barras invertidas (Windows) ficam intactas."""
    return [
        tok[1:-1] if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'" else tok
        for tok in _TOKEN.findall(text)
    ]


def parse_line(line: str) -> ParsedCommand | None:
    """`/cmd args` -> ParsedCommand; texto comum (ou vazio) -> None."""
    stripped = line.strip()
    if not stripped.startswith("/"):
        return None
    head, _, rest = stripped[1:].partition(" ")
    return ParsedCommand(head.lower(), tokenize(rest), rest.strip())


def suggest(text: str, dynamic: dict[str, Callable[[], list[str]]] | None = None) -> list[str]:
    """Candidatos para a palavra atual: nomes de comando, subcomandos, skills e ids pendentes."""
    if not text.startswith("/"):
        return []
    parts = text.split(" ")
    if len(parts) == 1:
        prefix = parts[0].lower()
        return sorted(f"/{n}" for n in COMMAND_NAMES if f"/{n}".startswith(prefix))
    command, partial, done = parts[0][1:].lower(), parts[-1], len(parts) - 2
    options: list[str] = []
    if command == "behavior" and done == 1 and parts[1] == "rule":
        options = ["add", "rm"]
    elif done == 0:
        options = SUBCOMMANDS.get(command) or (dynamic or {}).get(command, lambda: [])()
    return [o for o in options if o.startswith(partial)]


# ================================================================================== app
class TerminasApp:
    def __init__(self, services: Services, console: Console, reader: LineReader) -> None:
        self.services = services
        self.console = console
        self.reader = reader
        self._last_dag: TaskDAG | None = None
        self._live: Live | None = None
        self._run_task: asyncio.Task[RunReport] | None = None
        self._approval_lock = asyncio.Lock()
        self._busy = False
        services.set_approver(self.approve)
        self._handlers: dict[str, Callable[[ParsedCommand], Any]] = {
            "plan": self._cmd_plan, "run": self._cmd_run, "rag": self._cmd_rag,
            "skills": self._cmd_skills, "skill": self._cmd_skill, "status": self._cmd_status,
            "agents": self._cmd_agents, "memory": self._cmd_memory, "behavior": self._cmd_behavior,
            "auto": self._cmd_auto, "approve": self._cmd_approve, "reject": self._cmd_reject,
            "cancel": self._cmd_cancel, "clear": self._cmd_clear, "help": self._cmd_help,
            "login": self._cmd_login, "logout": self._cmd_logout, "model": self._cmd_model,
        }  # fmt: skip

    # ------------------------------------------------------------------ saída
    def _say(self, text: str, style: str | None = None) -> None:
        self.console.print(Text(redact(text), style=style or ""))  # nunca imprime uma chave

    def info(self, text: str) -> None:
        self._say(text, theme.DIM)

    def ok(self, text: str) -> None:
        self._say(f"✔ {text}", theme.SUCCESS)

    def warn(self, text: str) -> None:
        self._say(f"⚠ {text}", theme.WARNING)

    def error(self, text: str) -> None:
        self._say(f"✖ {text}", theme.ERROR)

    def suggestions(self, text: str) -> list[str]:
        return suggest(
            text,
            {
                "skill": lambda: self.services.skills.names(),
                "approve": self._pending_ids,
                "reject": self._pending_ids,
            },
        )

    def _pending_ids(self) -> list[str]:
        return [d.id for d in self.services.guardrails.pending()]

    def _header(self) -> Panel:
        stats = self.services.telemetry.all_stats()
        return components.header(
            __version__,
            session=self.services.memory.session_id,
            busy=self._busy,
            workers=sum(len(s.active) for s in stats.values()),
            tokens=sum(s.total_tokens for s in stats.values()),
            auto_remaining=self.services.guardrails.auto_remaining,
            model=self.services.auth.active_model(),
        )

    # ------------------------------------------------------------------- loop
    async def run(self) -> None:
        """Loop interativo até /exit, Ctrl+D ou fim da entrada."""
        self.services.memory.start_session("interativa")
        self.console.print(self._header())
        self._startup_notes()
        self._login_notice()
        self.info("Digite /help para ver os comandos. Texto sem '/' vira /run <meta>.")
        while True:
            try:
                line = await self.reader.read_line(PROMPT)
            except KeyboardInterrupt:
                self.info("Use /exit (ou Ctrl+D) para sair.")
                continue
            if line is None or not await self.handle_line(line):
                break
        self.services.memory.end_session()

    def _startup_notes(self) -> None:
        services = self.services
        if services.plugins and services.plugins.errors:
            for file, reason in services.plugins.errors.items():
                self.warn(f"Plugin {file} não carregou: {reason}")
        for server, reason in services.mcp_errors.items():
            self.warn(f"MCP {server}: {reason}")

    def _login_notice(self) -> None:
        if not self.services.auth.is_authenticated:
            self.warn(
                "Nenhum provedor de IA conectado. Use /login para entrar com Claude, Codex ou Gemini "
                "(chave de API). Até lá, o agente fica bloqueado."
            )

    def _blocked_without_login(self) -> None:
        self.warn("O agente está bloqueado: nenhum provedor de IA conectado. Use /login (Claude, Codex ou Gemini).")
        allowed = ", ".join(f"/{name}" for name in sorted(ALLOWED_WITHOUT_LOGIN - {"quit"}))
        self.info(f"Sem login, só funcionam: {allowed}.")

    async def handle_line(self, line: str) -> bool:
        """Processa uma linha. Retorna False quando o usuário pediu para sair."""
        line = line.strip()
        if not line:
            return True
        if detect_secret(line):
            # Uma chave colada como texto comum iria para o histórico e para a memória.
            self.warn(
                "Isso parece uma chave de API. Não guardei nada. Para conectar um provedor, "
                "use /login: a chave é digitada sem aparecer na tela."
            )
            return True
        parsed = parse_line(line)
        if parsed is None:
            parsed = ParsedCommand("run", [], line)  # texto comum = /run <meta>
        if parsed.name in {"exit", "quit"}:
            return False
        handler = self._handlers.get(parsed.name)
        if handler is None:
            self.error(f"Comando desconhecido: /{parsed.name}")
            close = difflib.get_close_matches(parsed.name, sorted(COMMAND_NAMES), n=1)
            if close:
                self.info(f"Você quis dizer /{close[0]}?")
            return True
        if not self.services.auth.is_authenticated and parsed.name not in ALLOWED_WITHOUT_LOGIN:
            self._blocked_without_login()
            return True
        self.services.memory.add_message("user", redact(line))
        try:
            await handler(parsed)
        except Exception as exc:  # um comando com erro nunca derruba o terminal
            self.error(f"/{parsed.name} falhou: {type(exc).__name__}: {exc}")  # _say redige chaves
        return True

    # -------------------------------------------------------------- modal HITL
    @contextmanager
    def _paused_live(self) -> Iterator[None]:
        live = self._live
        if live is not None and live.is_started:
            live.stop()
        try:
            yield
        finally:
            if live is not None and self._live is live and not live.is_started:
                live.start(refresh=True)

    async def approve(self, decision: GuardrailDecision) -> Approval:
        """Modal HITL: bloqueia o fluxo até o usuário responder y/a/n/m.

        Uma por vez (lock): aprovações concorrentes de vários nós esperam sua vez."""
        async with self._approval_lock:
            with self._paused_live():
                self.console.print(components.hitl_box(decision))
                is_shell = decision.request.kind == "shell"
                valid = "y/a/n/m" if is_shell else "y/a/n"
                while True:
                    answer = await self.reader.read_line(f"Sua escolha [{valid}]: ", history=False)
                    if answer is None:
                        self.warn("Sem entrada do usuário: ação negada.")
                        return Approval("deny", reason="sem entrada do usuário")
                    choice = answer.strip().lower()
                    if choice in {"y", "s", "yes", "sim"}:
                        return Approval("once")
                    if choice in {"a", "all"}:
                        return Approval("session")
                    if choice in {"n", "no", "nao", "não"}:
                        return Approval("deny")
                    if choice in {"m", "modify"} and is_shell:
                        command = await self.reader.read_line("Novo comando: ", history=False)
                        if command is None:
                            return Approval("deny", reason="sem entrada do usuário")
                        if command.strip():
                            return Approval("modify", command=command.strip())
                        self.warn("Comando vazio.")
                        continue
                    self.warn(f"Opção inválida. Responda com {valid}.")

    # ---------------------------------------------------------------- comandos
    async def _plan(self, goal: str) -> TaskDAG | None:
        try:
            with self.console.status("Planejando...", spinner_style=theme.PRIMARY):
                dag = await self.services.engine.plan(goal)
        except PlanningError as exc:
            self.error(f"Não consegui montar um plano válido: {exc}")
            return None
        self._last_dag = dag
        return dag

    async def _cmd_plan(self, cmd: ParsedCommand) -> None:
        if not cmd.raw_args:
            self.error("Uso: /plan <meta>")
            return
        dag = await self._plan(cmd.raw_args)
        if dag is not None:
            self.console.print(components.render_dag(dag))
            self.info("Use /run para executar este plano.")

    async def _cmd_run(self, cmd: ParsedCommand) -> None:
        await self.plan_and_run(cmd.raw_args)

    async def plan_and_run(self, goal: str = "") -> RunReport | None:
        """Planeja (ou reaproveita o último plano) e executa com visualização ao vivo."""
        if goal:
            dag = await self._plan(goal)
        else:
            dag = self._last_dag
            if dag is None:
                self.error("Nenhum plano pendente. Use /run <meta> ou /plan <meta>.")
        if dag is None:
            return None

        engine = self.services.engine
        states: dict[str, NodeState] = {}
        outcomes: dict[str, NodeOutcome] = {}

        def on_event(event: RunEvent) -> None:
            states[event.node_id] = event.state
            if event.outcome is not None:
                outcomes[event.node_id] = event.outcome
            if self._live is not None:
                self._live.update(components.render_dag(dag, states, outcomes))

        self._busy = True
        report: RunReport | None = None
        with Live(
            components.render_dag(dag, states, outcomes), console=self.console, refresh_per_second=8
        ) as live:
            self._live = live
            task = asyncio.create_task(engine.run(dag, on_event))
            self._run_task = task
            try:
                # shield: o Ctrl+C não cancela o plano sozinho; `_cancel_run` avisa os agentes primeiro
                report = await asyncio.shield(task)
            except asyncio.CancelledError:
                await self._cancel_run(task)
            finally:
                self._live = None
                self._run_task = None
                self._busy = False
        if report is None:
            return None

        summary = engine.synthesize(report)
        self.console.print(Panel(Text(summary), title="Resultado", title_align="left",
                                 border_style=theme.SUCCESS if report.success else theme.WARNING))  # fmt: skip
        self._record_episode(goal or dag.goal, dag, report, summary)
        return report

    async def _cancel_run(self, task: asyncio.Task[Any]) -> None:
        """Cancela o plano em curso e devolve o controle ao prompt (Ctrl+C)."""
        # Avisa os subagentes ANTES de cancelar o task: ao cancelar, a telemetria esquece as ativas.
        cancelled = await self.services.engine.cancel_all()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            current.uncancel()  # a UI sobrevive ao Ctrl+C e volta ao prompt
        self.warn(f"Execução cancelada ({cancelled} tarefa(s) de subagente interrompida(s)).")

    def _record_episode(self, goal: str, dag: TaskDAG, report: RunReport, summary: str) -> None:
        counts = report.counts()
        stats = self.services.telemetry.all_stats()
        status = "success" if report.success else ("failed" if counts["failed"] else "partial")
        memory = self.services.memory
        memory.record_episode(
            goal,
            status,
            plan=dag.model_dump(mode="json"),
            summary=summary,
            metrics={**counts, "tokens": sum(s.total_tokens for s in stats.values())},
        )
        memory.add_message("assistant", summary)

    async def _cmd_rag(self, cmd: ParsedCommand) -> None:
        sub, _, rest = cmd.raw_args.partition(" ")
        sub, rest = sub.lower(), rest.strip()
        params: dict[str, Any]
        if sub == "index" and rest:
            params = {"op": "index", "path": tokenize(rest)[0]}
        elif sub == "ask" and rest:
            params = {"op": "ask", "question": rest}
        elif sub == "status":
            params = {"op": "status"}
        else:
            self.error("Uso: /rag index <caminho> | /rag ask <pergunta> | /rag status")
            return
        self._print_result(await self.services.skills.invoke("rag-indexer", params))

    # ------------------------------------------------------------ login / logout / model
    async def _pick_provider(
        self, statuses: list[ProviderStatus], question: str, *, allow_all: bool = False
    ) -> ProviderSpec | str | None:
        """Mostra a lista numerada e lê a escolha. Retorna o provedor, "all" ou None (cancelou)."""
        self.console.print(components.providers_table(statuses, numbered=True))
        extra = " ou all" if allow_all else ""
        answer = await self.reader.read_line(
            f"{question} [1-{len(statuses)}, nome{extra}; Enter cancela]: ", history=False
        )
        if answer is None or not answer.strip():
            return None
        choice = answer.strip().lower()
        if allow_all and choice == "all":
            return "all"
        if choice.isdigit():
            number = int(choice)
            if 1 <= number <= len(statuses):
                return statuses[number - 1].spec
            self.error("Número fora da lista.")
            return None
        try:
            return get_provider(choice)
        except UnknownProviderError as exc:
            self.error(str(exc))
            return None

    async def _cmd_login(self, cmd: ParsedCommand) -> None:
        auth = self.services.auth
        if cmd.args:
            try:
                spec = get_provider(cmd.args[0])
            except UnknownProviderError as exc:
                self.error(str(exc))
                return
        else:
            picked = await self._pick_provider(auth.status(), "Qual provedor conectar?")
            if not isinstance(picked, ProviderSpec):
                return
            spec = picked
        if spec.oauth_note:
            self.info(f"Login pelo navegador não disponível: {spec.oauth_note}")
        self.info(f"Crie uma chave de API em: {spec.key_url}")
        self.info("Cole a chave abaixo. Ela não aparece na tela e não fica no histórico. Enter vazio cancela.")
        key = await self.reader.read_secret("Chave de API: ")
        if key is None or not key.strip():
            self.info("Login cancelado.")
            return
        with self.console.status("Validando a chave...", spinner_style=theme.PRIMARY):
            result = await auth.login_api_key(spec, key)
        if not result.ok:
            self.error(result.message)
            self.info("Nada foi salvo.")
            return
        self.ok(f"{spec.label} conectado. Modelo ativo: {result.model}.")
        if result.warning:
            self.warn(f"Atenção: {result.warning}")
        self.info(f"Chave guardada em {auth.store.path}. Use /model para trocar de modelo.")

    async def _cmd_logout(self, cmd: ParsedCommand) -> None:
        auth = self.services.auth
        logged = [status for status in auth.status() if status.logged_in]
        if not logged:
            self.info("Nenhum provedor conectado.")
            return
        if cmd.args:
            target = cmd.args[0].lower()
        else:
            picked = await self._pick_provider(logged, "Sair de qual provedor?", allow_all=True)
            if picked is None:
                return
            target = "all" if picked == "all" else picked.id
        try:
            outcomes = auth.logout(None if target == "all" else target)
        except UnknownProviderError as exc:
            self.error(str(exc))
            return
        for outcome in outcomes:
            label = outcome.provider.label
            if outcome.removed:
                self.ok(f"{label} desconectado (chave removida de {auth.store.path}).")
            elif not outcome.still_in_environment:
                self.info(f"{label} não estava conectado.")
            if outcome.still_in_environment:
                self.warn(
                    f"A variável {outcome.provider.env_var} continua definida no seu ambiente; "
                    "remova-a para sair de verdade."
                )
        if not auth.is_authenticated:
            self.warn("Nenhum provedor conectado: o agente fica bloqueado até o próximo /login.")

    async def _cmd_model(self, cmd: ParsedCommand) -> None:
        auth = self.services.auth
        if not cmd.args:
            self.console.print(components.providers_table([s for s in auth.status() if s.logged_in]))
            active = auth.active_model()
            self.info(f"Modelo ativo: {active}" if active else "Nenhum modelo ativo.")
            self.info("Trocar: /model <provedor> [modelo]. Ver os disponíveis: /model <provedor> list")
            return
        try:
            spec = get_provider(cmd.args[0])
            wanted = cmd.args[1] if len(cmd.args) > 1 else None
            if wanted is None:
                auth.set_active(spec)
                self.ok(f"Provedor ativo: {spec.label} (modelo {auth.model_id(spec)}).")
                return
            with self.console.status("Consultando modelos...", spinner_style=theme.PRIMARY):
                available = await auth.list_models(spec)
            if wanted.lower() == "list":
                if not available:
                    self.warn("Não consegui listar os modelos agora (sem rede?).")
                current = auth.model_id(spec)
                for model in available:
                    self._say(f"{'*' if model == current else ' '} {model}")
                return
            if available and wanted not in available:
                self.error(f"Modelo '{wanted}' não existe em {spec.label}.")
                close = difflib.get_close_matches(wanted, available, n=5)
                if close:
                    self.info("Parecidos: " + ", ".join(close))
                return
            auth.set_active(spec, wanted)
            self.ok(f"Agora usando {spec.label}, modelo {wanted}.")
        except (UnknownProviderError, NotAuthenticatedError) as exc:
            self.error(str(exc))

    async def _cmd_skills(self, cmd: ParsedCommand) -> None:
        self.console.print(components.skills_table(self.services.skills.specs()))
        mcp = self.services.mcp
        if mcp is not None and mcp.servers:
            self.info(f"Servidores MCP conectados: {', '.join(mcp.servers)}")
        elif self.services.config.mcp_servers:
            self.warn("Há servidores MCP configurados, mas nenhum conectou.")

    async def _cmd_skill(self, cmd: ParsedCommand) -> None:
        name, _, payload = cmd.raw_args.partition(" ")
        if not name:
            self.error("Uso: /skill <nome> [json]. Veja os nomes em /skills.")
            return
        try:
            params = json.loads(payload) if payload.strip() else {}
        except json.JSONDecodeError as exc:
            self.error(f"JSON inválido: {exc}")
            return
        if name not in self.services.skills:
            self.error(f"Skill '{name}' não existe.")
            close = difflib.get_close_matches(name, self.services.skills.names(), n=1)
            if close:
                self.info(f"Você quis dizer {close[0]}?")
            return
        self._print_result(await self.services.skills.invoke(name, params))

    def _print_result(self, result: SkillResult) -> None:
        if result.ok:
            self.console.print(Text(result.output or "OK"))
        elif result.status == "awaiting_approval":
            self.warn(f"{result.error}\nUse /approve {result.decision_id} para autorizar.")
        else:
            self.error(result.error or "falhou")

    async def _cmd_status(self, cmd: ParsedCommand) -> None:
        self.console.print(self._header())
        self.console.print(components.providers_table(self.services.auth.status()))
        services = self.services
        rows = await services.skills.invoke("swarm-inspector", {"op": "health"})
        health = {r["agent"]: r["healthy"] for r in rows.data.get("agents", [])}
        stats = {name: services.telemetry.stats(name) for name in health}
        if stats:
            self.console.print(components.telemetry_table(stats, health))
        else:
            self.info("Nenhum subagente registrado.")
        guardrails = services.guardrails
        pending = guardrails.pending()
        if pending:
            self.warn(f"{len(pending)} ação(ões) aguardando aprovação: {', '.join(d.id for d in pending)}")
        if guardrails.auto_active:
            self.warn(f"Modo automático ativo por mais {int(guardrails.auto_remaining // 60)} min.")

    async def _cmd_agents(self, cmd: ParsedCommand) -> None:
        infos = self.services.subagents.list_info()
        if infos:
            self.console.print(components.agents_table(infos))
        else:
            self.info("Nenhum subagente registrado (eles são módulos externos plugáveis).")
        mcp = self.services.mcp
        if mcp is not None and mcp.servers:
            tools = await mcp.list_tools()
            for server in mcp.servers:
                names = [t.name for t in tools if t.server == server]
                self.info(f"MCP {server}: {', '.join(names) or 'sem ferramentas'}")

    async def _cmd_memory(self, cmd: ParsedCommand) -> None:
        memory = self.services.memory
        self.console.print(
            components.memory_table(
                session=memory.session_id,
                context_size=len(memory.get_context()),
                preferences=memory.get_preferences(),
                notes=len(memory.list_long_term("notes")),
                episodes=memory.list_episodes(5),
            )
        )

    async def _cmd_behavior(self, cmd: ParsedCommand) -> None:
        manager = self.services.behavior
        args = cmd.args
        sub = args[0].lower() if args else "show"
        try:
            if sub == "show":
                self.console.print(components.behavior_table(manager.get()))
                return
            if sub in manager.FIELDS and len(args) > 1:
                manager.set_field(sub, " ".join(args[1:]))
                self.ok(f"{sub} atualizado.")
            elif sub == "rule" and len(args) > 2 and args[1] == "add":
                manager.add_rule(cmd.raw_args.split(None, 2)[2])
                self.ok("Regra adicionada.")
            elif sub == "rule" and len(args) == 3 and args[1] == "rm" and args[2].isdigit():
                self.ok(f"Regra removida: {manager.remove_rule(int(args[2]))}")
            elif sub == "reset":
                manager.reset()
                self.ok("Comportamento restaurado para os padrões do config.yaml.")
            else:
                self.error("Uso: /behavior [tone|verbosity|language <valor> | rule add <texto> | rule rm <n> | reset]")
                return
        except ValueError as exc:
            self.error(str(exc))
            return
        self.console.print(components.behavior_table(manager.get()))

    async def _cmd_auto(self, cmd: ParsedCommand) -> None:
        guardrails = self.services.guardrails
        sub = cmd.args[0].lower() if cmd.args else "status"
        if sub == "on":
            minutes = DEFAULT_AUTO_MINUTES
            if len(cmd.args) > 1:
                if not cmd.args[1].isdigit() or not 1 <= int(cmd.args[1]) <= MAX_AUTO_MINUTES:
                    self.error(f"Informe os minutos entre 1 e {MAX_AUTO_MINUTES}.")
                    return
                minutes = int(cmd.args[1])
            guardrails.enable_auto(minutes * 60)
            self.warn(
                f"Modo automático LIGADO por {minutes} min: ações de risco Médio rodam sem perguntar. "
                "Ações de risco CRÍTICO continuam exigindo sua aprovação. Tudo fica no log de auditoria."
            )
        elif sub == "off":
            guardrails.disable_auto()
            self.ok("Modo automático desligado.")
        elif sub == "status":
            if guardrails.auto_active:
                self.warn(f"Modo automático ativo por mais {int(guardrails.auto_remaining // 60)} min.")
            else:
                self.info("Modo automático desligado: toda ação de risco Médio ou Crítico pede aprovação.")
        else:
            self.error("Uso: /auto [on [minutos] | off]")

    async def _cmd_approve(self, cmd: ParsedCommand) -> None:
        guardrails = self.services.guardrails
        if not cmd.args:
            pending = guardrails.pending()
            if not pending:
                self.info("Nenhuma ação aguardando aprovação.")
            for decision in pending:
                self.console.print(components.hitl_box(decision))
            return
        try:
            decision = guardrails.approve(cmd.args[0])
        except GuardrailError as exc:
            self.error(str(exc))
            return
        self.ok(f"Ação {decision.id} aprovada. Reexecute o comando que a originou.")

    async def _cmd_reject(self, cmd: ParsedCommand) -> None:
        if not cmd.args:
            self.error("Uso: /reject <id>")
            return
        try:
            decision = self.services.guardrails.reject(cmd.args[0])
        except GuardrailError as exc:
            self.error(str(exc))
            return
        self.ok(f"Ação {decision.id} negada.")

    async def _cmd_cancel(self, cmd: ParsedCommand) -> None:
        cancelled = await self.services.engine.cancel_all()  # primeiro avisa os subagentes
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
        if cancelled:
            self.warn(f"{cancelled} tarefa(s) de subagente cancelada(s).")
        else:
            self.info("Nada em execução.")

    async def _cmd_clear(self, cmd: ParsedCommand) -> None:
        self.console.clear()
        self.services.memory.clear_working()
        self._last_dag = None

    async def _cmd_help(self, cmd: ParsedCommand) -> None:
        self.console.print(components.help_panel((s.usage, s.summary) for s in COMMANDS))
