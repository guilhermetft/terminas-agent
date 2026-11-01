"""Detecção, máscara e remoção de chaves de API em texto livre."""

from __future__ import annotations

import re

# Ordem importa: "sk-ant-..." também casaria com o padrão genérico "sk-...".
# O lookbehind evita falsos positivos como "task-management-..." (o "sk-" no meio de uma palavra).
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic", re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai", re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}")),
    ("gemini", re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_\-]{30,}")),
    ("github", re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{30,}")),
)
PLACEHOLDER = "[chave {kind} oculta]"


def detect_secret(text: str) -> str | None:
    """Tipo da primeira chave encontrada ('anthropic', 'openai', 'gemini', 'github') ou None."""
    for kind, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return kind
    return None


def redact(text: str) -> str:
    """Troca qualquer chave reconhecível por um marcador, sem revelar parte dela."""
    for kind, pattern in _SECRET_PATTERNS:
        text = pattern.sub(PLACEHOLDER.format(kind=kind), text)
    return text


def mask(key: str) -> str:
    """Forma segura de mostrar uma chave: só o começo e o fim (ex.: 'sk-a…9Qx2')."""
    key = key.strip()
    if len(key) <= 12:
        return "****"
    return f"{key[:4]}…{key[-4:]}"
