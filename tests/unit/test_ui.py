"""Parsing de slash commands, autocomplete, renderização dos componentes e leitura de linhas."""

import asyncio
import builtins

import pytest
from rich.console import Console

from src.core import ActionRequest, GuardrailEngine, NodeOutcome, NodeState, TaskDAG, TaskNode
from src.memory.config import BehaviorConfig
from src.skills import SkillSpec
from src.subagents_bridge import SubagentInfo
from src.subagents_bridge.telemetry import AgentStats
from src.ui import components
from src.ui.cli import COMMAND_NAMES, COMMANDS, parse_line, suggest, tokenize
from src.ui.prompt import PlainReader, PromptToolkitReader, make_reader
from tests.helpers import make_console, output


# ===================================================================== parsing
def test_plain_text_and_blank_are_not_commands():
    assert parse_line("quero um relatório") is None
    assert parse_line("   ") is None and parse_line("") is None
    assert parse_line("explique /plan no meio") is None  # '/' só vale no início


def test_parse_command_name_and_args():
    cmd = parse_line("  /PLAN   Criar   um script  ")
    assert (cmd.name, cmd.args, cmd.raw_args) == ("plan", ["Criar", "um", "script"], "Criar   um script")
    bare = parse_line("/status")
    assert (bare.name, bare.args, bare.raw_args) == ("status", [], "")
    assert parse_line("/").name == ""


def test_tokenize_respects_quotes_and_keeps_windows_backslashes():
    assert tokenize(r'index "C:\Users\ana\meus docs\a.md" depois') == ["index", r"C:\Users\ana\meus docs\a.md", "depois"]
    assert tokenize("ask 'o que é isso?'") == ["ask", "o que é isso?"]
    assert tokenize(r"index C:\proj\docs") == ["index", r"C:\proj\docs"]
    assert tokenize('"') == ['"'] and tokenize("") == []


def test_every_documented_command_exists():
    names = {spec.name for spec in COMMANDS}
    # comandos de docs/6 (6.3) e README
    assert {"plan", "run", "status", "agents", "memory", "approve", "cancel", "clear",
            "rag", "skills", "behavior"} <= names  # fmt: skip
    assert "quit" in COMMAND_NAMES
    assert all(spec.usage.startswith(f"/{spec.name}") and spec.summary for spec in COMMANDS)


# ================================================================== autocomplete
def test_suggest_command_names():
    assert suggest("/") == sorted(f"/{n}" for n in COMMAND_NAMES)
    assert suggest("/ru") == ["/run"]
    assert suggest("/S") == ["/skill", "/skills", "/status"]
    assert suggest("/zzz") == []


def test_suggest_ignores_plain_text():
    assert suggest("") == [] and suggest("rag index") == []


def test_suggest_subcommands():
    assert suggest("/rag ") == ["index", "ask", "status"]
    assert suggest("/rag i") == ["index"]
    assert suggest("/auto o") == ["on", "off"]
    assert suggest("/behavior ") == ["show", "tone", "verbosity", "language", "rule", "reset"]
    assert suggest("/behavior rule ") == ["add", "rm"]
    assert suggest("/behavior rule a") == ["add"]
    assert suggest("/rag index ") == []  # só o 1º argumento tem sugestões


def test_suggest_dynamic_sources():
    dynamic = {"skill": lambda: ["quick-notes", "doc-gen"], "approve": lambda: ["apr-1"]}
    assert suggest("/skill ", dynamic) == ["quick-notes", "doc-gen"]
    assert suggest("/skill q", dynamic) == ["quick-notes"]
    assert suggest("/approve a", dynamic) == ["apr-1"]
    assert suggest("/skill x", dynamic) == []
    assert suggest("/skill ") == []  # sem fonte dinâmica


# ====================================================================== componentes
def render(renderable, width=120) -> str:
    console = Console(width=width, force_terminal=False, color_system=None, record=True)
    console.print(renderable)
    return console.export_text()


def make_dag():
    return TaskDAG(
        goal="Auditar o projeto",
        nodes=[
            TaskNode(id="t1", description="Analisar estrutura", agent="dev-agent"),
            TaskNode(id="t2", description="Rodar [bold]linter[/bold]", agent="research-agent"),
            TaskNode(id="t3", description="Sintetizar relatório", depends_on=["t1", "t2"]),
        ],
    )


def test_dag_view_shows_levels_parallelism_agents_and_dependencies():
    text = render(components.render_dag(make_dag()))
    assert "Plano de Execução (DAG)" in text and "Auditar o projeto" in text
    assert "Etapa 1 — 2 em paralelo" in text and "Etapa 2" in text
    assert "[dev-agent]" in text and "[research-agent]" in text and "[master]" in text
    assert "← t1, t2" in text
    assert text.count("○") == 3 and text.count("PENDENTE") == 3
    assert "Rodar [bold]linter[/bold]" in text  # texto do LLM nunca é interpretado como markup


def test_dag_view_shows_state_symbols_durations_and_errors():
    states = {"t1": NodeState.DONE, "t2": NodeState.RUNNING, "t3": NodeState.SKIPPED}
    outcomes = {
        "t1": NodeOutcome(NodeState.DONE, duration=1.234, agent="dev-agent"),
        "t3": NodeOutcome(NodeState.SKIPPED, error="dependência não concluída: t2"),
    }
    text = render(components.render_dag(make_dag(), states, outcomes))
    assert "✔ t1" in text and "● t2" in text and "⊘ t3" in text
    assert "(1.2s)" in text and "RODANDO" in text and "CONCLUÍDO" in text
    assert "dependência não concluída: t2" in text
    failed = render(components.render_dag(make_dag(), {"t1": NodeState.FAILED}, {"t1": NodeOutcome(NodeState.FAILED, error="boom")}))
    assert "✖ t1" in failed and "FALHOU" in failed and "boom" in failed


def test_header_states():
    idle = render(components.header("0.1.0", session="42", busy=False, workers=2, tokens=2410, auto_remaining=0))
    assert "Termina's Agent v0.1.0" in idle and "[Session: #42]" in idle
    assert "Master: IDLE" in idle and "Active Workers: 2" in idle and "Total Tokens: 2,410" in idle
    assert "Auto: off" in idle
    busy = render(components.header("0.1.0", session=None, busy=True, workers=0, tokens=0, auto_remaining=125))
    assert "BUSY" in busy and "Auto: 2min" in busy and "Session" not in busy


def test_hitl_box_for_shell_command():
    decision = GuardrailEngine().evaluate(
        ActionRequest.shell("git checkout -B fix && git clean -fd", agent="DevAgent", justification="Isolar os testes")
    )
    text = " ".join(render(components.hitl_box(decision)).split())  # ignora o alinhamento em colunas
    for expected in ("ALERTA DE SEGURANÇA: APROVAÇÃO NECESSÁRIA", "Agente Solicitante: DevAgent", "Risco: Crítico",
                     "git-clean-force", "Comando de shell", "git checkout -B fix && git clean -fd",
                     "Justificativa: Isolar os testes", decision.id, "Deseja autorizar a execução desta ação?",
                     "[y] Aprovar uma vez", "[a] Aprovar igual nesta sessão", "[n] Negar",
                     "[m] Modificar comando"):  # fmt: skip
        assert expected in text, expected


def test_hitl_box_for_tool_has_no_modify_option():
    decision = GuardrailEngine().evaluate(ActionRequest.tool_call("write_file", path="a.txt"))
    text = " ".join(render(components.hitl_box(decision)).split())
    assert "Chamada de ferramenta" in text and "write_file(path='a.txt')" in text
    assert "Risco: Médio" in text and "[m]" not in text and "Justificativa" not in text


def test_help_panel_lists_commands():
    text = render(components.help_panel((s.usage, s.summary) for s in COMMANDS))
    for spec in COMMANDS:
        assert spec.usage in text.replace("\n", " ") or spec.name in text
    assert "Ctrl+C" in text


def test_tables():
    stats = {"dev": AgentStats(calls=3, successes=2, failures=1, prompt_tokens=100, completion_tokens=50,
                               total_latency=0.9, active={"t1"}), "ghost": AgentStats()}  # fmt: skip
    text = render(components.telemetry_table(stats, {"dev": True, "ghost": False}))
    assert "dev" in text and "OK" in text and "OFF" in text and "300 ms" in text and "150" in text
    assert "—" in render(components.telemetry_table(stats))  # sem dados de saúde

    spec = SkillSpec(name="quick-notes", description="Notas", category="Memória", source="native", input_schema={})
    assert "quick-notes" in render(components.skills_table([spec]))

    info = SubagentInfo(name="dev-agent", capabilities=["code", "test"], recommended_models=["m1"], description="Dev")
    agents = render(components.agents_table([info, SubagentInfo(name="bare", capabilities=["x"])]))
    assert "code, test" in agents and "m1" in agents and "bare" in agents

    memory = render(components.memory_table(
        session="abc", context_size=4, preferences={"theme": "dark"}, notes=2,
        episodes=[{"id": 7, "status": "success", "goal": "indexar docs"}]))  # fmt: skip
    assert "abc" in memory and "preferência: theme" in memory and "episódio #7 (success)" in memory

    assert "nenhuma" in render(components.behavior_table(BehaviorConfig()))
    rules = render(components.behavior_table(BehaviorConfig(rules=["sem emoji"])))
    assert "regra 1" in rules and "sem emoji" in rules


def test_rendering_is_plain_text_safe_for_any_terminal():
    """Sem cor nem terminal real: mesma estrutura em Windows Terminal e terminais Linux."""
    console = make_console(width=100)
    console.print(components.render_dag(make_dag()))
    console.print(components.header("0.1.0", session="1", busy=False, workers=0, tokens=0, auto_remaining=0))
    text = output(console)
    assert "\x1b[" not in text and all(len(line) <= 100 for line in text.splitlines())


# ========================================================================== leitor
async def test_plain_reader_reads_and_signals_eof(monkeypatch):
    answers = iter(["olá"])

    def fake_input(prompt):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(builtins, "input", fake_input)
    reader = PlainReader()
    assert await reader.read_line("> ") == "olá"
    assert await reader.read_line("> ", history=False) is None


def reader_with_pipe(pipe, suggester=lambda text: [], history_file=None):
    from prompt_toolkit.output import DummyOutput

    return PromptToolkitReader(suggester, history_file, input=pipe, output=DummyOutput())


async def test_prompt_toolkit_reader_reads_lines_and_eof():
    from prompt_toolkit.input import create_pipe_input

    with create_pipe_input() as pipe:
        reader = reader_with_pipe(pipe)
        pipe.send_text("/status\r")
        assert await reader.read_line("> ") == "/status"
        pipe.send_text("y\r")
        assert await reader.read_line("Sua escolha: ", history=False) == "y"
        pipe.send_text("\x04")  # Ctrl+D
        assert await reader.read_line("> ") is None


async def test_prompt_toolkit_history_persists_and_modal_answers_do_not_pollute_it(tmp_path):
    from prompt_toolkit.input import create_pipe_input

    history = tmp_path / "nested" / "history"
    with create_pipe_input() as pipe:
        reader = reader_with_pipe(pipe, history_file=history)
        pipe.send_text("/help\r")
        await reader.read_line("> ")
        pipe.send_text("y\r")
        await reader.read_line("? ", history=False)
    saved = history.read_text(encoding="utf-8")
    assert "/help" in saved and "\ny" not in saved and "+y" not in saved


def test_completer_adapter_replaces_only_the_current_word():
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document
    from prompt_toolkit.input import create_pipe_input

    with create_pipe_input() as pipe:
        reader = reader_with_pipe(pipe, suggester=lambda text: suggest(text))
        complete = lambda text: list(reader.completer.get_completions(Document(text), CompleteEvent()))  # noqa: E731
        commands = complete("/ru")
        assert [c.text for c in commands] == ["/run"] and commands[0].start_position == -3
        rag = complete("/rag i")
        assert [c.text for c in rag] == ["index"] and rag[0].start_position == -1
        after_space = complete("/rag ")
        assert {c.text for c in after_space} == {"index", "ask", "status"} and after_space[0].start_position == 0
        assert complete("texto livre") == []


def test_make_reader_falls_back_to_plain_when_not_interactive(monkeypatch):
    assert isinstance(make_reader(lambda t: [], None), PlainReader)  # pytest: stdin não é TTY

    class FakeTty:
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", FakeTty())
    sentinel = object()
    monkeypatch.setattr("src.ui.prompt.PromptToolkitReader", lambda *a, **k: sentinel)
    assert make_reader(lambda t: [], None) is sentinel

    def broken(*a, **k):
        raise RuntimeError("console sem suporte")

    monkeypatch.setattr("src.ui.prompt.PromptToolkitReader", broken)
    assert isinstance(make_reader(lambda t: [], None), PlainReader)




def test_hitl_box_without_rule_reasons_omits_the_reasons_row():
    from src.core import RiskLevel

    engine = GuardrailEngine(approval_threshold=RiskLevel.LOW)  # até leitura pede aprovação
    decision = engine.evaluate(ActionRequest.tool_call("read_file", path="a.txt"))
    assert decision.reasons == () and decision.awaiting
    text = " ".join(render(components.hitl_box(decision)).split())
    assert "Risco: Baixo" in text and "Motivos" not in text


async def test_tab_key_completes_a_command_in_a_real_prompt_session():
    from prompt_toolkit.input import create_pipe_input

    with create_pipe_input() as pipe:
        reader = reader_with_pipe(pipe, suggester=lambda text: suggest(text))
        async def type_then_enter(text):
            pipe.send_text(text)  # termina em Tab
            await asyncio.sleep(0.3)  # a completion roda em background; o usuário espera o resultado
            pipe.send_text("\r")

        asyncio.get_running_loop().create_task(type_then_enter("/ru\t"))
        assert await reader.read_line("> ") == "/run"  # Tab completa o único candidato
        asyncio.get_running_loop().create_task(type_then_enter("/rag i\t"))
        assert await reader.read_line("> ") == "/rag index"  # completa só a palavra atual


async def test_arrow_up_recalls_history_across_sessions(tmp_path):
    from prompt_toolkit.input import create_pipe_input

    history = tmp_path / "history"
    with create_pipe_input() as pipe:
        first = reader_with_pipe(pipe, history_file=history)
        pipe.send_text("/status\r")
        assert await first.read_line("> ") == "/status"
    with create_pipe_input() as pipe:  # "nova execução": o histórico veio do arquivo
        second = reader_with_pipe(pipe, history_file=history)
        pipe.send_text("\x1b[A\r")  # seta para cima + Enter
        assert await second.read_line("> ") == "/status"
