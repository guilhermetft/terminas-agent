"""Leitura de linhas do usuário: prompt_toolkit (histórico, autocomplete) com fallback simples."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

Suggester = Callable[[str], list[str]]
"""Recebe o texto digitado até o cursor e devolve candidatos para a palavra atual."""


class LineReader(Protocol):
    async def read_line(self, prompt: str, *, history: bool = True) -> str | None:
        """Lê uma linha. Retorna None em fim de entrada (Ctrl+D / EOF)."""
        ...


# O Windows PowerShell 5.1 prefixa um BOM UTF-8 ao enviar texto por pipe a um programa nativo.
# O Python o le como U+FEFF (entrada em UTF-8) ou como U+00EF U+00BB U+00BF (cp1252), e o "/comando" deixaria de ser comando.
_BOM_PREFIXES = ("\ufeff", "\u00ef\u00bb\u00bf")


class PlainReader:
    """`input()` numa thread: funciona em qualquer terminal e com entrada redirecionada."""

    async def read_line(self, prompt: str, *, history: bool = True) -> str | None:
        try:
            line = await asyncio.to_thread(input, prompt)
        except EOFError:
            return None
        for bom in _BOM_PREFIXES:
            line = line.removeprefix(bom)
        return line


class PromptToolkitReader:
    """Entrada interativa com histórico persistente e autocomplete de comandos.

    Funciona igual no Windows Terminal e em terminais Linux (prompt_toolkit abstrai o
    console de cada sistema)."""

    def __init__(
        self,
        suggester: Suggester,
        history_file: str | Path | None = None,
        *,
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

        self.completer = CommandCompleter()
        if history_file is not None:
            path = Path(history_file).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            history: Any = FileHistory(str(path))
        else:
            history = InMemoryHistory()
        self._session = PromptSession(
            history=history, completer=self.completer, complete_while_typing=False,
            input=input, output=output,
        )  # fmt: skip
        # Sessão separada para respostas de modais (y/a/n/m): não polui o histórico de comandos.
        self._modal = PromptSession(history=InMemoryHistory(), input=input, output=output)

    async def read_line(self, prompt: str, *, history: bool = True) -> str | None:
        session = self._session if history else self._modal
        try:
            return await session.prompt_async(prompt)
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
