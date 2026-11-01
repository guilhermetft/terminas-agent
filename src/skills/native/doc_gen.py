"""doc-gen: relatórios em MD/HTML/PDF/DOCX e diagramas Mermaid/SVG."""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.core.guardrails import ActionRequest
from src.skills.base import Skill, SkillError, SkillResult
from src.skills.native import render

EXTENSIONS = {"md": ".md", "html": ".html", "pdf": ".pdf", "docx": ".docx", "mermaid": ".mmd", "svg": ".svg"}


class DocGenParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["md", "html", "pdf", "docx", "mermaid", "svg"]
    content: str = Field(min_length=1, description="Markdown (md/html/pdf/docx), Mermaid ou SVG.")
    output: str = Field(min_length=1, description="Caminho relativo ao diretório de trabalho.")
    title: str = ""
    overwrite: bool = False


class DocGen(Skill):
    name: ClassVar[str] = "doc-gen"
    description: ClassVar[str] = (
        "Gera relatórios (Markdown, HTML, PDF, DOCX) e diagramas (Mermaid, SVG) em arquivos."
    )
    category: ClassVar[str] = "Documentação & Mídia"
    Params: ClassVar[type[BaseModel]] = DocGenParams

    def _target(self, params: DocGenParams) -> Path:
        target = self.ctx.resolve_inside_workdir(params.output)
        wanted = EXTENSIONS[params.format]
        return target if target.suffix.lower() == wanted else target.with_name(target.name + wanted)

    def action(self, params: DocGenParams) -> ActionRequest:
        return ActionRequest.tool_call(
            f"{self.name}.generate", agent="master", path=params.output, format=params.format
        )

    async def run(self, params: DocGenParams) -> SkillResult:
        target = self._target(params)
        if target.exists() and not params.overwrite:
            raise SkillError(f"{target.name} já existe; use overwrite=true para substituir.")

        warnings: list[str] = []
        fmt = params.format
        if fmt in {"md", "mermaid"}:
            data = params.content.encode("utf-8")
        elif fmt == "svg":
            try:
                root = ET.fromstring(params.content)
            except ET.ParseError as exc:
                raise SkillError(f"SVG inválido: {exc}") from None
            if not root.tag.lower().endswith("svg"):
                raise SkillError("O conteúdo não é um SVG (elemento raiz diferente de <svg>).")
            data = params.content.encode("utf-8")
        else:
            blocks = render.parse_markdown(params.content)
            data = {"html": render.to_html, "pdf": render.to_pdf, "docx": render.to_docx}[fmt](
                blocks, params.title
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files = [target.as_posix()]

        if fmt == "mermaid":
            rendered = await self._render_mermaid(target)
            if rendered is None:
                warnings.append("mmdc (mermaid-cli) não encontrado: gravado só o código-fonte .mmd.")
            else:
                files.append(rendered.as_posix())
        return SkillResult.success(
            f"Gerado: {', '.join(files)}", files=files, bytes=len(data), warnings=warnings
        )

    async def _render_mermaid(self, source: Path) -> Path | None:
        if shutil.which("mmdc") is None:
            return None
        svg = source.with_suffix(".svg")
        result = await self.ctx.runner.run(
            ["mmdc", "-i", str(source), "-o", str(svg)], cwd=self.ctx.workdir, timeout=120
        )
        return svg if result.returncode == 0 and svg.exists() else None
