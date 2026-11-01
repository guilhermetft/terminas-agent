"""Subsistema de RAG documental local (ingestão, chunking, embeddings e busca vetorial)."""

from src.rag.chunking import Chunk, chunk_text
from src.rag.embeddings import (
    Embedder,
    FastEmbedEmbedder,
    HashingEmbedder,
    default_embedder,
)
from src.rag.engine import EmbedderMismatchError, IndexReport, IndexResult, RAGEngine
from src.rag.ingestion import Document, UnsupportedFileError, iter_files, load_file
from src.rag.vector_store import SearchHit, VectorStore, cosine_similarity

__all__ = [
    "Chunk",
    "Document",
    "Embedder",
    "EmbedderMismatchError",
    "FastEmbedEmbedder",
    "HashingEmbedder",
    "IndexReport",
    "IndexResult",
    "RAGEngine",
    "SearchHit",
    "UnsupportedFileError",
    "VectorStore",
    "chunk_text",
    "cosine_similarity",
    "default_embedder",
    "iter_files",
    "load_file",
]
