"""Ponto de entrada: argumentos, config, códigos de saída e execução não interativa."""

import subprocess
import sys
from pathlib import Path

import pytest

from src.main import EXIT_ERROR, EXIT_INTERRUPTED, EXIT_PARTIAL, _make_console, build_parser, main
from tests.helpers import FakeLLM, ScriptedReader, make_console, output, plan_json

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # config.yaml e .terminas ficam isolados
    return tmp_path


def test_parser_run_joins_goal_words_and_defaults_to_interactive():
    parser = build_parser()
    args = parser.parse_args(["run", "indexar", "a", "pasta"])
    assert args.command == "run" and args.goal == ["indexar", "a", "pasta"] and args.config is None
    assert parser.parse_args([]).command is None
    assert parser.parse_args(["--config", "x.yaml"]).config == "x.yaml"
    with pytest.raises(SystemExit):
        parser.parse_args(["run"])  # meta é obrigatória


def test_version_flag_via_real_subprocess():
    result = subprocess.run(
        [sys.executable, "-m", "src.main", "--version"], capture_output=True, text=True, cwd=str(REPO)
    )
    assert result.returncode == 0 and result.stdout.strip() == "terminas 0.1.0"


def test_interactive_session_with_injected_reader(isolated_cwd):
    console, reader = make_console(), ScriptedReader("/status", "/exit")
    assert main([], reader=reader, console=console, llm=FakeLLM()) == 0
    assert "Termina's Agent v0.1.0" in output(console) and "Nenhum subagente registrado." in output(console)
    assert (isolated_cwd / ".terminas" / "memory.sqlite").is_file()  # padrão: dados no projeto
    assert (isolated_cwd / ".terminas" / "rag.sqlite").is_file()


def test_audit_log_is_written_under_data_dir(isolated_cwd):
    reader = ScriptedReader('/skill doc-gen {"format": "md", "content": "x", "output": "a"}', "n", "/exit")
    main([], reader=reader, console=make_console(), llm=FakeLLM())
    lines = (isolated_cwd / ".terminas" / "logs" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"event": "evaluated"' in line and "doc_gen.generate" in line for line in lines)
    assert any('"event": "rejected"' in line for line in lines)


def test_custom_config_changes_data_dir_and_behavior(isolated_cwd):
    data_dir = (isolated_cwd / "dados").as_posix()
    (isolated_cwd / "meu.yaml").write_text(
        f"paths: {{data_dir: '{data_dir}'}}\nbehavior: {{tone: seco}}\n", encoding="utf-8"
    )
    console = make_console()
    reader = ScriptedReader("/behavior", "/exit")
    assert main(["--config", "meu.yaml"], reader=reader, console=console, llm=FakeLLM()) == 0
    assert (isolated_cwd / "dados" / "memory.sqlite").is_file() and "seco" in output(console)


def test_config_errors_return_exit_2(isolated_cwd, capsys):
    assert main(["--config", "nao-existe.yaml"]) == EXIT_ERROR
    (isolated_cwd / "ruim.yaml").write_text("limits: {max_steps: 99}", encoding="utf-8")
    assert main(["--config", "ruim.yaml"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "terminas: Arquivo de configuração não encontrado" in err and "limits.max_steps" in err


def test_run_subcommand_exit_codes():
    console = make_console()
    skipped = main(["run", "fazer", "algo"], console=console, llm=FakeLLM(plan_json(("t1", None))))
    assert skipped == EXIT_PARTIAL  # sem subagentes, o nó fica ignorado
    text = output(console)
    assert "Resultado" in text and "nenhum executor disponível para 'master'" in text

    console = make_console()
    assert main(["run", "meta"], console=console, llm=FakeLLM(*["lixo"] * 3)) == EXIT_ERROR
    assert "Não consegui montar um plano válido" in output(console)


def test_ctrl_c_during_startup_returns_130(monkeypatch):
    def interrupted(coro):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr("asyncio.run", interrupted)
    assert main([]) == EXIT_INTERRUPTED


def test_console_never_fails_on_legacy_encodings(monkeypatch):
    class Stream:
        reconfigured = None

        def reconfigure(self, **kwargs):
            Stream.reconfigured = kwargs

        def isatty(self):
            return False

        def write(self, text):
            return len(text)

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stdout", Stream())
    monkeypatch.setattr(sys, "stderr", object())  # sem reconfigure: ignorado
    _make_console()
    assert Stream.reconfigured == {"errors": "replace"}
