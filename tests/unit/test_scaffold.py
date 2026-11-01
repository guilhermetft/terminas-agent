import importlib
from pathlib import Path

import pytest
import yaml

import src

MODULES = [
    "src",
    "src.core",
    "src.rag",
    "src.skills",
    "src.skills.native",
    "src.skills.plugins",
    "src.memory",
    "src.subagents_bridge",
    "src.ui",
    "src.ui.components",
    "src.utils",
    "src.main",
]

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", MODULES)
def test_module_importable(name):
    assert importlib.import_module(name)


def test_version():
    assert src.__version__ == "0.1.0"


def test_main_entrypoint_returns_zero(tmp_path, monkeypatch):
    from src.main import main
    from tests.helpers import ScriptedReader, make_console

    monkeypatch.chdir(tmp_path)  # os dados (.terminas) ficam no diretório temporário
    assert main([], reader=ScriptedReader("/exit"), console=make_console()) == 0


def test_main_version_flag(capsys):
    from src.main import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "terminas 0.1.0" in capsys.readouterr().out


def test_config_example_is_valid_yaml():
    data = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert {"behavior", "llm", "limits", "paths"} <= data.keys()
    assert data["limits"]["max_steps"] == 15
