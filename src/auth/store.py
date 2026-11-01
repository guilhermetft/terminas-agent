"""Armazenamento local das credenciais (`.env` do usuário) e do estado não secreto do login."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

HEADER = (
    "# Credenciais do Termina's Agent. Este arquivo e pessoal: nao compartilhe, nao versione.\n"
    "# Gerenciado por /login e /logout.\n"
)


def restrict_to_owner(path: Path, *, directory: bool = False) -> str | None:
    """Deixa o arquivo/pasta acessível só ao usuário atual.

    Retorna uma mensagem de aviso se não conseguiu (nunca levanta), senão None."""
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME")
            if not user:
                return "não consegui descobrir o usuário do Windows para restringir o acesso"
            grant = f"{user}:(OI)(CI)F" if directory else f"{user}:F"
            result = subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
                capture_output=True, text=True, timeout=15,
            )  # fmt: skip
            if result.returncode != 0:
                return f"icacls falhou ao restringir o acesso: {result.stderr.strip() or result.stdout.strip()}"
        else:
            os.chmod(path, 0o700 if directory else 0o600)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"não consegui restringir o acesso a {path}: {exc}"
    return None


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            values[name] = value
    return values


class CredentialStore:
    """`.env` com as chaves. Escrita atômica; o arquivo nasce já restrito ao dono."""

    def __init__(self, env_file: str | Path) -> None:
        self.path = Path(env_file).expanduser()
        self.warning: str | None = None  # último problema ao restringir permissões

    def read(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        return parse_env(self.path.read_text(encoding="utf-8"))

    def get(self, name: str) -> str | None:
        return self.read().get(name)

    def _write_lines(self, lines: list[str]) -> None:
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(directory, 0o700)
        fd, temp_name = tempfile.mkstemp(dir=directory, prefix=".env-", suffix=".tmp")
        temp = Path(temp_name)
        try:
            os.close(fd)
            # restringe ANTES de gravar o segredo (mkstemp já cria 0600 no POSIX)
            self.warning = restrict_to_owner(temp)
            temp.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def _current_lines(self) -> list[str]:
        if self.path.is_file():
            return self.path.read_text(encoding="utf-8").splitlines()
        return HEADER.rstrip("\n").splitlines()

    def set(self, name: str, value: str) -> None:
        if not name.replace("_", "").isalnum() or any(c in value for c in "\r\n"):
            raise ValueError("Nome ou valor de credencial inválido.")
        lines, replaced = [], False
        for line in self._current_lines():
            key = line.split("=", 1)[0].strip().removeprefix("export ").strip()
            if "=" in line and not line.lstrip().startswith("#") and key == name:
                if not replaced:
                    lines.append(f'{name}="{value}"')
                    replaced = True
                continue  # remove duplicatas
            lines.append(line)
        if not replaced:
            lines.append(f'{name}="{value}"')
        self._write_lines(lines)

    def remove(self, name: str) -> bool:
        if not self.path.is_file():
            return False
        kept, removed = [], False
        for line in self._current_lines():
            key = line.split("=", 1)[0].strip().removeprefix("export ").strip()
            if "=" in line and not line.lstrip().startswith("#") and key == name:
                removed = True
                continue
            kept.append(line)
        if removed:
            self._write_lines(kept)
        return removed


class AuthState:
    """Estado NÃO secreto: provedor ativo e modelo escolhido por provedor (`auth.json`)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    def read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"active": None, "models": {}}
        if not isinstance(data, dict):
            return {"active": None, "models": {}}
        models = data.get("models")
        return {
            "active": data.get("active") if isinstance(data.get("active"), str) else None,
            "models": models if isinstance(models, dict) else {},
        }

    def write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
