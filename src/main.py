"""Ponto de entrada do Termina's Agent."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from src import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="terminas",
        description="Termina's Agent - Agente Mestre para Copiloto Pessoal, RAG e Multi-Agente.",
    )
    parser.add_argument("--version", action="version", version=f"terminas {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
