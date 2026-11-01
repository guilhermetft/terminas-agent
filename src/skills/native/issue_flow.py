"""issue-flow: ciclo de vida de issues, commits convencionais e PRs via `gh`/`git`."""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.guardrails import ActionRequest
from src.skills.base import Skill, SkillError, SkillResult

CONVENTIONAL = re.compile(
    r"^(?:feat|fix|refactor|docs|test|chore|style|perf|build|ci)(?:\([\w./-]+\))?!?: \S.*$"
)
BLIND_ADD = {".", "*", "-A", "--all", "-a", ":/"}


class IssueFlowParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["view", "list", "create_issue", "commit", "create_pr"]
    number: int | None = Field(default=None, ge=1)
    state: Literal["open", "closed", "all"] = "open"
    limit: int = Field(default=20, ge=1, le=100)
    title: str | None = None
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    message: str | None = None
    paths: list[str] = Field(default_factory=list)
    base: str | None = None

    @model_validator(mode="after")
    def _check_op(self) -> Self:
        if self.op == "view" and self.number is None:
            raise ValueError("'view' exige 'number'.")
        if self.op in {"create_issue", "create_pr"} and not (self.title and self.title.strip()):
            raise ValueError(f"'{self.op}' exige 'title'.")
        if self.op == "create_pr" and not CONVENTIONAL.match(self.title or ""):
            raise ValueError("O título do PR deve seguir Conventional Commits (ex.: 'feat: ...').")
        if self.op == "commit":
            if not self.message or not CONVENTIONAL.match(self.message.splitlines()[0]):
                raise ValueError("'message' deve seguir Conventional Commits (ex.: 'fix: ...').")
            if not self.paths:
                raise ValueError("'commit' exige 'paths' explícitos (sem add cego).")
            if any(p.strip() in BLIND_ADD or p.startswith("-") for p in self.paths):
                raise ValueError("Informe arquivos específicos; '.', '*' e '-A' não são aceitos.")
        return self


class IssueFlow(Skill):
    name: ClassVar[str] = "issue-flow"
    description: ClassVar[str] = (
        "Ciclo de vida no GitHub: ver/listar/criar issues, commits convencionais e abrir PRs "
        "(nunca faz merge)."
    )
    category: ClassVar[str] = "Engenharia & Git"
    Params: ClassVar[type[BaseModel]] = IssueFlowParams

    def action(self, params: IssueFlowParams) -> ActionRequest:
        summary: dict[str, Any] = {
            k: v
            for k, v in {
                "number": params.number,
                "title": params.title,
                "message": params.message,
                "paths": params.paths or None,
            }.items()
            if v is not None
        }
        return ActionRequest.tool_call(f"{self.name}.{params.op}", agent="master", **summary)

    async def _run(self, argv: list[str]) -> str:
        result = await self.ctx.runner.run(argv, cwd=self.ctx.workdir, timeout=60)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise SkillError(f"`{' '.join(argv[:3])}` falhou (código {result.returncode}): {detail}")
        return result.stdout.strip()

    async def run(self, params: IssueFlowParams) -> SkillResult:
        label_args = [arg for label in params.labels for arg in ("--label", label)]
        if params.op == "view":
            fields = "number,title,body,labels,state,url"
            out = await self._run(["gh", "issue", "view", str(params.number), "--json", fields])
            return self._json_result(out)
        if params.op == "list":
            argv = ["gh", "issue", "list", "--state", params.state, "--limit", str(params.limit)]
            out = await self._run(
                [*argv, *label_args, "--json", "number,title,labels,state,url"]
            )
            return self._json_result(out)
        if params.op == "create_issue":
            out = await self._run(
                ["gh", "issue", "create", "--title", str(params.title), "--body", params.body, *label_args]
            )
            return SkillResult.success(out, url=out.splitlines()[-1] if out else "")
        if params.op == "commit":
            await self._run(["git", "add", "--", *params.paths])
            out = await self._run(["git", "commit", "-m", str(params.message)])
            return SkillResult.success(out)
        argv = ["gh", "pr", "create", "--title", str(params.title), "--body", params.body]
        if params.base:
            argv += ["--base", params.base]
        out = await self._run([*argv, *label_args])
        return SkillResult.success(out, url=out.splitlines()[-1] if out else "")

    @staticmethod
    def _json_result(text: str) -> SkillResult:
        try:
            return SkillResult.success(text, json=json.loads(text))
        except json.JSONDecodeError:
            return SkillResult.success(text)
