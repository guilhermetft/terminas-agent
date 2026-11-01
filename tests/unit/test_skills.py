"""Testes do catálogo de skills (CT-04; RF-06, US-04): registry, base, plugins e skills locais."""

import json
import sys
from pathlib import Path

import pytest

from src.core import Approval, GuardrailEngine
from src.memory import MemoryEngine
from src.rag import HashingEmbedder, RAGEngine
from src.skills import (
    SKILL_TOOL_RISKS,
    CommandResult,
    DuplicateSkillError,
    Skill,
    SkillContext,
    SkillError,
    SkillResult,
    SkillsRegistry,
    SubprocessRunner,
    UnknownSkillError,
    build_default_registry,
)

NATIVE_NAMES = [
    "issue-flow", "doc-gen", "google-workspace", "deep-search", "swarm-inspector",
    "daily-briefing", "rag-indexer", "shell-safe-runner", "quick-notes",
]  # fmt: skip
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "manual.txt"
PY = f'"{sys.executable}"'


class FakeRunner:
    def __init__(self, *results):
        self.calls = []
        self.results = list(results)

    async def run(self, command, *, cwd=None, timeout=60.0):
        self.calls.append({"command": command, "cwd": cwd, "timeout": timeout})
        return self.results.pop(0) if self.results else CommandResult(0, "")


def approve_once(decision):
    async def go(d):
        return Approval("once")

    return go(decision)


async def deny(decision):
    return Approval("deny")


@pytest.fixture
def memory(tmp_path):
    with MemoryEngine(tmp_path / "mem.sqlite") as engine:
        yield engine


@pytest.fixture
def ctx(tmp_path, memory):
    return SkillContext(workdir=tmp_path, memory=memory, approver=approve_once)


@pytest.fixture
def registry(ctx):
    return build_default_registry(ctx)


# ============================================================ CT-04: catálogo e registry
def test_ct04_all_nine_native_skills_loaded_and_accessible_by_name(registry):
    assert len(registry) == 9
    assert sorted(registry.names()) == sorted(NATIVE_NAMES)
    for name in NATIVE_NAMES:
        skill = registry.get(name)
        assert skill.name == name and name in registry
        assert skill.source == "native" and skill.description and skill.category


def test_specs_expose_typed_json_schema(registry):
    specs = {s.name: s for s in registry.specs()}
    assert set(specs) == set(NATIVE_NAMES)
    schema = specs["shell-safe-runner"].input_schema
    assert schema["required"] == ["command"] and "timeout_seconds" in schema["properties"]
    assert specs["quick-notes"].category == "Memória & Notas"
    assert registry.names(source="plugin") == [] and registry.names(source="native")


async def test_ct04_quick_notes_executes_and_persists(registry, tmp_path):
    result = await registry.invoke(
        "quick-notes", {"op": "add", "text": "Reunião de alinhamento às 15h"}
    )
    assert result.ok and result.status == "ok"
    note_id = result.data["note"]["id"]

    with MemoryEngine(tmp_path / "mem.sqlite") as reopened:  # persistido em disco
        stored = reopened.get_long_term(note_id, namespace="notes")
    assert stored["text"] == "Reunião de alinhamento às 15h"


def test_registry_duplicates_unregister_and_errors(ctx):
    registry = SkillsRegistry(ctx)
    registry.register_native()
    assert ctx.skills is registry
    with pytest.raises(DuplicateSkillError):
        registry.register(registry.get("quick-notes"))
    registry.register(registry.get("quick-notes"), replace=True)
    assert registry.unregister("quick-notes").name == "quick-notes"
    assert "quick-notes" not in registry and len(registry) == 8
    with pytest.raises(UnknownSkillError):
        registry.get("quick-notes")
    with pytest.raises(UnknownSkillError):
        registry.unregister("quick-notes")
    with pytest.raises(TypeError):
        registry.register(object())
    assert [s.name for s in registry] == registry.names()
    assert issubclass(UnknownSkillError, LookupError)


async def test_invoke_unknown_skill_raises(registry):
    with pytest.raises(UnknownSkillError):
        await registry.invoke("nao-existe", {})


async def test_invalid_params_return_failure_without_raising(registry):
    result = await registry.invoke("shell-safe-runner", {"command": "", "extra": 1})
    assert not result.ok and result.status == "error"
    assert "Parâmetros inválidos para 'shell-safe-runner'" in result.error
    assert "command" in result.error and "extra" in result.error
    bad_op = await registry.invoke("quick-notes", {"op": "add"})  # falta 'text'
    assert not bad_op.ok and "text" in bad_op.error


async def test_invoke_with_none_params_uses_defaults(registry):
    result = await registry.invoke("quick-notes", None)  # op=add sem texto
    assert not result.ok and "'add' exige 'text'" in result.error


def test_skill_tool_risks_cover_every_guarded_operation():
    assert SKILL_TOOL_RISKS["issue_flow.view"].label == "Baixo"
    assert SKILL_TOOL_RISKS["issue_flow.create_pr"].label == "Médio"
    assert SKILL_TOOL_RISKS["swarm_inspector.kill"].label == "Médio"


# ===================================================================== guardrail na skill
class _Touch(Skill):
    name = "touch"
    description = "Skill de teste"
    category = "teste"
    from pydantic import BaseModel as _BM

    class Params(_BM):
        target: str = "x"

    ran: list = []

    def action(self, params):
        from src.core import ActionRequest

        return ActionRequest.tool_call("write_file", path=params.target)

    async def run(self, params):
        _Touch.ran.append(params.target)
        if params.target == "boom":
            raise SkillError("falhou de propósito")
        return SkillResult.success("ok")


async def test_skill_waits_for_approval_without_approver(tmp_path):
    _Touch.ran = []
    skill = _Touch(SkillContext(workdir=tmp_path))
    result = await skill.execute({"target": "a"})
    assert not result.ok and result.status == "awaiting_approval"
    assert result.decision_id.startswith("apr-") and "write_file" in result.error
    assert _Touch.ran == []  # nada executou
    skill.ctx.guardrails.approve(result.decision_id)
    assert skill.ctx.guardrails.get(result.decision_id).allowed


async def test_skill_rejected_and_approved_paths(tmp_path):
    _Touch.ran = []
    rejected = await _Touch(SkillContext(workdir=tmp_path, approver=deny)).execute({})
    assert rejected.status == "rejected" and not rejected.ok and _Touch.ran == []
    approved = await _Touch(SkillContext(workdir=tmp_path, approver=approve_once)).execute({})
    assert approved.ok and approved.output == "ok" and _Touch.ran == ["x"]


async def test_skill_error_inside_run_becomes_failure(tmp_path):
    result = await _Touch(SkillContext(workdir=tmp_path, approver=approve_once)).execute(
        {"target": "boom"}
    )
    assert not result.ok and result.error == "falhou de propósito"


async def test_custom_guardrail_engine_is_respected(tmp_path):
    lenient = GuardrailEngine(tool_risks={"write_file": SKILL_TOOL_RISKS["issue_flow.view"]})
    result = await _Touch(SkillContext(workdir=tmp_path, guardrails=lenient)).execute({})
    assert result.ok  # risco Baixo: sem interrupção


# ============================================================================ contexto
def test_context_require_and_workdir_confinement(tmp_path):
    ctx = SkillContext(workdir=tmp_path)
    with pytest.raises(SkillError, match="Memória"):
        ctx.require("memory", "Memória")
    assert ctx.require("workdir", "x") == tmp_path
    assert ctx.resolve_inside_workdir("a/b.txt") == (tmp_path / "a" / "b.txt").resolve()
    assert ctx.resolve_inside_workdir(".") == tmp_path.resolve()
    for escape in ("..", "../fora.txt", "a/../../x"):
        with pytest.raises(SkillError, match="fora do diretório"):
            ctx.resolve_inside_workdir(escape)


# ==================================================================== SubprocessRunner
async def test_runner_shell_string_and_exec_list(tmp_path):
    runner = SubprocessRunner()
    shell = await runner.run(f"{PY} -c \"print('ola')\"", cwd=tmp_path)
    assert shell.returncode == 0 and shell.stdout.strip() == "ola" and not shell.timed_out
    exec_ = await runner.run([sys.executable, "-c", "import sys; sys.stderr.write('e'); sys.exit(3)"])
    assert exec_.returncode == 3 and exec_.stderr == "e"


async def test_runner_timeout_missing_program_and_truncation():
    runner = SubprocessRunner()
    slow = await runner.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)
    assert slow.timed_out and slow.returncode == -1 and "Tempo limite" in slow.stderr
    missing = await runner.run(["programa-que-nao-existe-xyz"])
    assert missing.returncode == 127 and "não encontrado" in missing.stderr
    big = await runner.run([sys.executable, "-c", "print('x' * 30000)"])
    assert len(big.stdout) < 21000 and "truncada" in big.stdout


# ================================================================================ plugins
PLUGIN = '''
from pydantic import BaseModel
from src.skills import Skill, SkillResult

class Hello(Skill):
    name = "hello-plugin"
    description = "Diz olá"
    category = "teste"
    class Params(BaseModel):
        who: str = "mundo"
    async def run(self, params):
        return SkillResult.success(f"olá, {params.who}")
'''


def test_discover_plugins_loads_good_and_reports_bad(ctx, tmp_path):
    folder = tmp_path / "plugins"
    folder.mkdir()
    (folder / "hello.py").write_text(PLUGIN, encoding="utf-8")
    (folder / "broken.py").write_text("def (:\n", encoding="utf-8")
    (folder / "empty.py").write_text("X = 1\n", encoding="utf-8")
    (folder / "_private.py").write_text(PLUGIN.replace("hello-plugin", "hidden"), encoding="utf-8")

    registry = SkillsRegistry(ctx)
    report = registry.discover_plugins(folder)
    assert report.loaded == ["hello-plugin"]
    assert set(report.errors) == {"broken.py", "empty.py"}
    assert "SyntaxError" in report.errors["broken.py"]
    assert "nenhuma subclasse" in report.errors["empty.py"]
    assert registry.get("hello-plugin").source == "plugin"
    assert registry.names(source="plugin") == ["hello-plugin"] and "hidden" not in registry


async def test_plugin_runs_through_registry(ctx, tmp_path):
    folder = tmp_path / "plugins"
    folder.mkdir()
    (folder / "hello.py").write_text(PLUGIN, encoding="utf-8")
    registry = build_default_registry(ctx, plugins_dir=folder)
    assert len(registry) == 10
    result = await registry.invoke("hello-plugin", {"who": "Termina"})
    assert result.ok and result.output == "olá, Termina"


def test_discover_plugins_missing_directory_and_duplicate_name(ctx, tmp_path):
    registry = build_default_registry(ctx)
    assert registry.discover_plugins(tmp_path / "nao-existe").loaded == []
    folder = tmp_path / "plugins"
    folder.mkdir()
    (folder / "dup.py").write_text(PLUGIN.replace("hello-plugin", "quick-notes"), encoding="utf-8")
    report = registry.discover_plugins(folder)
    assert report.loaded == [] and "já registrada" in report.errors["dup.py"]


# ================================================================================ quick-notes
async def test_quick_notes_list_search_and_filters(registry):
    for text, tags in [("comprar café", ["Casa"]), ("revisar PR 14", ["trabalho", "pendente"]),
                       ("ligar para o banco", ["pendente"])]:  # fmt: skip
        assert (await registry.invoke("quick-notes", {"text": text, "tags": tags})).ok

    everything = await registry.invoke("quick-notes", {"op": "list"})
    assert len(everything.data["notes"]) == 3 and "revisar PR 14" in everything.output
    pending = await registry.invoke("quick-notes", {"op": "list", "tag": "PENDENTE"})
    assert {n["text"] for n in pending.data["notes"]} == {"revisar PR 14", "ligar para o banco"}
    found = await registry.invoke("quick-notes", {"op": "search", "query": "CAFÉ".lower()})
    assert [n["text"] for n in found.data["notes"]] == ["comprar café"]
    by_tag = await registry.invoke("quick-notes", {"op": "search", "query": "casa"})
    assert len(by_tag.data["notes"]) == 1  # busca também nas tags
    assert (await registry.invoke("quick-notes", {"op": "list", "limit": 1})).data["notes"].__len__() == 1
    nothing = await registry.invoke("quick-notes", {"op": "search", "query": "inexistente"})
    assert nothing.ok and nothing.output == "Nenhuma nota encontrada."
    assert (await registry.invoke("quick-notes", {"op": "search"})).error.endswith("exige 'query'.")


async def test_quick_notes_normalizes_tags_and_orders_newest_first(ctx, registry):
    from datetime import datetime

    ctx.clock = lambda: datetime(2026, 11, 1, 9, 0, 0)
    await registry.invoke("quick-notes", {"text": "antiga", "tags": [" Foo ", "foo", ""]})
    ctx.clock = lambda: datetime(2026, 11, 2, 9, 0, 0)
    second = await registry.invoke("quick-notes", {"text": "nova"})
    assert second.data["note"]["created_at"] == "2026-11-02T09:00:00"
    listed = (await registry.invoke("quick-notes", {"op": "list"})).data["notes"]
    assert [n["text"] for n in listed] == ["nova", "antiga"]
    assert listed[1]["tags"] == ["foo"]


async def test_quick_notes_without_memory_fails_cleanly(tmp_path):
    result = await build_default_registry(SkillContext(workdir=tmp_path)).invoke(
        "quick-notes", {"text": "x"}
    )
    assert not result.ok and "Memória" in result.error


async def test_quick_notes_vault_sync_needs_approval_and_never_overwrites(tmp_path, memory):
    from datetime import datetime

    vault = tmp_path / "vault"
    base = dict(workdir=tmp_path, memory=memory, vault_dir=vault, clock=lambda: datetime(2026, 11, 1, 8, 30))
    pending = await build_default_registry(SkillContext(**base)).invoke(
        "quick-notes", {"text": "Ideia: RAG híbrido!", "tags": ["ideia"]}
    )
    assert pending.status == "awaiting_approval" and not vault.exists()

    registry = build_default_registry(SkillContext(**base, approver=approve_once))
    first = await registry.invoke("quick-notes", {"text": "Ideia: RAG híbrido!", "tags": ["ideia"]})
    second = await registry.invoke("quick-notes", {"text": "Ideia: RAG híbrido!"})
    files = sorted(p.name for p in vault.iterdir())
    assert files == ["2026-11-01-ideia-rag-hibrido-2.md", "2026-11-01-ideia-rag-hibrido.md"]
    assert first.data["vault_file"].endswith("2026-11-01-ideia-rag-hibrido.md")
    content = (vault / "2026-11-01-ideia-rag-hibrido.md").read_text(encoding="utf-8")
    assert content.startswith("---\ncreated: 2026-11-01T08:30:00\ntags: [ideia]") and "RAG híbrido!" in content
    assert second.ok


async def test_quick_notes_vault_write_failure_reports_error(tmp_path, memory):
    blocker = tmp_path / "vault"
    blocker.write_text("sou um arquivo", encoding="utf-8")  # impede mkdir
    registry = build_default_registry(
        SkillContext(workdir=tmp_path, memory=memory, vault_dir=blocker, approver=approve_once)
    )
    result = await registry.invoke("quick-notes", {"text": "x"})
    assert not result.ok and "vault falhou" in result.error
    assert len(memory.list_long_term("notes")) == 1  # a nota ficou salva na memória


def test_slugify():
    from src.skills.native.quick_notes import slugify

    assert slugify("Reunião às 15h: ação!") == "reuniao-as-15h-acao"
    assert slugify("???") == "nota"
    assert len(slugify("a" * 200)) <= 48


# ================================================================================ rag-indexer
@pytest.fixture
def rag_registry(tmp_path, memory):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "manual.txt").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    rag = RAGEngine(tmp_path / "rag.sqlite", HashingEmbedder(), chunk_size=60, overlap=0)
    yield build_default_registry(SkillContext(workdir=tmp_path, memory=memory, rag=rag))
    rag.close()


async def test_rag_indexer_index_ask_status(rag_registry):
    assert "0 documento" in (await rag_registry.invoke("rag-indexer", {"op": "status"})).output
    indexed = await rag_registry.invoke("rag-indexer", {"op": "index", "path": "docs"})
    assert indexed.ok and indexed.data["files_indexed"] == 1 and indexed.data["chunks"] >= 4
    again = await rag_registry.invoke("rag-indexer", {"op": "index", "path": "docs"})
    assert again.data["files_indexed"] == 0 and again.data["files_unchanged"] == 1
    forced = await rag_registry.invoke("rag-indexer", {"op": "index", "path": "docs", "force": True})
    assert forced.data["files_indexed"] == 1

    answer = await rag_registry.invoke(
        "rag-indexer", {"op": "ask", "question": "Qual a porta padrão do serviço?", "top_k": 2}
    )
    top = answer.data["hits"][0]
    assert "8080" in top["content"] and top["citation"].endswith("manual.txt:3")
    assert len(answer.data["hits"]) == 2 and "8080" in answer.output
    assert (await rag_registry.invoke("rag-indexer", {"op": "status"})).data["documents"] == 1


async def test_rag_indexer_errors(rag_registry, tmp_path):
    outside = await rag_registry.invoke("rag-indexer", {"op": "index", "path": "../fora"})
    assert not outside.ok and "fora do diretório" in outside.error
    missing = await rag_registry.invoke("rag-indexer", {"op": "index", "path": "nao-existe"})
    assert not missing.ok and "não encontrado" in missing.error.lower()
    empty_index = await rag_registry.invoke("rag-indexer", {"op": "ask", "question": "algo"})
    assert empty_index.ok and empty_index.output == "Nada relevante encontrado."
    assert "exige 'path'" in (await rag_registry.invoke("rag-indexer", {"op": "index"})).error
    assert "exige 'question'" in (await rag_registry.invoke("rag-indexer", {"op": "ask"})).error


async def test_rag_indexer_without_engine(tmp_path):
    result = await build_default_registry(SkillContext(workdir=tmp_path)).invoke(
        "rag-indexer", {"op": "status"}
    )
    assert not result.ok and "Motor RAG" in result.error


# ============================================================================ shell-safe-runner
async def test_shell_runner_low_risk_runs_without_prompt(tmp_path):
    prompts = []

    async def spy(decision):
        prompts.append(decision)
        return Approval("deny")

    registry = build_default_registry(SkillContext(workdir=tmp_path, approver=spy))
    result = await registry.invoke("shell-safe-runner", {"command": "echo ola"})
    assert result.ok and result.output.strip() == "ola" and prompts == []
    assert result.data["returncode"] == 0 and result.data["command"] == "echo ola"


async def test_shell_runner_critical_is_blocked_and_never_runs(tmp_path):
    runner = FakeRunner()
    registry = build_default_registry(SkillContext(workdir=tmp_path, runner=runner))
    result = await registry.invoke("shell-safe-runner", {"command": "rm -rf /"})
    assert result.status == "awaiting_approval" and not result.ok and runner.calls == []
    assert "Crítico" in result.error


async def test_shell_runner_medium_runs_after_approval_with_real_process(tmp_path):
    registry = build_default_registry(SkillContext(workdir=tmp_path, approver=approve_once))
    result = await registry.invoke(
        "shell-safe-runner", {"command": f"{PY} -c \"print('feito')\""}
    )
    assert result.ok and result.output.strip() == "feito"


async def test_shell_runner_denied_does_not_run(tmp_path):
    runner = FakeRunner()
    registry = build_default_registry(SkillContext(workdir=tmp_path, runner=runner, approver=deny))
    result = await registry.invoke("shell-safe-runner", {"command": "rm file.txt"})
    assert result.status == "rejected" and runner.calls == []


async def test_shell_runner_executes_the_modified_command(tmp_path):
    async def modify(decision):
        return Approval("modify", command="echo seguro")

    runner = FakeRunner(CommandResult(0, "seguro\n"))
    registry = build_default_registry(SkillContext(workdir=tmp_path, runner=runner, approver=modify))
    result = await registry.invoke("shell-safe-runner", {"command": "rm -rf /tmp/x"})
    assert result.ok and runner.calls[0]["command"] == "echo seguro"
    assert result.data["command"] == "echo seguro"


async def test_shell_runner_failure_timeout_and_cwd(tmp_path):
    (tmp_path / "sub").mkdir()
    runner = FakeRunner(
        CommandResult(2, "parcial", "erro feio"), CommandResult(-1, "", "lento", timed_out=True), CommandResult(0, "")
    )
    registry = build_default_registry(SkillContext(workdir=tmp_path, runner=runner, approver=approve_once))
    failed = await registry.invoke("shell-safe-runner", {"command": "echo a", "cwd": "sub"})
    assert not failed.ok and failed.error == "código de saída 2"
    assert failed.output == "parcial\n[stderr]\nerro feio"
    assert runner.calls[0]["cwd"] == (tmp_path / "sub").resolve()
    timed = await registry.invoke("shell-safe-runner", {"command": "echo a", "timeout_seconds": 5})
    assert timed.error == "tempo limite excedido" and runner.calls[1]["timeout"] == 5
    assert runner.calls[1]["cwd"] == tmp_path  # padrão: diretório de trabalho
    escaped = await registry.invoke("shell-safe-runner", {"command": "echo a", "cwd": ".."})
    assert not escaped.ok and "fora do diretório" in escaped.error
    too_long = await registry.invoke("shell-safe-runner", {"command": "echo a", "timeout_seconds": 9999})
    assert not too_long.ok


async def test_shell_runner_run_without_guardrail_path(tmp_path):
    # `run` direto (sem execute) delega para run_approved com o comando original
    skill = build_default_registry(SkillContext(workdir=tmp_path, runner=FakeRunner(CommandResult(0, "x")))).get(
        "shell-safe-runner"
    )
    params = skill.parse_params({"command": "echo x"})
    assert (await skill.run(params)).output == "x"


# ==================================================================================== issue-flow
@pytest.fixture
def gh(tmp_path, memory):
    runner = FakeRunner()
    ctx = SkillContext(workdir=tmp_path, memory=memory, runner=runner, approver=approve_once)
    return build_default_registry(ctx), runner


async def test_issue_flow_view_and_list_are_low_risk_and_parse_json(tmp_path):
    runner = FakeRunner(
        CommandResult(0, json.dumps({"number": 7, "title": "T"})),
        CommandResult(0, json.dumps([{"number": 1, "title": "A", "labels": []}])),
        CommandResult(0, "não é json"),
    )
    registry = build_default_registry(SkillContext(workdir=tmp_path, runner=runner))  # sem approver
    view = await registry.invoke("issue-flow", {"op": "view", "number": 7})
    assert view.ok and view.data["json"]["number"] == 7
    assert runner.calls[0]["command"][:4] == ["gh", "issue", "view", "7"]
    listed = await registry.invoke("issue-flow", {"op": "list", "state": "all", "limit": 5, "labels": ["bug"]})
    assert listed.data["json"][0]["title"] == "A"
    argv = runner.calls[1]["command"]
    assert argv[:3] == ["gh", "issue", "list"] and "--label" in argv and argv[argv.index("--limit") + 1] == "5"
    plain = await registry.invoke("issue-flow", {"op": "list"})
    assert plain.ok and "json" not in plain.data and plain.output == "não é json"


async def test_issue_flow_create_issue_commit_and_pr(gh):
    registry, runner = gh
    runner.results = [
        CommandResult(0, "https://github.com/o/r/issues/9\n"),
        CommandResult(0, ""),
        CommandResult(0, "[development abc123] feat: x\n"),
        CommandResult(0, "aviso\nhttps://github.com/o/r/pull/10\n"),
    ]
    issue = await registry.invoke(
        "issue-flow", {"op": "create_issue", "title": "feat: X", "body": "corpo", "labels": ["enhancement"]}
    )
    assert issue.data["url"].endswith("/issues/9")
    assert runner.calls[0]["command"] == [
        "gh", "issue", "create", "--title", "feat: X", "--body", "corpo", "--label", "enhancement",
    ]  # fmt: skip

    commit = await registry.invoke(
        "issue-flow", {"op": "commit", "message": "feat: adiciona x. Closes #9", "paths": ["src/a.py", "tests/t.py"]}
    )
    assert commit.ok
    assert runner.calls[1]["command"] == ["git", "add", "--", "src/a.py", "tests/t.py"]
    assert runner.calls[2]["command"] == ["git", "commit", "-m", "feat: adiciona x. Closes #9"]
    assert not any("Co-Authored-By" in str(c["command"]) for c in runner.calls)

    pr = await registry.invoke(
        "issue-flow", {"op": "create_pr", "title": "feat: adiciona x", "body": "- x", "base": "main"}
    )
    assert pr.data["url"].endswith("/pull/10")
    assert runner.calls[3]["command"][:3] == ["gh", "pr", "create"]
    assert runner.calls[3]["command"][-2:] == ["--base", "main"]
    assert not any("merge" in c["command"] for c in runner.calls if isinstance(c["command"], list))


async def test_issue_flow_mutations_require_approval(tmp_path):
    runner = FakeRunner()
    registry = build_default_registry(SkillContext(workdir=tmp_path, runner=runner))
    result = await registry.invoke("issue-flow", {"op": "create_issue", "title": "feat: x"})
    assert result.status == "awaiting_approval" and runner.calls == []


@pytest.mark.parametrize(
    ("params", "fragment"),
    [
        ({"op": "view"}, "exige 'number'"),
        ({"op": "create_issue"}, "exige 'title'"),
        ({"op": "create_pr", "title": "titulo sem tipo"}, "Conventional Commits"),
        ({"op": "commit", "message": "mensagem ruim", "paths": ["a"]}, "Conventional Commits"),
        ({"op": "commit", "paths": ["a"]}, "Conventional Commits"),
        ({"op": "commit", "message": "fix: x"}, "paths"),
        ({"op": "commit", "message": "fix: x", "paths": ["."]}, "arquivos específicos"),
        ({"op": "commit", "message": "fix: x", "paths": ["-A"]}, "arquivos específicos"),
        ({"op": "commit", "message": "fix: x", "paths": ["--all"]}, "arquivos específicos"),
        ({"op": "merge"}, "op"),
    ],
)
async def test_issue_flow_validation(gh, params, fragment):
    registry, runner = gh
    result = await registry.invoke("issue-flow", params)
    assert not result.ok and fragment in result.error and runner.calls == []


async def test_issue_flow_command_failure_is_reported(gh):
    registry, runner = gh
    runner.results = [CommandResult(1, "", "HTTP 404: Not Found")]
    result = await registry.invoke("issue-flow", {"op": "view", "number": 999})
    assert not result.ok and "código 1" in result.error and "404" in result.error


async def test_issue_flow_commit_stops_if_add_fails(gh):
    registry, runner = gh
    runner.results = [CommandResult(128, "", "pathspec did not match")]
    result = await registry.invoke("issue-flow", {"op": "commit", "message": "fix: x", "paths": ["nada"]})
    assert not result.ok and len(runner.calls) == 1


# ===================================================================================== doc-gen
@pytest.fixture
def docs(tmp_path, memory):
    runner = FakeRunner()
    return build_default_registry(SkillContext(workdir=tmp_path, memory=memory, runner=runner, approver=approve_once)), runner


@pytest.mark.parametrize(
    ("fmt", "suffix", "magic"),
    [("md", ".md", b"# T"), ("html", ".html", b"<!doctype html>"), ("pdf", ".pdf", b"%PDF-1.4"),
     ("docx", ".docx", b"PK"), ("mermaid", ".mmd", b"# T")],
)  # fmt: skip
async def test_doc_gen_writes_each_format_with_auto_extension(docs, tmp_path, fmt, suffix, magic):
    registry, _ = docs
    result = await registry.invoke("doc-gen", {"format": fmt, "content": "# T\n\ntexto", "output": "out/rel", "title": "R"})
    assert result.ok, result.error
    target = tmp_path / "out" / f"rel{suffix}"
    assert target.is_file() and target.read_bytes().startswith(magic)
    assert result.data["files"][0] == target.resolve().as_posix()


async def test_doc_gen_keeps_existing_extension(docs, tmp_path):
    registry, _ = docs
    await registry.invoke("doc-gen", {"format": "md", "content": "x", "output": "nota.md"})
    assert (tmp_path / "nota.md").exists() and not (tmp_path / "nota.md.md").exists()


async def test_doc_gen_svg_validation(docs, tmp_path):
    registry, _ = docs
    ok = await registry.invoke(
        "doc-gen", {"format": "svg", "content": '<svg xmlns="http://www.w3.org/2000/svg"/>', "output": "d"}
    )
    assert ok.ok and (tmp_path / "d.svg").exists()
    broken = await registry.invoke("doc-gen", {"format": "svg", "content": "<svg", "output": "e"})
    assert not broken.ok and "SVG inválido" in broken.error
    other = await registry.invoke("doc-gen", {"format": "svg", "content": "<html/>", "output": "f"})
    assert not other.ok and "não é um SVG" in other.error
    assert not (tmp_path / "e.svg").exists() and not (tmp_path / "f.svg").exists()


async def test_doc_gen_never_overwrites_without_flag_and_confines_to_workdir(docs, tmp_path):
    registry, _ = docs
    await registry.invoke("doc-gen", {"format": "md", "content": "v1", "output": "a"})
    again = await registry.invoke("doc-gen", {"format": "md", "content": "v2", "output": "a"})
    assert not again.ok and "overwrite=true" in again.error
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == "v1"
    forced = await registry.invoke("doc-gen", {"format": "md", "content": "v2", "output": "a", "overwrite": True})
    assert forced.ok and (tmp_path / "a.md").read_text(encoding="utf-8") == "v2"
    escaped = await registry.invoke("doc-gen", {"format": "md", "content": "x", "output": "../fora"})
    assert not escaped.ok and "fora do diretório" in escaped.error


async def test_doc_gen_needs_approval(tmp_path):
    registry = build_default_registry(SkillContext(workdir=tmp_path))
    result = await registry.invoke("doc-gen", {"format": "md", "content": "x", "output": "a"})
    assert result.status == "awaiting_approval" and not (tmp_path / "a.md").exists()


async def test_doc_gen_mermaid_rendering(docs, tmp_path, monkeypatch):
    registry, runner = docs
    monkeypatch.setattr("shutil.which", lambda _name: None)
    plain = await registry.invoke("doc-gen", {"format": "mermaid", "content": "graph TD; A-->B", "output": "g"})
    assert plain.ok and "mmdc" in plain.data["warnings"][0] and runner.calls == []

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/mmdc")

    class Renderer(FakeRunner):
        async def run(self, command, *, cwd=None, timeout=60.0):
            Path(command[command.index("-o") + 1]).write_text("<svg/>", encoding="utf-8")
            return await super().run(command, cwd=cwd, timeout=timeout)

    registry.ctx.runner = runner = Renderer()
    rendered = await registry.invoke("doc-gen", {"format": "mermaid", "content": "graph TD; A-->B", "output": "h"})
    assert len(rendered.data["files"]) == 2 and rendered.data["files"][1].endswith("h.svg")
    assert runner.calls[0]["command"][0] == "mmdc"


async def test_issue_flow_create_pr_without_base_uses_repo_default(gh):
    registry, runner = gh
    runner.results = [CommandResult(0, "https://github.com/o/r/pull/1\n")]
    result = await registry.invoke("issue-flow", {"op": "create_pr", "title": "fix: corrige x"})
    assert result.ok and "--base" not in runner.calls[0]["command"]


def test_plugin_with_unloadable_spec_is_reported(ctx, tmp_path, monkeypatch):
    folder = tmp_path / "plugins"
    folder.mkdir()
    (folder / "ghost.py").write_text(PLUGIN, encoding="utf-8")
    monkeypatch.setattr("importlib.util.spec_from_file_location", lambda *a, **k: None)
    report = SkillsRegistry(ctx).discover_plugins(folder)
    assert report.loaded == [] and "não foi possível carregar" in report.errors["ghost.py"]
