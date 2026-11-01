"""shell-safe-runner: execução de comandos com interceptação obrigatória pelo guardrail HITL."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from src.core.guardrails import ActionRequest
from src.skills.base import Skill, SkillResult

DEFAULT_TIMEOUT = 60.0
MAX_TIMEOUT = 600.0


class ShellRunnerParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1)
    cwd: str | None = Field(default=None, description="Relativo ao diretório de trabalho.")
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT, gt=0, le=MAX_TIMEOUT)
    justification: str = ""


class ShellSafeRunner(Skill):
    name: ClassVar[str] = "shell-safe-runner"
    description: ClassVar[str] = (
        "Executa comandos de terminal (build, testes, git). Comandos de risco Médio/Crítico "
        "exigem aprovação humana antes de rodar."
    )
    category: ClassVar[str] = "Operação de Sistema"
    Params: ClassVar[type[BaseModel]] = ShellRunnerParams

    def action(self, params: ShellRunnerParams) -> ActionRequest:
        return ActionRequest.shell(params.command, justification=params.justification)

    async def run(self, params: ShellRunnerParams) -> SkillResult:
        return await self.run_approved(params, self.action(params))

    async def run_approved(self, params: ShellRunnerParams, request: ActionRequest) -> SkillResult:
        # Roda `request.command`: pode ser a versão modificada pelo usuário ([m]).
        cwd = self.ctx.resolve_inside_workdir(params.cwd) if params.cwd else self.ctx.workdir
        result = await self.ctx.runner.run(
            str(request.command), cwd=cwd, timeout=params.timeout_seconds
        )
        output = result.stdout
        if result.stderr:
            output += ("\n" if output else "") + "[stderr]\n" + result.stderr
        data = {
            "command": request.command,
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        if result.returncode != 0:
            reason = "tempo limite excedido" if result.timed_out else f"código de saída {result.returncode}"
            return SkillResult(ok=False, status="error", output=output, error=reason, data=data)
        return SkillResult(ok=True, output=output, data=data)
