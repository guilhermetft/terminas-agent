"""Leitura de linhas do usuário: prompt_toolkit (histórico, autocomplete) com fallback simples."""

from __future__ import annotations

import asyncio
import getpass
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from src.utils.secrets import detect_secret

Suggester = Callable[[str], list[str]]
"""Recebe o texto digitado até o cursor e devolve candidatos para a palavra atual."""
HistoryFilter = Callable[[str], bool]
"""True = a linha pode ir para o histórico."""


def keep_in_history(line: str) -> bool:
    """O histórico nunca guarda chaves de API coladas por engano."""
    return detect_secret(line) is None


class LineReader(Protocol):
    async def read_line(self, prompt: str, *, history: bool = True) -> str | None:
        """Lê uma linha. Retorna None em fim de entrada (Ctrl+D / EOF)."""
        ...

    async def read_secret(self, prompt: str) -> str | None:
        """Lê um segredo (chave de API) sem mostrar o que é digitado e sem guardar no histórico."""
        ...


# O Windows PowerShell 5.1 prefixa um BOM UTF-8 ao enviar texto por pipe a um programa nativo.
# O Python o le como U+FEFF (entrada em UTF-8) ou como U+00EF U+00BB U+00BF (cp1252), e o "/comando" deixaria de ser comando.
_BOM_PREFIXES = ("\ufeff", "\u00ef\u00bb\u00bf")


def _strip_bom(line: str) -> str:
    for bom in _BOM_PREFIXES:
        line = line.removeprefix(bom)
    return line


class PlainReader:
    """`input()` numa thread: funciona em qualquer terminal e com entrada redirecionada."""

    async def read_line(self, prompt: str, *, history: bool = True) -> str | None:
        try:
            line = await asyncio.to_thread(input, prompt)
        except EOFError:
            return None
        return _strip_bom(line)

    async def read_secret(self, prompt: str) -> str | None:
        # Em terminal: sem eco (getpass). Com entrada redirecionada não há eco a esconder, e o getpass
        # (que lê o console, não o stdin) travaria; lê a linha normalmente.
        reader = getpass.getpass if sys.stdin is not None and sys.stdin.isatty() else input
        try:
            line = await asyncio.to_thread(reader, prompt)
        except EOFError:
            return None
        return _strip_bom(line)


class PromptToolkitReader:
    """Entrada interativa com histórico persistente e autocomplete de comandos.

    Funciona igual no Windows Terminal e em terminais Linux (prompt_toolkit abstrai o
    console de cada sistema)."""

    def __init__(
        self,
        suggester: Suggester,
        history_file: str | Path | None = None,
        *,
        history_filter: HistoryFilter = keep_in_history,
        input: Any = None,  # noqa: A002 - injetável em testes
        output: Any = None,
    ) -> None:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import FileHistory, InMemoryHistory

        class CommandCompleter(Completer):
            def get_completions(self, document, complete_event):  # noqa: ANN001, ANN202
                text = document.text_before_cursor
                partial = text.rsplit(None, 1)[-1] if text and not text[-1].isspace() else ""
                for candidate in suggester(text):
                    yield Completion(candidate, start_position=-len(partial))

        class SafeFileHistory(FileHistory):
            def append_string(self, string: str) -> None:
                if history_filter(string):
                    super().append_string(string)

        class SafeMemoryHistory(InMemoryHistory):
            def append_string(self, string: str) -> None:
                if history_filter(string):
                    super().append_string(string)

        self.completer = CommandCompleter()
        if history_file is not None:
            path = Path(history_file).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            history: Any = SafeFileHistory(str(path))
        else:
            history = SafeMemoryHistory()
        self._session = PromptSession(
            history=history, completer=self.completer, complete_while_typing=False,
            input=input, output=output,
        )  # fmt: skip
        # Sessão separada para respostas de modais (y/a/n/m): não polui o histórico de comandos.
        self._modal = PromptSession(history=InMemoryHistory(), input=input, output=output)
        # Segredos: sem eco (is_password) e sem histórico.
        self._secret = PromptSession(history=InMemoryHistory(), is_password=True, input=input, output=output)

    async def read_line(self, prompt: str, *, history: bool = True) -> str | None:
        session = self._session if history else self._modal
        try:
            return await session.prompt_async(prompt)
        except EOFError:
            return None

    async def read_secret(self, prompt: str) -> str | None:
        try:
            return await self._secret.prompt_async(prompt)
        except EOFError:
            return None


def make_reader(suggester: Suggester, history_file: str | Path | None) -> LineReader:
    """prompt_toolkit em terminal interativo; `input()` simples quando a entrada é redirecionada."""
    if sys.stdin is not None and sys.stdin.isatty():
        try:
            return PromptToolkitReader(suggester, history_file)
        except Exception:  # console sem suporte (ex.: terminal "burro"): cai no modo simples
            return PlainReader()
    return PlainReader()
