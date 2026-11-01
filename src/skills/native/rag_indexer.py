"""rag-indexer: ingestão e consulta semântica de arquivos locais (`/rag index` e `/rag ask`)."""

from __future__ import annotations

from typing import ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.skills.base import Skill, SkillError, SkillResult


class RagIndexerParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["index", "ask", "status"]
    path: str | None = None
    question: str | None = None
    top_k: int = Field(default=5, ge=1, le=20)
    force: bool = False

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.op == "index" and not self.path:
            raise ValueError("'index' exige 'path'.")
        if self.op == "ask" and not (self.question and self.question.strip()):
            raise ValueError("'ask' exige 'question'.")
        return self


class RagIndexer(Skill):
    name: ClassVar[str] = "rag-indexer"
    description: ClassVar[str] = (
        "Indexa arquivos locais (PDF, Markdown, texto, código) e responde consultas "
        "semânticas com citação de arquivo e linhas."
    )
    category: ClassVar[str] = "Conhecimento Local"
    Params: ClassVar[type[BaseModel]] = RagIndexerParams

    async def run(self, params: RagIndexerParams) -> SkillResult:
        import asyncio

        rag = self.ctx.require("rag", "Motor RAG")
        if params.op == "status":
            stats = rag.stats()
            return SkillResult.success(
                f"{stats['documents']} documento(s), {stats['chunks']} trecho(s) indexados.", **stats
            )
        try:
            if params.op == "index":
                path = self.ctx.resolve_inside_workdir(str(params.path))
                report = await asyncio.to_thread(rag.index_path, path, force=params.force)
                return SkillResult.success(
                    f"{report.files_indexed} arquivo(s) indexado(s), {report.files_unchanged} "
                    f"sem mudança, {len(report.unsupported)} ignorado(s); {report.chunks} trecho(s).",
                    files_indexed=report.files_indexed,
                    files_unchanged=report.files_unchanged,
                    unsupported=report.unsupported,
                    chunks=report.chunks,
                )
            hits = await asyncio.to_thread(rag.query, str(params.question), top_k=params.top_k)
        except (FileNotFoundError, ValueError, ImportError, RuntimeError) as exc:
            raise SkillError(str(exc)) from exc
        return SkillResult.success(
            rag.format_context(hits) if hits else "Nada relevante encontrado.",
            hits=[
                {"citation": h.citation, "lines": h.lines, "score": round(h.score, 4), "content": h.content}
                for h in hits
            ],
        )
