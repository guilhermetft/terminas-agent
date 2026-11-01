"""Terminal interativo: comandos, loop, modal HITL, execução ao vivo e cancelamento."""

import asyncio
import sys
from pathlib import Path

import pytest

from src.core import Approval, NodeState
from src.skills import CommandResult
from src.skills.registry import PluginReport
from src.ui.cli import COMMANDS, PROMPT
from tests.helpers import (
    FakeLLM,
    FakeRunner,
    ForbiddenLLM,
    ScriptedReader,
    Worker,
    build_app,
    make_console,
    output,
    plan_json,
)

PY = f'"{sys.executable}"'
PLAN_AB = plan_json(("t1", "dev-agent"), ("t2", "dev-agent", "t1"))


@pytest.fixture
async def make_app(tmp_path):
    created = []

    async def factory(*reader_lines, **kwargs):
        app, services, reader = await build_app(tmp_path, *reader_lines, **kwargs)
        created.append(services)
        return app, services, reader

    yield factory
    from src.ui.services import stop_services

    for services in created:
        await stop_services(services)


# ======================================================================== dispatch
async def test_every_documented_command_has_a_handler(make_app):
    app, *_ = await make_app()
    assert set(app._handlers) | {"exit"} == {spec.name for spec in COMMANDS}


async def test_blank_line_is_ignored_and_exit_variants_stop(make_app):
    app, *_ = await make_app()
    assert await app.handle_line("   ") is True
    assert await app.handle_line("/exit") is False
    assert await app.handle_line("/QUIT") is False
    assert output(app.console) == ""


async def test_unknown_command_suggests_the_closest_one(make_app):
    app, *_ = await make_app()
    assert await app.handle_line("/stauts") is True
    text = output(app.console)
    assert "Comando desconhecido: /stauts" in text and "Você quis dizer /status?" in text
    await app.handle_line("/qqqqqq")
    assert text.count("Você quis dizer") == 1  # sem palpite para algo muito diferente


async def test_a_crashing_command_never_kills_the_terminal(make_app):
    app, *_ = await make_app()

    async def broken(cmd):
        raise RuntimeError("bug interno")

    app._handlers["memory"] = broken
    assert await app.handle_line("/memory") is True
    assert "/memory falhou: RuntimeError: bug interno" in output(app.console)


async def test_help_lists_every_command(make_app):
    app, *_ = await make_app()
    await app.handle_line("/help")
    text = "".join(output(app.console).replace("│", " ").split())  # sem bordas nem quebras de linha
    for spec in COMMANDS:
        assert "".join(spec.summary.split()) in text, spec.name


# ============================================== comandos determinísticos (sem LLM)
async def test_deterministic_commands_never_call_the_llm(make_app, tmp_path):
    (tmp_path / "doc.txt").write_text("a porta padrão é 8080\n", encoding="utf-8")
    app, services, _ = await make_app(llm=ForbiddenLLM(), workers=(Worker("dev-agent"),))
    for line in [
        "/help", "/skills", "/status", "/agents", "/memory", "/behavior", "/behavior tone seco",
        "/behavior rule add sem emoji", "/behavior rule rm 1", "/behavior reset", "/auto", "/auto on 5",
        "/auto off", "/approve", "/reject apr-x", "/cancel", "/clear", "/rag status", "/rag index doc.txt",
        '/rag ask porta padrão', '/skill quick-notes {"text": "ideia"}', "/run", "/foo",
    ]:  # fmt: skip
        assert await app.handle_line(line) is True, line
    assert ForbiddenLLM.calls == []  # nenhum AssertionError escapou: o LLM nunca foi chamado


async def test_skills_and_agents_and_memory_views(make_app):
    app, services, _ = await make_app(workers=(Worker("dev-agent"),))
    await app.handle_line('/skill quick-notes {"text": "comprar café", "tags": ["pendente"]}')
    for line in ("/skills", "/agents", "/memory"):
        await app.handle_line(line)
    text = output(app.console)
    assert "quick-notes" in text and "shell-safe-runner" in text  # 9 nativas
    assert "dev-agent" in text and "Agente de teste" in text
    assert "Notas salvas" in text and "Nota n-" in text


async def test_agents_command_lists_mcp_tools(make_app):
    class FakeMcp:
        servers = ["srv"]

        async def close(self):
            pass

        async def list_tools(self, server=None):
            from src.skills import McpTool

            return [McpTool(server="srv", name="ping"), McpTool(server="srv", name="pong")]

    app, services, _ = await make_app()
    services.mcp = FakeMcp()
    await app.handle_line("/agents")
    await app.handle_line("/skills")
    text = output(app.console)
    assert "Nenhum subagente registrado" in text and "MCP srv: ping, pong" in text
    assert "Servidores MCP conectados: srv" in text


async def test_skills_warns_when_configured_mcp_servers_did_not_connect(make_app, tmp_path):
    from src.skills import McpServerConfig
    from tests.helpers import make_config

    config = make_config(tmp_path, mcp_servers=[McpServerConfig(name="x", command="nao-existe")])
    app, services, _ = await make_app(config=config)
    await app.handle_line("/skills")
    assert "nenhum conectou" in output(app.console)
    assert "x" in services.mcp_errors  # a falha de conexão foi registrada, sem derrubar o app


# =================================================================== /skill e /rag
async def test_skill_command_validation_and_results(make_app):
    app, *_ = await make_app()
    await app.handle_line("/skill")
    await app.handle_line("/skill quick-notes {nao e json}")
    await app.handle_line("/skill quick-note")
    await app.handle_line("/skill quick-notes")  # sem texto -> erro de parâmetros do skill
    await app.handle_line('/skill quick-notes {"op": "list"}')
    text = output(app.console)
    assert "Uso: /skill <nome> [json]" in text and "JSON inválido" in text
    assert "Skill 'quick-note' não existe" in text and "Você quis dizer quick-notes?" in text
    assert "'add' exige 'text'" in text and "Nenhuma nota encontrada." in text


async def test_rag_commands(make_app, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "manual.txt").write_text("A porta padrão do serviço é 8080.\n\nOutro assunto qualquer.\n", encoding="utf-8")
    app, *_ = await make_app()
    await app.handle_line("/rag")
    await app.handle_line("/rag index")
    await app.handle_line("/rag status")
    await app.handle_line('/rag index "docs"')
    await app.handle_line("/rag ask qual a porta padrão do serviço?")
    await app.handle_line("/rag index ../fora")
    text = output(app.console)
    assert text.count("Uso: /rag index") == 2 and "0 documento(s)" in text
    assert "1 arquivo(s) indexado(s)" in text
    assert "manual.txt:1" in text and "8080" in text
    assert "fora do diretório" in text


# ==================================================================== plan / run
async def test_plan_shows_dag_and_run_executes_the_last_plan(make_app):
    worker = Worker("dev-agent")
    app, services, _ = await make_app(llm=FakeLLM(PLAN_AB), workers=(worker,))
    await app.handle_line("/plan escrever e testar")
    text = output(app.console)
    assert "Plano de Execução (DAG)" in text and "t1" in text and "Use /run para executar" in text
    assert worker.tasks == []  # /plan não executa

    await app.handle_line("/run")  # sem meta: reaproveita o plano
    text = output(app.console)
    assert [t.goal for t in worker.tasks] == ["passo t1", "passo t2"]
    assert "Resultado" in text and "✔ t1: passo t1 — feito: passo t1" in text
    assert "2 concluída(s), 0 com falha, 0 ignorada(s)." in text
    assert services.telemetry.stats("dev-agent").calls == 2


async def test_run_with_goal_and_plain_text_plan_and_execute(make_app):
    llm = FakeLLM(plan_json(("t1", "dev-agent")), plan_json(("t1", "dev-agent")))
    worker = Worker("dev-agent")
    app, *_ = await make_app(llm=llm, workers=(worker,))
    await app.handle_line("/run fazer a coisa")
    await app.handle_line("outra coisa sem barra")  # texto comum = /run <meta>
    assert len(llm.calls) == 2 and len(worker.tasks) == 2
    assert "Meta: outra coisa sem barra" in llm.calls[1][1]["content"]


async def test_run_records_episode_and_session_messages(make_app):
    app, services, _ = await make_app(llm=FakeLLM(PLAN_AB), workers=(Worker("dev-agent"),))
    await app.handle_line("/run fazer tudo")
    (episode,) = services.memory.list_episodes()
    assert episode["goal"] == "fazer tudo" and episode["status"] == "success"
    assert episode["metrics"]["done"] == 2 and episode["metrics"]["tokens"] == 30
    assert [n["id"] for n in episode["plan"]["nodes"]] == ["t1", "t2"]
    roles = [m["role"] for m in services.memory.get_context()]
    assert roles == ["user", "assistant"] and "✔ t1" in services.memory.get_context()[1]["content"]


async def test_failed_and_skipped_nodes_are_reported(make_app):
    plan = plan_json(("t1", "bad"), ("t2", "bad", "t1"), ("t3", None))
    app, services, _ = await make_app(llm=FakeLLM(plan), workers=(Worker("bad", mode="fail"),))
    report = await app.plan_and_run("meta")
    text = output(app.console)
    assert report is not None and not report.success
    assert "✖ t1: passo t1 — não deu" in text and "⊘ t2" in text and "nenhum executor disponível para 'master'" in text
    assert services.memory.list_episodes()[0]["status"] == "failed"


async def test_partial_status_when_only_skips(make_app):
    app, services, _ = await make_app(llm=FakeLLM(plan_json(("t1", None))))
    report = await app.plan_and_run("meta")
    assert report is not None and services.memory.list_episodes()[0]["status"] == "partial"


async def test_planning_problems_are_reported_without_crashing(make_app):
    app, *_ = await make_app(llm=FakeLLM(*["lixo"] * 6))
    await app.handle_line("/plan algo")
    await app.handle_line("/plan")
    await app.handle_line("/run")
    assert await app.plan_and_run("outra") is None
    text = output(app.console)
    assert "Não consegui montar um plano válido" in text
    assert "Uso: /plan <meta>" in text and "Nenhum plano pendente" in text


# ==================================================================== cancelamento
async def test_ctrl_c_cancels_the_run_and_the_terminal_survives(make_app):
    worker = Worker("dev-agent", delay=10)
    app, services, _ = await make_app(llm=FakeLLM(plan_json(("t1", "dev-agent"))), workers=(worker,))
    pending = asyncio.create_task(app.handle_line("/run lento"))
    for _ in range(200):  # espera o nó começar
        await asyncio.sleep(0.01)
        if services.telemetry.active_tasks("dev-agent"):
            break
    active = services.telemetry.active_tasks("dev-agent")
    assert active and app._busy and app._run_task is not None

    pending.cancel()  # equivale ao SIGINT cancelando o task principal
    assert await asyncio.wait_for(pending, 5) is True  # volta ao prompt, não encerra
    assert worker.cancelled == active
    assert not app._busy and app._run_task is None and app._live is None
    assert "Execução cancelada (1 tarefa(s)" in output(app.console)
    assert services.telemetry.active_tasks("dev-agent") == []
    assert services.memory.list_episodes() == []  # execução cancelada não vira episódio
    assert await app.handle_line("/status") is True  # e o app continua utilizável


async def test_cancel_command(make_app):
    worker = Worker("dev-agent")
    app, services, _ = await make_app(workers=(worker,))
    await app.handle_line("/cancel")
    assert "Nada em execução." in output(app.console)
    services.telemetry.record_start("dev-agent", "tsk-9")
    await app.handle_line("/cancel")
    assert worker.cancelled == ["tsk-9"] and "1 tarefa(s) de subagente cancelada(s)" in output(app.console)


async def test_cancel_command_also_cancels_an_in_flight_run(make_app):
    worker = Worker("dev-agent", delay=10)
    app, services, _ = await make_app(llm=FakeLLM(plan_json(("t1", "dev-agent"))), workers=(worker,))
    running = asyncio.create_task(app.handle_line("/run lento"))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if app._run_task is not None and services.telemetry.active_tasks("dev-agent"):
            break
    await app.handle_line("/cancel")
    assert await asyncio.wait_for(running, 5) is True
    assert "Execução cancelada" in output(app.console)


# =================================================================== modal HITL
def shell_call(command):
    return f"/skill shell-safe-runner {{\"command\": {command!r}}}".replace("'", '"')


async def hitl(make_app, answers, command="rm -rf /tmp/x", runner=None):
    runner = runner or FakeRunner(CommandResult(0, "executou\n"))
    app, services, reader = await make_app(*answers, runner=runner)
    await app.handle_line(f'/skill shell-safe-runner {{"command": "{command}"}}')
    return app, services, reader, runner


async def test_hitl_yes_runs_the_command_once(make_app):
    app, services, reader, runner = await hitl(make_app, ["y"])
    text = output(app.console)
    assert "ALERTA DE SEGURANÇA" in text and "rm -rf /tmp/x" in text and "executou" in text
    assert [c["command"] for c in runner.calls] == ["rm -rf /tmp/x"]
    assert reader.prompts == [("Sua escolha [y/a/n/m]: ", False)]  # modal fora do histórico


@pytest.mark.parametrize("answer", ["s", "SIM", " yes "])
async def test_hitl_accepts_portuguese_and_long_forms(make_app, answer):
    _, _, _, runner = await hitl(make_app, [answer])
    assert len(runner.calls) == 1


@pytest.mark.parametrize("answer", ["n", "no", "não", "nao"])
async def test_hitl_no_blocks_the_command(make_app, answer):
    app, _, _, runner = await hitl(make_app, [answer])
    assert runner.calls == [] and "Ação não autorizada" in output(app.console)


async def test_hitl_eof_denies(make_app):
    app, _, _, runner = await hitl(make_app, [])
    assert runner.calls == [] and "Sem entrada do usuário: ação negada." in output(app.console)


async def test_hitl_invalid_answer_reprompts_until_valid(make_app):
    app, _, reader, runner = await hitl(make_app, ["talvez", "", "y"])
    assert len(reader.prompts) == 3 and len(runner.calls) == 1
    assert output(app.console).count("Opção inválida. Responda com y/a/n/m.") == 2


async def test_hitl_session_choice_skips_the_next_prompt(make_app):
    app, services, reader, runner = await hitl(make_app, ["a"])
    await app.handle_line('/skill shell-safe-runner {"command": "rm -rf /tmp/x"}')
    assert len(runner.calls) == 2 and len(reader.prompts) == 1  # a 2ª vez não perguntou
    await app.handle_line('/skill shell-safe-runner {"command": "rm -rf /tmp/outro"}')
    assert len(reader.prompts) == 2  # outro comando crítico volta a perguntar (e é negado: EOF)
    assert len(runner.calls) == 2


async def test_hitl_modify_runs_the_replacement_command(make_app):
    app, _, reader, runner = await hitl(make_app, ["m", "echo seguro"])
    assert [c["command"] for c in runner.calls] == ["echo seguro"]
    assert [p[0] for p in reader.prompts] == ["Sua escolha [y/a/n/m]: ", "Novo comando: "]


async def test_hitl_modify_with_empty_command_asks_again(make_app):
    app, _, _, runner = await hitl(make_app, ["m", "  ", "y"])
    assert "Comando vazio." in output(app.console) and [c["command"] for c in runner.calls] == ["rm -rf /tmp/x"]


async def test_hitl_modify_then_eof_denies(make_app):
    _, _, _, runner = await hitl(make_app, ["m"])
    assert runner.calls == []


async def test_hitl_for_a_tool_has_no_modify_option(make_app, tmp_path):
    app, _, reader = await make_app("m", "n")
    await app.handle_line('/skill doc-gen {"format": "md", "content": "x", "output": "a"}')
    assert [p[0] for p in reader.prompts] == ["Sua escolha [y/a/n]: ", "Sua escolha [y/a/n]: "]
    assert "Opção inválida. Responda com y/a/n." in output(app.console)
    assert not (tmp_path / "a.md").exists()


async def test_approvals_are_serialized_so_prompts_never_interleave(make_app):
    app, services, _ = await make_app()
    log = []

    class SlowReader:
        async def read_line(self, prompt, *, history=True):
            log.append("start")
            await asyncio.sleep(0.03)
            log.append("end")
            return "y"

    app.reader = SlowReader()
    from src.core import ActionRequest

    decisions = [services.guardrails.evaluate(ActionRequest.shell(f"rm -rf /tmp/{i}")) for i in range(3)]
    answers = await asyncio.gather(*(app.approve(d) for d in decisions))
    assert all(a == Approval("once") for a in answers)
    assert log == ["start", "end"] * 3


async def test_modal_pauses_and_restores_the_live_view(make_app):
    app, services, _ = await make_app("y")

    class FakeLive:
        def __init__(self):
            self.is_started, self.calls = True, []

        def stop(self):
            self.is_started = False
            self.calls.append("stop")

        def start(self, refresh=False):
            self.is_started = True
            self.calls.append("start")

    live = FakeLive()
    app._live = live
    from src.core import ActionRequest

    await app.approve(services.guardrails.evaluate(ActionRequest.shell("rm -rf /tmp/x")))
    assert live.calls == ["stop", "start"] and live.is_started
    app._live = None  # sem Live ativa: nada a pausar
    app.reader = ScriptedReader("y")
    await app.approve(services.guardrails.evaluate(ActionRequest.shell("rm -rf /tmp/y")))


# ================================================================== /auto e aprovações
async def test_auto_mode_flow(make_app):
    runner = FakeRunner(CommandResult(0, "ok\n"), CommandResult(0, "ok\n"))
    app, services, reader = await make_app(runner=runner)
    await app.handle_line("/auto")
    assert "Modo automático desligado" in output(app.console)

    await app.handle_line("/auto on 10")
    assert services.guardrails.auto_active and 590 < services.guardrails.auto_remaining <= 600
    assert "CRÍTICO continuam exigindo" in output(app.console)

    await app.handle_line('/skill shell-safe-runner {"command": "rm arquivo.txt"}')  # Médio: sem pergunta
    assert len(runner.calls) == 1 and reader.prompts == []
    await app.handle_line('/skill shell-safe-runner {"command": "rm -rf /tmp/x"}')  # Crítico: pergunta (EOF nega)
    assert len(reader.prompts) == 1 and len(runner.calls) == 1

    await app.handle_line("/status")
    assert "Modo automático ativo" in output(app.console)
    await app.handle_line("/auto status")
    await app.handle_line("/auto off")
    assert not services.guardrails.auto_active and "desligado." in output(app.console)

    events = [e["event"] for e in services.audit.read()]
    assert "auto_mode_on" in events and "auto_mode_off" in events  # tudo auditado


async def test_auto_mode_default_minutes_and_validation(make_app):
    app, services, _ = await make_app()
    await app.handle_line("/auto on")
    assert 1790 < services.guardrails.auto_remaining <= 1800
    for bad in ("/auto on 0", "/auto on 999", "/auto on abc", "/auto talvez"):
        await app.handle_line(bad)
    text = output(app.console)
    assert text.count("Informe os minutos entre 1 e 240.") == 3 and "Uso: /auto [on [minutos] | off]" in text
    assert services.guardrails.auto_active  # entradas inválidas não alteraram o estado


async def test_approve_and_reject_commands(make_app):
    from src.core import ActionRequest

    app, services, _ = await make_app()
    await app.handle_line("/approve")
    assert "Nenhuma ação aguardando aprovação." in output(app.console)
    first = services.guardrails.evaluate(ActionRequest.shell("rm -rf /tmp/a"))
    second = services.guardrails.evaluate(ActionRequest.shell("rm -rf /tmp/b"))
    await app.handle_line("/approve")
    assert "rm -rf /tmp/a" in output(app.console) and "rm -rf /tmp/b" in output(app.console)

    await app.handle_line(f"/approve {first.id}")
    await app.handle_line(f"/reject {second.id}")
    assert first.allowed and second.state.value == "REJECTED"
    assert f"Ação {first.id} aprovada" in output(app.console) and f"Ação {second.id} negada" in output(app.console)
    for line in ("/approve apr-nada", f"/approve {first.id}", "/reject apr-nada", "/reject"):
        await app.handle_line(line)
    text = output(app.console)
    assert "não encontrada" in text and "já está APPROVED" in text and "Uso: /reject <id>" in text


async def test_skill_awaiting_approval_without_approver_shows_hint(make_app):
    app, services, _ = await make_app()
    services.set_approver(None)
    await app.handle_line('/skill doc-gen {"format": "md", "content": "x", "output": "a"}')
    pending = services.guardrails.pending()
    assert len(pending) == 1
    assert f"Use /approve {pending[0].id} para autorizar." in output(app.console)


# ==================================================================== status/clear/behavior
async def test_status_shows_agents_health_and_pending(make_app):
    from src.core import ActionRequest

    app, services, _ = await make_app(workers=(Worker("dev-agent"),))
    services.telemetry.record_start("dev-agent", "t1")
    services.guardrails.evaluate(ActionRequest.shell("rm -rf /tmp/a"))
    await app.handle_line("/status")
    text = output(app.console)
    assert "Master: IDLE" in text and "Active Workers: 1" in text
    assert "dev-agent" in text and "OK" in text and "aguardando aprovação: apr-" in text


async def test_clear_resets_screen_working_memory_and_last_plan(make_app):
    app, services, _ = await make_app(llm=FakeLLM(PLAN_AB), workers=(Worker("dev-agent"),))
    await app.handle_line("/plan meta")
    services.memory.set_working("k", 1)
    await app.handle_line("/clear")
    assert services.memory.get_working("k") is None and app._last_dag is None
    await app.handle_line("/run")
    assert "Nenhum plano pendente" in output(app.console)


async def test_behavior_command_end_to_end(make_app):
    app, services, _ = await make_app()
    for line in ("/behavior", "/behavior tone direto", "/behavior language en", "/behavior rule add nunca usar emoji",
                 "/behavior rule rm 1", "/behavior rule rm 9", "/behavior tone", "/behavior rule", "/behavior cor azul",
                 "/behavior reset"):  # fmt: skip
        await app.handle_line(line)
    text = output(app.console)
    assert "tone atualizado." in text and "Regra adicionada." in text and "Regra removida: nunca usar emoji" in text
    assert "Regra 9 não existe" in text and text.count("Uso: /behavior") == 3
    assert "restaurado para os padrões" in text
    assert services.behavior.get() == services.behavior.defaults


# ========================================================================== loop
async def test_interactive_loop_runs_commands_until_exit(make_app):
    app, services, reader = await make_app("/help", "", "/status", "/exit", "/nunca-chega")
    await app.run()
    text = output(app.console)
    assert "Termina's Agent v" in text and "Digite /help" in text and "Comandos" in text
    assert [p[0] for p in reader.prompts] == [PROMPT] * 4 and reader.lines == ["/nunca-chega"]
    assert services.memory.session_id is None  # sessão encerrada ao sair


async def test_interactive_loop_ends_on_eof_and_survives_keyboard_interrupt(make_app):
    app, *_ = await make_app("/help", KeyboardInterrupt(), "/status")
    await app.run()  # termina por EOF depois das 3 linhas
    text = output(app.console)
    assert "Use /exit (ou Ctrl+D) para sair." in text and "Active Workers" in text


async def test_startup_warnings_for_plugins_and_mcp(make_app):
    app, services, _ = await make_app()
    services.plugins = PluginReport(errors={"quebrado.py": "SyntaxError: x"})
    services.mcp_errors = {"google-workspace": "não respondeu"}
    await app.run()
    text = output(app.console)
    assert "Plugin quebrado.py não carregou: SyntaxError: x" in text
    assert "MCP google-workspace: não respondeu" in text


async def test_app_autocomplete_uses_live_skills_and_pending_ids(make_app):
    from src.core import ActionRequest

    app, services, _ = await make_app()
    assert "quick-notes" in app.suggestions("/skill q") and app.suggestions("/skill q") == ["quick-notes"]
    decision = services.guardrails.evaluate(ActionRequest.shell("rm -rf /tmp/x"))
    assert app.suggestions("/approve a") == [decision.id]
    assert app.suggestions("/reject ") == [decision.id]
    assert app.suggestions("/ru") == ["/run"]


async def test_unknown_skill_without_close_match_gives_no_suggestion(make_app):
    app, *_ = await make_app()
    await app.handle_line("/skill zzzzzzzz")
    text = output(app.console)
    assert "Skill 'zzzzzzzz' não existe." in text and "Você quis dizer" not in text
