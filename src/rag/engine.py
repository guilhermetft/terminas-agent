"""RAGEngine: indexa arquivos locais e responde consultas com citação de arquivo e linhas."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from src.rag.chunking import DEFAULT_CHUNK_SIZE, DEFAULT_OVERLAP, chunk_text
from src.rag.embeddings import Embedder, default_embedder
from src.rag.ingestion import UnsupportedFileError, iter_files, load_file
from src.rag.vector_store import DEFAULT_RAG_DB_PATH, SearchHit, StoredChunk, VectorStore
from src.subagents_bridge.schemas import RagChunk

logger = logging.getLogger(__name__)

EMBED_BATCH_SIZE = 32


class EmbedderMismatchError(RuntimeError):
    """O índice foi criado com outro embedder/dimensão; reindexe ou use o mesmo embedder."""


@dataclass(frozen=True)
class IndexResult:
    path: str
    chunks: int
    skipped: bool  # True se o arquivo não mudou desde a última indexação


@dataclass
class IndexReport:
    indexed: list[IndexResult] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)

    @property
    def files_indexed(self) -> int:
        return sum(1 for r in self.indexed if not r.skipped)

    @property
    def files_unchanged(self) -> int:
        return sum(1 for r in self.indexed if r.skipped)

    @property
    def chunks(self) -> int:
        return sum(r.chunks for r in self.indexed if not r.skipped)


class RAGEngine:
    """Pipeline completo: ingestão -> chunking -> embeddings locais -> SQLite -> busca.

    Síncrono (CPU-bound); em código assíncrono chame via `asyncio.to_thread`.
    """

    def __init__(
        self,
        db_path: str | Path = DEFAULT_RAG_DB_PATH,
        embedder: Embedder | None = None,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        overlap: int = DEFAULT_OVERLAP,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> None:
        chunk_text("", chunk_size, overlap)  # valida parâmetros cedo
        self.store = VectorStore(db_path)
        self.embedder = embedder or default_embedder()
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.top_k = top_k
        self.min_score = min_score

    # ---------------------------------------------------------------- índice
    def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            vectors.extend(self.embedder.embed(texts[start : start + EMBED_BATCH_SIZE]))
        return vectors

    def _check_space(self, dim: int) -> None:
        """Garante que o índice usa sempre o mesmo embedder e dimensão."""
        recorded = self.store.get_meta("embedder")
        recorded_dim = self.store.get_meta("dim")
        if recorded is None:
            self.store.set_meta("embedder", self.embedder.name)
            self.store.set_meta("dim", str(dim))
        elif recorded != self.embedder.name or recorded_dim != str(dim):
            raise EmbedderMismatchError(
                f"Índice criado com '{recorded}' (dim {recorded_dim}); "
                f"embedder atual: '{self.embedder.name}' (dim {dim})."
            )

    def _check_recorded_embedder(self) -> None:
        recorded = self.store.get_meta("embedder")
        if recorded is not None and recorded != self.embedder.name:
            raise EmbedderMismatchError(
                f"Índice criado com '{recorded}'; embedder atual: '{self.embedder.name}'."
            )

    def index_file(self, path: str | Path, *, force: bool = False) -> IndexResult:
        """Indexa (ou reindexa, se mudou) um arquivo. Sem mudanças, pula sem recalcular."""
        self._check_recorded_embedder()
        doc = load_file(path)
        key = doc.path.as_posix()
        if not force and self.store.get_document_hash(key) == doc.content_hash:
            return IndexResult(key, 0, skipped=True)

        chunks = chunk_text(doc.text, self.chunk_size, self.overlap)
        vectors = self._embed([c.text for c in chunks])
        if vectors:
            self._check_space(len(vectors[0]))
        self.store.upsert_document(
            key,
            doc.content_hash,
            doc.kind,
            [
                StoredChunk(c.index, c.start_line, c.end_line, c.text, v)
                for c, v in zip(chunks, vectors, strict=True)
            ],
        )
        return IndexResult(key, len(chunks), skipped=False)

    def index_path(
        self, path: str | Path, *, recursive: bool = True, force: bool = False
    ) -> IndexReport:
        """Indexa um arquivo ou diretório. Arquivos ilegíveis são reportados, não abortam."""
        report = IndexReport()
        for file in iter_files(path, recursive=recursive):
            try:
                report.indexed.append(self.index_file(file, force=force))
            except UnsupportedFileError as exc:
                logger.info("Ignorado: %s", exc)
                report.unsupported.append(file.as_posix())
        return report

    def remove(self, path: str | Path) -> bool:
        return self.store.remove_document(Path(path).expanduser().resolve().as_posix())

    def stats(self) -> dict[str, int]:
        return self.store.stats()

    def documents(self) -> list[dict[str, str | int]]:
        return self.store.list_documents()

    # ----------------------------------------------------------------- busca
    def query(
        self, question: str, *, top_k: int | None = None, min_score: float | None = None
    ) -> list[SearchHit]:
        """Top-K trechos mais similares à pergunta, ordenados por score (cosseno) decrescente."""
        question = question.strip()
        if not question:
            raise ValueError("A pergunta não pode ser vazia.")
        self._check_recorded_embedder()
        if self.store.stats()["chunks"] == 0:
            return []
        (vector,) = self.embedder.embed([question])
        self._check_space(len(vector))
        return self.store.search(
            vector,
            top_k=self.top_k if top_k is None else top_k,
            min_score=self.min_score if min_score is None else min_score,
        )

    @staticmethod
    def to_rag_chunks(hits: list[SearchHit]) -> list[RagChunk]:
        """Converte resultados para o contrato A2A (`context.relevant_rag_chunks`)."""
        return [RagChunk(file=h.path, lines=h.lines, content=h.content) for h in hits]

    @staticmethod
    def format_context(hits: list[SearchHit]) -> str:
        """Texto pronto para injetar em prompt, com citação `arquivo:linhas` por trecho."""
        return "\n\n".join(f"[{h.citation}] (score {h.score:.2f})\n{h.content}" for h in hits)

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> RAGEngine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
