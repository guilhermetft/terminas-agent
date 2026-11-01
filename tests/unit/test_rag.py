"""Testes unitários do RAG local (CT-03; RF-05, RNF-06). Embeddings determinísticos, sem rede."""

import logging
import math
import sys
import types
from pathlib import Path

import pytest

from src.rag import (
    EmbedderMismatchError,
    FastEmbedEmbedder,
    HashingEmbedder,
    RAGEngine,
    UnsupportedFileError,
    VectorStore,
    chunk_text,
    cosine_similarity,
    default_embedder,
    iter_files,
    load_file,
)
from src.rag.embeddings import normalize
from src.rag.vector_store import StoredChunk
from src.subagents_bridge import AgentTask

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "manual.txt"


def make_pdf(text: str) -> bytes:
    """PDF mínimo de 1 página com texto extraível (sem dependência de gerador de PDF)."""
    stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 200] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


@pytest.fixture
def engine(tmp_path):
    with RAGEngine(tmp_path / "rag.sqlite", HashingEmbedder(), chunk_size=60, overlap=0) as eng:
        yield eng


# ----------------------------------------------------------------- chunking
def test_chunk_empty_and_whitespace_text():
    assert chunk_text("") == []
    assert chunk_text("\n\n   \n") == []


def test_chunk_single_block_keeps_line_numbers():
    chunks = chunk_text("um\ndois\ntres", chunk_size=100, overlap=0)
    assert len(chunks) == 1
    assert (chunks[0].start_line, chunks[0].end_line, chunks[0].lines) == (1, 3, "1-3")
    assert chunks[0].text == "um\ndois\ntres"


def test_chunk_single_line_lines_format():
    assert chunk_text("só uma linha")[0].lines == "1"


def test_chunk_strips_blank_edges_from_citation():
    (chunk,) = chunk_text("\n\nconteúdo\n\n", chunk_size=100, overlap=0)
    assert (chunk.start_line, chunk.end_line) == (3, 3)


def test_chunk_prefers_paragraph_boundary():
    text = "aaaa aaaa\naaaa aaaa\n\nbbbb bbbb\nbbbb bbbb"
    chunks = chunk_text(text, chunk_size=26, overlap=0)
    assert [c.text for c in chunks] == ["aaaa aaaa\naaaa aaaa", "bbbb bbbb\nbbbb bbbb"]
    assert [c.lines for c in chunks] == ["1-2", "4-5"]


def test_chunk_overlap_repeats_trailing_lines():
    text = "\n".join(f"linha{i:02d}" for i in range(1, 9))  # 8 linhas de 7 chars
    chunks = chunk_text(text, chunk_size=24, overlap=8)
    assert len(chunks) > 1
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert current.start_line <= previous.end_line  # há sobreposição
        assert current.start_line > previous.start_line  # e há progresso
    assert chunks[-1].end_line == 8
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_chunk_covers_every_line_without_overlap():
    text = "\n".join(f"l{i}" for i in range(1, 40))
    chunks = chunk_text(text, chunk_size=30, overlap=0)
    covered = {n for c in chunks for n in range(c.start_line, c.end_line + 1)}
    assert covered == set(range(1, 40))
    assert all(len(c.text) <= 30 for c in chunks)


def test_chunk_splits_very_long_line():
    chunks = chunk_text("x" * 250, chunk_size=100, overlap=0)
    assert [len(c.text) for c in chunks] == [100, 100, 50]
    assert {c.lines for c in chunks} == {"1"}


@pytest.mark.parametrize(("size", "overlap"), [(0, 0), (10, 10), (10, -1)])
def test_chunk_invalid_params(size, overlap):
    with pytest.raises(ValueError):
        chunk_text("texto", size, overlap)


# ---------------------------------------------------------------- ingestion
def test_load_text_markdown_and_code(tmp_path):
    (tmp_path / "a.txt").write_bytes("olá\r\nmundo".encode("utf-8"))  # CRLF preservado
    (tmp_path / "b.md").write_text("# Título", encoding="utf-8")
    (tmp_path / "c.py").write_text("print('x')\n", encoding="utf-8")
    assert load_file(tmp_path / "a.txt").text == "olá\nmundo"
    assert load_file(tmp_path / "a.txt").kind == "text"
    assert load_file(tmp_path / "b.md").kind == "text"
    doc = load_file(tmp_path / "c.py")
    assert doc.kind == "code" and doc.path.is_absolute() and len(doc.content_hash) == 64


def test_load_handles_bom_and_bad_bytes(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"\xef\xbb\xbfabc\xff")
    assert load_file(tmp_path / "a.txt").text == "abc�"


def test_load_pdf_extracts_text(tmp_path):
    (tmp_path / "doc.pdf").write_bytes(make_pdf("Porta padrao 8080"))
    doc = load_file(tmp_path / "doc.pdf")
    assert doc.kind == "pdf" and "Porta padrao 8080" in doc.text


def test_load_pdf_without_pypdf_gives_clear_error(tmp_path, monkeypatch):
    (tmp_path / "doc.pdf").write_bytes(make_pdf("x"))
    monkeypatch.setitem(sys.modules, "pypdf", None)  # força ImportError
    with pytest.raises(ImportError, match="pypdf"):
        load_file(tmp_path / "doc.pdf")


def test_load_errors(tmp_path):
    (tmp_path / "x.exe").write_bytes(b"MZ")
    (tmp_path / "bin.txt").write_bytes(b"ab\x00cd")
    with pytest.raises(FileNotFoundError):
        load_file(tmp_path / "nao-existe.txt")
    with pytest.raises(UnsupportedFileError, match="Extensão"):
        load_file(tmp_path / "x.exe")
    with pytest.raises(UnsupportedFileError, match="binário"):
        load_file(tmp_path / "bin.txt")


def test_iter_files_filters_and_orders(tmp_path):
    for name in ["b.txt", "a.md", "img.png", "sub/c.py", ".git/x.txt", "node_modules/y.js",
                 ".hidden/z.txt", "__pycache__/w.py"]:  # fmt: skip
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    found = [p.relative_to(tmp_path.resolve()).as_posix() for p in iter_files(tmp_path)]
    assert found == ["a.md", "b.txt", "sub/c.py"]
    flat = [p.name for p in iter_files(tmp_path, recursive=False)]
    assert flat == ["a.md", "b.txt"]
    assert [p.name for p in iter_files(tmp_path / "b.txt")] == ["b.txt"]
    with pytest.raises(FileNotFoundError):
        list(iter_files(tmp_path / "nada"))


# --------------------------------------------------------------- embeddings
def test_hashing_embedder_is_deterministic_normalized_and_accent_insensitive():
    emb = HashingEmbedder(64)
    a, b, c = emb.embed(["Porta Padrão", "porta padrao", "banco de dados"])
    assert a == b and len(a) == 64
    assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0, rel_tol=1e-9)
    assert cosine_similarity(a, c) < cosine_similarity(a, b)
    assert emb.embed(["Porta Padrão"]) == [a]
    assert emb.name == "hashing-64"


def test_hashing_embedder_empty_text_and_bad_dim():
    assert HashingEmbedder(16).embed([""]) == [[0.0] * 16]
    with pytest.raises(ValueError):
        HashingEmbedder(4)


def test_normalize_zero_vector():
    assert normalize([0, 0]) == [0.0, 0.0]
    assert normalize([3, 4]) == [0.6, 0.8]


class _FakeArray(list):
    def tolist(self):
        return list(self)


def _fake_fastembed(monkeypatch, calls):
    class TextEmbedding:
        def __init__(self, model_name, cache_dir=None):
            calls.append((model_name, cache_dir))

        def embed(self, texts):
            return iter(_FakeArray([3.0, 4.0]) for _ in texts)

    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=TextEmbedding))


def test_fastembed_embedder_lazy_loads_once_and_normalizes(monkeypatch, tmp_path):
    calls = []
    _fake_fastembed(monkeypatch, calls)
    emb = FastEmbedEmbedder("meu/modelo", cache_dir=tmp_path)
    assert calls == []  # nada carregado no construtor
    assert emb.embed([]) == []
    assert emb.embed(["a", "b"]) == [[0.6, 0.8], [0.6, 0.8]]
    emb.embed(["c"])
    assert calls == [("meu/modelo", str(tmp_path))]
    assert emb.name == "fastembed:meu/modelo"


def test_fastembed_missing_gives_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)
    with pytest.raises(ImportError, match="FastEmbed"):
        FastEmbedEmbedder().embed(["x"])


def test_default_embedder_prefers_fastembed_else_hashing(monkeypatch, caplog):
    _fake_fastembed(monkeypatch, [])
    assert isinstance(default_embedder(), FastEmbedEmbedder)
    monkeypatch.setitem(sys.modules, "fastembed", None)
    with caplog.at_level(logging.WARNING):
        assert isinstance(default_embedder(), HashingEmbedder)
    assert "FastEmbed ausente" in caplog.text


# ------------------------------------------------------------- vector store
def test_cosine_similarity_values():
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)
    assert cosine_similarity([2, 2], [1, 1]) == pytest.approx(1.0)  # ignora magnitude
    assert cosine_similarity([0, 0], [1, 1]) == 0.0
    with pytest.raises(ValueError, match="Dimensões"):
        cosine_similarity([1], [1, 2])


@pytest.fixture
def vstore(tmp_path):
    with VectorStore(tmp_path / "v.sqlite") as store:
        yield store


def _chunks(*vectors):
    return [StoredChunk(i, i + 1, i + 1, f"c{i}", v) for i, v in enumerate(vectors)]


def test_vector_store_search_orders_by_cosine_and_cites_lines(vstore):
    vstore.upsert_document("/a.txt", "h1", "text", _chunks([1, 0], [0.9, 0.1], [0, 1]))
    hits = vstore.search([1, 0], top_k=2)
    assert [h.content for h in hits] == ["c0", "c1"]
    assert hits[0].score == pytest.approx(1.0, abs=1e-6)
    assert hits[0].score > hits[1].score
    assert (hits[0].path, hits[0].lines, hits[0].citation) == ("/a.txt", "1", "/a.txt:1")


def test_vector_store_min_score_and_top_k(vstore):
    vstore.upsert_document("/a.txt", "h", "text", _chunks([1, 0], [0.9, 0.1], [0, 1]))
    assert [h.content for h in vstore.search([1, 0], top_k=10, min_score=0.9)] == ["c0", "c1"]
    assert vstore.search([1, 0], top_k=10, min_score=0.9999)[0].content == "c0"
    assert len(vstore.search([1, 0], top_k=1)) == 1
    with pytest.raises(ValueError, match="top_k"):
        vstore.search([1, 0], top_k=0)


def test_vector_store_hit_multiline_citation(vstore):
    vstore.upsert_document("/a.txt", "h", "text", [StoredChunk(0, 10, 25, "x", [1, 0])])
    assert vstore.search([1, 0])[0].lines == "10-25"


def test_vector_store_search_dimension_mismatch_and_empty(vstore):
    assert vstore.search([1, 0]) == []
    vstore.upsert_document("/a.txt", "h", "text", _chunks([1, 0]))
    with pytest.raises(ValueError, match="Dimensão"):
        vstore.search([1, 0, 0])


def test_vector_store_upsert_replaces_remove_and_stats(vstore):
    vstore.upsert_document("/a.txt", "h1", "text", _chunks([1, 0], [0, 1]))
    vstore.upsert_document("/b.py", "h2", "code", _chunks([1, 1]))
    assert vstore.stats() == {"documents": 2, "chunks": 3}
    vstore.upsert_document("/a.txt", "h1b", "text", _chunks([1, 0]))  # substitui
    assert vstore.stats() == {"documents": 2, "chunks": 2}
    assert vstore.get_document_hash("/a.txt") == "h1b"
    assert vstore.get_document_hash("/nada") is None
    assert [d["path"] for d in vstore.list_documents()] == ["/a.txt", "/b.py"]
    assert vstore.remove_document("/a.txt") is True
    assert vstore.remove_document("/a.txt") is False
    assert vstore.stats() == {"documents": 1, "chunks": 1}  # cascade apaga os chunks


def test_vector_store_meta_and_persistence(tmp_path):
    path = tmp_path / "v.sqlite"
    with VectorStore(path) as store:
        assert store.get_meta("k") is None
        store.set_meta("k", "1")
        store.set_meta("k", "2")
        store.upsert_document("/a.txt", "h", "text", _chunks([1, 0]))
    with VectorStore(path) as again:  # sobrevive à reabertura
        assert again.get_meta("k") == "2"
        assert again.search([1, 0])[0].content == "c0"


# ------------------------------------------------------------------- engine
def test_ct03_index_and_query_returns_right_chunk_with_citation(engine):
    result = engine.index_file(FIXTURE)
    assert result.chunks >= 4 and not result.skipped

    hits = engine.query("Qual a porta padrão do serviço?")
    assert "8080" in hits[0].content
    assert hits[0].lines == "3" and hits[0].path == FIXTURE.resolve().as_posix()
    assert hits[0].score > hits[1].score
    assert all(0.0 <= h.score <= 1.0 + 1e-6 for h in hits)


def test_query_other_topics_hit_other_chunks(engine):
    engine.index_file(FIXTURE)
    assert "JSONL" in engine.query("formato dos logs de auditoria", top_k=1)[0].content
    assert "SQLite" in engine.query("onde fica o banco SQLite", top_k=1)[0].content


def test_query_respects_top_k_and_min_score(engine):
    engine.index_file(FIXTURE)
    assert len(engine.query("porta", top_k=2)) == 2
    assert engine.query("porta", min_score=0.99) == []
    engine.min_score = 0.0
    engine.top_k = 1
    assert len(engine.query("porta")) == 1


def test_query_empty_question_and_empty_index(engine):
    with pytest.raises(ValueError, match="vazia"):
        engine.query("   ")
    assert engine.query("qualquer coisa") == []  # índice vazio


def test_index_skips_unchanged_reindexes_changed_and_force(engine, tmp_path):
    doc = tmp_path / "n.txt"
    doc.write_text("a porta é 8080\n", encoding="utf-8")
    first = engine.index_file(doc)
    assert not first.skipped
    assert engine.index_file(doc).skipped
    assert not engine.index_file(doc, force=True).skipped

    doc.write_text("a porta é 9090\n", encoding="utf-8")
    assert not engine.index_file(doc).skipped
    assert engine.stats() == {"documents": 1, "chunks": 1}  # substituiu, não duplicou
    assert "9090" in engine.query("porta")[0].content


def test_index_path_directory_reports_unsupported(engine, tmp_path):
    (tmp_path / "ok.md").write_text("# Titulo\ntexto", encoding="utf-8")
    (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "bin.txt").write_bytes(b"a\x00b")
    (tmp_path / "ignored.png").write_bytes(b"png")

    report = engine.index_path(tmp_path)
    assert report.files_indexed == 2 and report.files_unchanged == 0 and report.chunks == 2
    assert [Path(p).name for p in report.unsupported] == ["bin.txt"]

    again = engine.index_path(tmp_path)
    assert again.files_indexed == 0 and again.files_unchanged == 2 and again.chunks == 0
    assert [Path(d["path"]).name for d in engine.documents()] == ["code.py", "ok.md"]


def test_index_pdf_and_query(engine, tmp_path):
    (tmp_path / "doc.pdf").write_bytes(make_pdf("A senha do cofre e abracadabra"))
    engine.index_file(tmp_path / "doc.pdf")
    assert "abracadabra" in engine.query("senha do cofre", top_k=1)[0].content


def test_empty_file_indexes_zero_chunks(engine, tmp_path):
    (tmp_path / "vazio.txt").write_text("", encoding="utf-8")
    assert engine.index_file(tmp_path / "vazio.txt").chunks == 0
    assert engine.stats() == {"documents": 1, "chunks": 0}


def test_remove_document(engine, tmp_path):
    doc = tmp_path / "r.txt"
    doc.write_text("conteúdo", encoding="utf-8")
    engine.index_file(doc)
    assert engine.remove(doc) is True
    assert engine.remove(doc) is False
    assert engine.stats()["documents"] == 0


def test_embedding_runs_in_batches(tmp_path):
    seen = []

    class Spy(HashingEmbedder):
        def embed(self, texts):
            seen.append(len(texts))
            return super().embed(texts)

    big = tmp_path / "big.txt"
    big.write_text("\n".join(f"linha numero {i}" for i in range(100)), encoding="utf-8")
    with RAGEngine(tmp_path / "r.sqlite", Spy(), chunk_size=20, overlap=0) as eng:
        eng.index_file(big)
    assert len(seen) > 1 and max(seen) <= 32


def test_index_persists_across_reopen(tmp_path):
    path = tmp_path / "r.sqlite"
    with RAGEngine(path, HashingEmbedder(), chunk_size=60, overlap=0) as eng:
        eng.index_file(FIXTURE)
    with RAGEngine(path, HashingEmbedder(), chunk_size=60, overlap=0) as eng:
        assert "8080" in eng.query("porta padrão")[0].content


def test_embedder_mismatch_is_rejected(tmp_path):
    path = tmp_path / "r.sqlite"
    with RAGEngine(path, HashingEmbedder(64)) as eng:
        eng.index_file(FIXTURE)
    with RAGEngine(path, HashingEmbedder(128)) as eng:
        with pytest.raises(EmbedderMismatchError, match="hashing-64"):
            eng.query("porta")
        with pytest.raises(EmbedderMismatchError):
            eng.index_file(FIXTURE, force=True)


def test_dimension_mismatch_with_same_embedder_name(tmp_path):
    class Fixed:
        name = "fixo"

        def __init__(self, dim):
            self.dim = dim

        def embed(self, texts):
            return [[1.0] + [0.0] * (self.dim - 1) for _ in texts]

    path = tmp_path / "r.sqlite"
    with RAGEngine(path, Fixed(4)) as eng:
        eng.index_file(FIXTURE)
    with RAGEngine(path, Fixed(8)) as eng:
        with pytest.raises(EmbedderMismatchError, match="dim 4"):
            eng.query("porta")


def test_default_embedder_used_when_none_given(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)
    with RAGEngine(tmp_path / "r.sqlite") as eng:
        assert isinstance(eng.embedder, HashingEmbedder)


def test_invalid_chunk_params_fail_fast(tmp_path):
    with pytest.raises(ValueError):
        RAGEngine(tmp_path / "r.sqlite", HashingEmbedder(), chunk_size=10, overlap=10)


def test_to_rag_chunks_matches_a2a_contract(engine):
    engine.index_file(FIXTURE)
    hits = engine.query("porta padrão", top_k=2)
    chunks = RAGEngine.to_rag_chunks(hits)
    assert [c.lines for c in chunks] == [h.lines for h in hits]
    task = AgentTask(
        assigned_agent="dev-agent", goal="g", context={"relevant_rag_chunks": chunks}
    )  # valida contra o schema A2A
    assert task.context.relevant_rag_chunks[0].file == FIXTURE.resolve().as_posix()


def test_format_context_has_citations(engine):
    engine.index_file(FIXTURE)
    text = RAGEngine.format_context(engine.query("porta padrão", top_k=1))
    assert text.startswith(f"[{FIXTURE.resolve().as_posix()}:3] (score ")
    assert "8080" in text
