"""Carregadores de arquivos locais para o RAG (PDF, Markdown, texto e código-fonte)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

TEXT_EXTENSIONS = frozenset({".md", ".markdown", ".txt", ".rst", ".csv", ".log"})
CODE_EXTENSIONS = frozenset(
    {
        ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".yaml", ".yml", ".toml", ".ini",
        ".cfg", ".sh", ".ps1", ".bat", ".sql", ".html", ".css", ".go", ".rs", ".java",
        ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php",
    }
)  # fmt: skip
PDF_EXTENSION = ".pdf"
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | CODE_EXTENSIONS | {PDF_EXTENSION}

IGNORED_DIRS = frozenset(
    {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".terminas", "dist", "build"}
)


class UnsupportedFileError(ValueError):
    """Extensão não suportada ou conteúdo binário."""


@dataclass(frozen=True)
class Document:
    path: Path  # caminho absoluto e resolvido
    text: str
    kind: str  # "text" | "code" | "pdf"
    content_hash: str


def is_supported(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS


def _kind(suffix: str) -> str:
    if suffix == PDF_EXTENSION:
        return "pdf"
    return "code" if suffix in CODE_EXTENSIONS else "text"


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ImportError(
            "Leitura de PDF requer 'pypdf'. Instale com: pip install -e \".[rag]\""
        ) from exc
    reader = PdfReader(path)
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def load_file(path: str | Path) -> Document:
    """Lê um arquivo suportado. Levanta `FileNotFoundError` ou `UnsupportedFileError`."""
    file = Path(path).expanduser().resolve()
    if not file.is_file():
        raise FileNotFoundError(f"Arquivo não encontrado: {file}")
    suffix = file.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFileError(f"Extensão não suportada: '{suffix or file.name}'")

    raw = file.read_bytes()
    if suffix == PDF_EXTENSION:
        text = _read_pdf(file)
    else:
        if b"\x00" in raw:
            raise UnsupportedFileError(f"Arquivo binário: {file.name}")
        text = raw.decode("utf-8-sig", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return Document(file, text, _kind(suffix), hashlib.sha256(raw).hexdigest())


def iter_files(path: str | Path, *, recursive: bool = True) -> Iterator[Path]:
    """Lista arquivos suportados de um arquivo ou diretório, ordenados, ignorando
    pastas de dependências/artefatos e diretórios ocultos."""
    root = Path(path).expanduser().resolve()
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        raise FileNotFoundError(f"Caminho não encontrado: {root}")
    pattern = "**/*" if recursive else "*"
    for file in sorted(root.glob(pattern)):
        relative_parts = file.relative_to(root).parts[:-1]
        if any(p in IGNORED_DIRS or p.startswith(".") for p in relative_parts):
            continue
        if file.is_file() and is_supported(file):
            yield file
