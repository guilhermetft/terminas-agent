"""Instaladores (install.sh / install.ps1): consistência com o projeto e fluxos simulados.

Estes testes não usam rede nem instalam nada: o `uv` e o `curl` são dublês em uma pasta
temporária. A instalação de verdade é validada no CI (.github/workflows/install.yml).
"""

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SH = ROOT / "install.sh"
PS1 = ROOT / "install.ps1"
README = ROOT / "README.md"
WORKFLOW = ROOT / ".github" / "workflows" / "install.yml"

RAW = "https://raw.githubusercontent.com/guilhermetft/terminas-agent/main"
ONE_LINER_PS = f"irm {RAW}/install.ps1 | iex"
ONE_LINER_SH = f"curl -fsSL {RAW}/install.sh | bash"

PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def code_lines(script: Path) -> str:
    """O script sem as linhas de comentário."""
    lines = script.read_text(encoding="ascii").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


# ================================================================== estáticos
@pytest.mark.parametrize("script", [SH, PS1], ids=lambda p: p.name)
def test_scripts_are_ascii_and_use_lf(script):
    raw = script.read_bytes()
    assert raw.isascii(), "caracteres não-ASCII quebram o irm | iex no Windows PowerShell 5.1"
    assert b"\r" not in raw, "CRLF quebra o bash e muda o que o GitHub serve"


def test_gitattributes_forces_lf_for_scripts():
    rules = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in rules and "*.ps1 text eol=lf" in rules


@pytest.mark.parametrize("script", [SH, PS1], ids=lambda p: p.name)
def test_scripts_never_escalate_privileges_or_relax_policies(script):
    code = code_lines(script).lower()
    for forbidden in ("sudo ", "set-executionpolicy", "runas", "chmod 777"):
        assert forbidden not in code, forbidden


@pytest.mark.parametrize("script", [SH, PS1], ids=lambda p: p.name)
def test_scripts_abort_on_error(script):
    text = script.read_text(encoding="ascii")
    if script is SH:
        assert "set -euo pipefail" in text
    else:
        assert "$ErrorActionPreference = 'Stop'" in text and "Set-StrictMode" in text


@pytest.mark.parametrize("script", [SH, PS1], ids=lambda p: p.name)
def test_package_and_command_match_pyproject(script):
    text = script.read_text(encoding="ascii")
    name = PYPROJECT["project"]["name"]
    assert f'"{name}"' in text or f"'{name}'" in text
    assert "terminas" in PYPROJECT["project"]["scripts"]
    assert re.search(r"""(COMMAND_NAME="|\$commandName = ')terminas['"]""", text)


@pytest.mark.parametrize("script", [SH, PS1], ids=lambda p: p.name)
def test_default_extras_and_python_are_valid_for_the_project(script):
    text = script.read_text(encoding="ascii")
    assert {"rag", "mcp"} <= set(PYPROJECT["project"]["optional-dependencies"])
    assert "rag,mcp" in text
    default_python = re.search(r"3\.\d+", text.split("TERMINAS_PYTHON")[1]).group(0)
    required = PYPROJECT["project"]["requires-python"].lstrip(">=")
    default = tuple(int(x) for x in default_python.split("."))
    assert default >= tuple(int(x) for x in required.split(".")[:2])


def test_repo_slug_is_consistent_everywhere():
    for script in (SH, PS1):
        assert "guilhermetft/terminas-agent" in script.read_text(encoding="ascii")
    assert "github.com/guilhermetft/terminas-agent" in README.read_text(encoding="utf-8")


def test_readme_documents_the_exact_one_liners_the_scripts_advertise():
    readme = README.read_text(encoding="utf-8")
    assert ONE_LINER_PS in readme and ONE_LINER_SH in readme
    assert ONE_LINER_PS in PS1.read_text(encoding="ascii")
    assert ONE_LINER_SH in SH.read_text(encoding="ascii")
    for section in ("#### Atualizar", "#### Desinstalar", "#### Opcoes"):
        assert section in readme
    assert "git clone" in readme.split("#### Instalacao para desenvolvimento")[1]  # dev continua possível
    assert "github.com/guilhermetft/terminas-agent/cli/" not in readme  # essa URL dá 404


def test_workflow_covers_the_required_platforms():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert {"static", "linux", "fedora", "windows", "one-liner"} <= set(jobs)
    assert set(jobs["linux"]["strategy"]["matrix"]["os"]) >= {"ubuntu-latest", "macos-latest"}
    assert jobs["fedora"]["container"] == "fedora:latest"
    assert jobs["windows"]["strategy"]["matrix"]["shell"] == ["powershell", "pwsh"]  # PS 5.1 e 7
    assert jobs["one-liner"]["if"] == "github.event_name != 'pull_request'"
    text = WORKFLOW.read_text(encoding="utf-8")
    assert f"{RAW}/install.sh" in text and f"{RAW}/install.ps1" in text


# ================================================================ sintaxe real
def find_bash() -> str | None:
    if sys.platform == "win32":  # evita o bash.exe do System32 (lançador do WSL)
        for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
            if Path(candidate).exists():
                return candidate
        return None
    return shutil.which("bash")


BASH = find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="bash indisponível")


def find_powershell() -> list[str] | None:
    for name in ("pwsh", "powershell"):
        exe = shutil.which(name)
        if exe:
            return [exe, "-NoProfile", "-ExecutionPolicy", "Bypass"]
    return None


POWERSHELL = find_powershell()
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell indisponível")


@needs_bash
def test_install_sh_has_valid_bash_syntax():
    result = subprocess.run([BASH, "-n", str(SH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@needs_powershell
def test_install_ps1_has_valid_powershell_syntax():
    command = (
        "$e = $null; $t = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{PS1}', [ref]$t, [ref]$e); "
        "if ($e.Count) { $e | ForEach-Object { $_.Message }; exit 1 }"
    )
    result = subprocess.run([*POWERSHELL, "-Command", command], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@needs_powershell
def test_install_ps1_errors_do_not_close_the_session_and_exit_nonzero_as_a_file():
    env = {**os.environ, "TERMINAS_EXTRAS": "rag;calc"}
    as_iex = subprocess.run(
        [*POWERSHELL, "-Command", f"Get-Content -Raw '{PS1}' | Invoke-Expression; Write-Host 'SESSAO-VIVA'"],
        capture_output=True, text=True, env=env,
    )  # fmt: skip
    assert "ERRO:" in as_iex.stdout and "SESSAO-VIVA" in as_iex.stdout
    as_file = subprocess.run([*POWERSHELL, "-File", str(PS1)], capture_output=True, text=True, env=env)
    assert as_file.returncode == 1 and "ERRO:" in as_file.stdout


# =========================================================== fluxos simulados
FAKE_UV = """#!/bin/sh
echo "uv $*" >> "$FAKE_LOG"
case "$1 $2" in
  "tool install")
    [ -z "${FAKE_UV_FAIL:-}" ] || exit 1
    mkdir -p "$FAKE_BIN"
    printf '#!/bin/sh\\necho "terminas 9.9.9"\\n' > "$FAKE_BIN/terminas"
    chmod +x "$FAKE_BIN/terminas" ;;
  "tool uninstall") rm -f "$FAKE_BIN/terminas" ;;
  "tool dir") echo "$FAKE_BIN" ;;
esac
exit 0
"""

FAKE_CURL = """#!/bin/sh
echo "curl $*" >> "$FAKE_LOG"
# o instalador do uv "baixado": cria um uv falso em $HOME/.local/bin
cat <<'INSTALLER'
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/uv" <<'UVSCRIPT'
@@UV@@
UVSCRIPT
chmod +x "$HOME/.local/bin/uv"
INSTALLER
"""

# O bash do Windows (Git) põe /usr/bin na frente do PATH ao iniciar; por isso os dublês entram no PATH
# aqui dentro, já com o bash rodando. Funciona igual em Linux e macOS.
WRAPPER = r"""
to_unix() { if command -v cygpath >/dev/null 2>&1; then cygpath -u "$1"; else printf '%s' "$1"; fi; }
fake="$(to_unix "$FAKE_BINDIR")"
FAKE_BIN="$(to_unix "$FAKE_BIN")"
export FAKE_BIN
extra=""
[ -z "${EXTRA_PATH:-}" ] || extra="$(to_unix "$EXTRA_PATH"):"
if [ -n "${RESTRICT_PATH:-}" ]; then PATH="$fake"; else PATH="$extra$fake:$PATH"; fi
export PATH
exec "$BASH" "$0" "$@"
"""


class Sandbox:
    def __init__(self, tmp_path: Path, *, uname: str = "Linux", with_uv: bool = True, with_curl: bool = False):
        self.fakebin = tmp_path / "fakebin"
        self.fakebin.mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.tool_bin = tmp_path / "toolbin"
        self.log = tmp_path / "calls.log"
        self.log.write_text("", encoding="utf-8")
        self.write_exe("uname", f"#!/bin/sh\necho {uname}\n")
        if with_uv:
            self.write_exe("uv", FAKE_UV)
        if with_curl:
            self.write_exe("curl", FAKE_CURL.replace("@@UV@@", FAKE_UV.rstrip("\n")))

    def write_exe(self, name: str, content: str) -> None:
        path = self.fakebin / name
        path.write_bytes(content.encode("utf-8"))  # bytes: sem conversão de fim de linha
        path.chmod(0o755)

    def run(self, *args: str, env: dict | None = None, restricted_path: bool = False):
        base = {
            "HOME": self.home.as_posix(),
            "FAKE_LOG": self.log.as_posix(),
            "FAKE_BIN": self.tool_bin.as_posix(),
            "FAKE_BINDIR": self.fakebin.as_posix(),
            "PATH": os.environ["PATH"],
        }
        if restricted_path:
            base["RESTRICT_PATH"] = "1"
        for key in ("SystemRoot", "TEMP", "TMP"):  # o bash do Windows precisa disso para iniciar
            if key in os.environ:
                base[key] = os.environ[key]
        return subprocess.run(
            [BASH, "-c", WRAPPER, str(SH), *args],
            capture_output=True, text=True, env={**base, **(env or {})}, timeout=60,
        )  # fmt: skip

    def reset_log(self) -> None:
        self.log.write_text("", encoding="utf-8")

    @property
    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def install_call(self) -> str:
        return next(c for c in self.calls if c.startswith("uv tool install"))


@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path)


@needs_bash
def test_default_install_uses_zip_url_python_and_all_default_extras(sandbox):
    result = sandbox.run()
    assert result.returncode == 0, result.stderr
    assert sandbox.install_call() == (
        "uv tool install --force --reinstall --python 3.12 "
        "terminas-agent[rag,mcp] @ https://github.com/guilhermetft/terminas-agent/archive/main.zip"
    )
    assert "uv tool dir --bin" in sandbox.calls and "uv tool update-shell" in sandbox.calls
    assert "Pronto: terminas 9.9.9" in result.stdout
    assert "NOVO terminal" in result.stdout  # a pasta do uv ainda não estava no PATH
    assert "git+" not in sandbox.install_call()  # o zip dispensa ter o git instalado


@needs_bash
def test_version_and_extras_via_environment_and_flags(sandbox):
    sandbox.run(env={"TERMINAS_VERSION": "v1.2.3", "TERMINAS_EXTRAS": "none"})
    assert "terminas-agent @ https://github.com/guilhermetft/terminas-agent/archive/v1.2.3.zip" in sandbox.install_call()
    sandbox.reset_log()
    sandbox.run("--version", "feature/x", "--extras", "mcp")
    assert "terminas-agent[mcp] @ https://github.com/guilhermetft/terminas-agent/archive/feature/x.zip" in sandbox.install_call()
    sandbox.reset_log()
    sandbox.run(env={"TERMINAS_EXTRAS": "", "TERMINAS_PYTHON": "3.13"})
    assert "--python 3.13 terminas-agent @ " in sandbox.install_call()  # extras vazio = nenhum


@needs_bash
def test_local_source_directory_becomes_a_file_url(sandbox, tmp_path):
    source = tmp_path / "meu-codigo"
    source.mkdir()
    sandbox.run(env={"TERMINAS_SOURCE": source.as_posix()})
    install = sandbox.install_call()
    assert " @ file://" in install and install.endswith("meu-codigo")
    sandbox.reset_log()
    sandbox.run(env={"TERMINAS_SOURCE": "https://exemplo.com/pacote.zip"})
    assert sandbox.install_call().endswith("@ https://exemplo.com/pacote.zip")


@needs_bash
def test_no_modify_path_skips_update_shell(sandbox):
    sandbox.run(env={"TERMINAS_NO_MODIFY_PATH": "1"})
    assert "uv tool update-shell" not in sandbox.calls


@needs_bash
def test_reinstall_is_idempotent_and_reports_command_already_available(sandbox):
    sandbox.run()
    first = sandbox.install_call()
    result = sandbox.run(env={"EXTRA_PATH": sandbox.tool_bin.as_posix()})
    assert result.returncode == 0 and "ja esta disponivel neste terminal" in result.stdout
    installs = [c for c in sandbox.calls if c.startswith("uv tool install")]
    assert len(installs) == 2 and installs[1] == first  # mesmo comando; --reinstall atualiza


@needs_bash
def test_uninstall_by_flag_and_by_environment(sandbox):
    sandbox.run()
    flag = sandbox.run("--uninstall")
    assert flag.returncode == 0 and "removido" in flag.stdout and ".terminas/" in flag.stdout
    assert "uv tool uninstall terminas-agent" in sandbox.calls
    assert not (sandbox.tool_bin / "terminas").exists()
    sandbox.reset_log()
    by_env = sandbox.run(env={"TERMINAS_UNINSTALL": "1"})
    assert by_env.returncode == 0 and not any(c.startswith("uv tool install") for c in sandbox.calls)
    assert "uv tool uninstall terminas-agent" in sandbox.calls


@needs_bash
def test_uninstall_without_uv_explains_instead_of_installing_it(tmp_path):
    box = Sandbox(tmp_path, with_uv=False, with_curl=True)
    result = box.run("--uninstall")
    assert result.returncode == 1 and "nada a desinstalar" in result.stderr
    assert box.calls == []  # nem baixou o uv à toa


@needs_bash
def test_missing_uv_is_bootstrapped_with_the_official_installer(tmp_path):
    box = Sandbox(tmp_path, with_uv=False, with_curl=True)
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert "Instalando o uv" in result.stdout
    assert box.calls[0] == "curl -LsSf https://astral.sh/uv/install.sh"
    assert (box.home / ".local" / "bin" / "uv").exists()
    assert any(c.startswith("uv tool install") for c in box.calls)  # usou o uv recém-instalado


@needs_bash
def test_missing_curl_and_uv_fails_with_clear_message(tmp_path):
    box = Sandbox(tmp_path, with_uv=False)
    result = box.run(restricted_path=True)
    assert result.returncode == 1 and "curl nao encontrado" in result.stderr


@needs_bash
def test_failed_uv_install_is_reported_and_fails(sandbox):
    result = sandbox.run(env={"FAKE_UV_FAIL": "1"})
    assert result.returncode == 1 and "falha ao instalar o Termina" in result.stderr
    assert "Pronto" not in result.stdout


@needs_bash
@pytest.mark.parametrize(
    ("args", "env", "fragment"),
    [
        (["--extras", "rag;rm -rf /"], {}, "extras invalidos"),
        (["--extras", "RAG"], {}, "extras invalidos"),
        (["--version", "main;x"], {}, "versao invalida"),
        (["--version", "a b"], {}, "versao invalida"),
        ([], {"TERMINAS_VERSION": ""}, "ok"),
        ([], {"TERMINAS_PYTHON": "3.12;x"}, "TERMINAS_PYTHON invalido"),
        (["--foo"], {}, "opcao desconhecida"),
        (["--version"], {}, "exige um valor"),
        (["--extras"], {}, "exige um valor"),
    ],
)
def test_invalid_input_never_reaches_uv(sandbox, args, env, fragment):
    result = sandbox.run(*args, env=env)
    if fragment == "ok":  # TERMINAS_VERSION vazio cai no padrão (main), não é erro
        assert result.returncode == 0 and "archive/main.zip" in sandbox.install_call()
        return
    assert result.returncode == 1 and fragment in result.stderr
    assert sandbox.calls == []


@needs_bash
@pytest.mark.parametrize(("uname", "fragment"), [("MINGW64_NT-10.0", "install.ps1"), ("FreeBSD", "sistema nao suportado")])
def test_unsupported_systems_are_rejected_with_guidance(tmp_path, uname, fragment):
    box = Sandbox(tmp_path, uname=uname)
    result = box.run()
    assert result.returncode == 1 and fragment in result.stderr and box.calls == []


@needs_bash
def test_macos_is_accepted(tmp_path):
    result = Sandbox(tmp_path, uname="Darwin").run()
    assert result.returncode == 0 and "Pronto" in result.stdout


@needs_bash
def test_help_works_without_touching_anything(sandbox):
    result = sandbox.run("--help")
    assert result.returncode == 0 and "TERMINAS_VERSION" in result.stdout and "--uninstall" in result.stdout
    assert sandbox.calls == []
