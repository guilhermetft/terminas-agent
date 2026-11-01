"""quick-notes: captura rápida de notas na memória de longo prazo, com sync opcional para vault."""

from __future__ import annotations

import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.guardrails import ActionRequest
from src.skills.base import Skill, SkillError, SkillResult

NOTES_NAMESPACE = "notes"


def slugify(text: str, max_len: int = 48) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    return (slug[:max_len].strip("-")) or "nota"


class QuickNotesParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["add", "list", "search"] = "add"
    text: str | None = None
    tags: list[str] = Field(default_factory=list)
    tag: str | None = Field(default=None, description="Filtra por tag em list/search.")
    query: str | None = None
    limit: int = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.op == "add" and not (self.text and self.text.strip()):
            raise ValueError("'add' exige 'text'.")
        if self.op == "search" and not (self.query and self.query.strip()):
            raise ValueError("'search' exige 'query'.")
        return self


class QuickNotes(Skill):
    name: ClassVar[str] = "quick-notes"
    description: ClassVar[str] = (
        "Captura ideias e decisões na memória de longo prazo (add), lista e busca; "
        "sincroniza com um vault Markdown/Obsidian quando configurado."
    )
    category: ClassVar[str] = "Memória & Notas"
    Params: ClassVar[type[BaseModel]] = QuickNotesParams

    def action(self, params: QuickNotesParams) -> ActionRequest | None:
        if params.op == "add" and self.ctx.vault_dir is not None:
            return ActionRequest.tool_call(f"{self.name}.sync_vault", agent="master")
        return None  # escrever na própria memória do Termina não precisa de aprovação

    def _notes(self) -> list[dict[str, Any]]:
        memory = self.ctx.require("memory", "Memória")
        notes = list(memory.list_long_term(NOTES_NAMESPACE).values())
        return sorted(notes, key=lambda n: n["created_at"], reverse=True)

    def _write_vault(self, note: dict[str, Any]) -> Path:
        vault = Path(str(self.ctx.vault_dir)).expanduser()
        vault.mkdir(parents=True, exist_ok=True)
        base = f"{note['created_at'][:10]}-{slugify(note['text'])}"
        path = vault / f"{base}.md"
        counter = 2
        while path.exists():  # nunca sobrescreve nota existente
            path = vault / f"{base}-{counter}.md"
            counter += 1
        tags = ", ".join(note["tags"])
        path.write_text(
            f"---\ncreated: {note['created_at']}\ntags: [{tags}]\nid: {note['id']}\n---\n\n{note['text']}\n",
            encoding="utf-8",
        )
        return path

    async def run(self, params: QuickNotesParams) -> SkillResult:
        if params.op == "add":
            memory = self.ctx.require("memory", "Memória")
            created = self.ctx.clock().isoformat(timespec="seconds")
            note = {
                "id": f"n-{created.replace('-', '').replace(':', '').replace('T', '')}-{uuid.uuid4().hex[:4]}",
                "text": str(params.text).strip(),
                "tags": sorted({t.strip().lower() for t in params.tags if t.strip()}),
                "created_at": created,
            }
            memory.set_long_term(note["id"], note, namespace=NOTES_NAMESPACE)
            data: dict[str, Any] = {"note": note}
            if self.ctx.vault_dir is not None:
                try:
                    data["vault_file"] = self._write_vault(note).as_posix()
                except OSError as exc:
                    raise SkillError(f"Nota salva na memória, mas o vault falhou: {exc}") from exc
            return SkillResult.success(f"Nota {note['id']} salva.", **data)

        notes = self._notes()
        if params.tag:
            notes = [n for n in notes if params.tag.strip().lower() in n["tags"]]
        if params.op == "search":
            needle = str(params.query).lower()
            notes = [n for n in notes if needle in n["text"].lower() or needle in " ".join(n["tags"])]
        notes = notes[: params.limit]
        lines = [f"- [{n['created_at'][:16]}] {n['text']}" + (f" #{' #'.join(n['tags'])}" if n["tags"] else "") for n in notes]
        return SkillResult.success("\n".join(lines) or "Nenhuma nota encontrada.", notes=notes)
