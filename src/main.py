"""Ponto de entrada do Termina's Agent."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import Any

from rich.console import Console

from src import __version__
from src.memory.config import ConfigError, load_config

EXIT_OK, EXIT_PARTIAL, EXIT_ERROR, EXIT_INTERRUPTED = 0, 1, 2, 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="terminas",
        description="Termina's Agent - Agente Mestre para Copiloto Pessoal, RAG e Multi-Agente.",
        epilog="Sem subcomando, abre o terminal interativo (/help lista os comandos).",
    )
    parser.add_argument("--version", action="version", version=f"terminas {__version__}")
    parser.add_argument("--config", metavar="ARQUIVO", help="caminho do config.yaml (padrão: ./config.yaml)")
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run", help="planeja e executa uma meta e sai")
    run.add_argument("goal", nargs="+", help="a meta em linguagem natural")
    return parser


def _make_console() -> Console:
    # Terminais Windows com codificação legada não podem falhar ao imprimir ✔/●/⚠.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    return Console()


async def _amain(args: argparse.Namespace, config: Any, overrides: dict[str, Any]) -> int:
    from src.ui.cli import TerminasApp
    from src.ui.prompt import PlainReader, make_reader
    from src.ui.services import start_services, stop_services

    console = overrides.get("console") or _make_console()
    services = await start_services(
        config,
        llm=overrides.get("llm"),
        embedder=overrides.get("embedder"),
        runner=overrides.get("runner"),
    )
    try:
        app = TerminasApp(services, console, PlainReader())
        app.reader = overrides.get("reader") or make_reader(app.suggestions, config.history_file)
        if args.command == "run":
            services.memory.start_session("run")
            report = await app.plan_and_run(" ".join(args.goal))
            services.memory.end_session()
            if report is None:
                return EXIT_ERROR
            return EXIT_OK if report.success else EXIT_PARTIAL
        await app.run()
        return EXIT_OK
    finally:
        await stop_services(services)


def main(argv: Sequence[str] | None = None, **overrides: Any) -> int:
    """Executa o Termina. `overrides` (console, reader, llm, embedder, runner) servem a testes."""
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"terminas: {exc}", file=sys.stderr)
        return EXIT_ERROR
    try:
        return asyncio.run(_amain(args, config, overrides))
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())
