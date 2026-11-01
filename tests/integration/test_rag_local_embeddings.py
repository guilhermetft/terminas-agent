"""CT-03 com embeddings semânticos reais (FastEmbed, CPU, local).

Pula se `fastembed` não estiver instalado ou se o modelo não puder ser carregado
(1º uso baixa o modelo uma vez para `.terminas/models`; depois roda offline).
"""

from pathlib import Path

import pytest

from src.rag import FastEmbedEmbedder, RAGEngine

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "manual.txt"


@pytest.fixture(scope="module")
def embedder():
    pytest.importorskip("fastembed")
    emb = FastEmbedEmbedder()
    try:
        emb.embed(["aquecimento"])
    except Exception as exc:  # modelo indisponível (sem rede e sem cache)
        pytest.skip(f"modelo de embeddings indisponível: {exc!r}")
    return emb


def test_ct03_semantic_retrieval_score_above_threshold(embedder, tmp_path):
    with RAGEngine(tmp_path / "rag.sqlite", embedder, chunk_size=60, overlap=0) as rag:
        rag.index_file(FIXTURE)
        hits = rag.query("Qual a porta padrão do serviço?")

    assert "8080" in hits[0].content
    assert hits[0].lines == "3"
    assert hits[0].score > 0.75
    assert hits[0].score > hits[1].score


def test_semantic_query_finds_paraphrase_without_shared_words(embedder, tmp_path):
    with RAGEngine(tmp_path / "rag.sqlite", embedder, chunk_size=60, overlap=0) as rag:
        rag.index_file(FIXTURE)
        top = rag.query("onde o banco de dados é criado?", top_k=1)[0]
    assert "SQLite" in top.content
