"""Contrato das skills: parâmetros tipados, resultado, contexto compartilhado e guardrail HITL."""

from __future__ import annotations

import asyncio
import contextlib
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol

from pydantic import BaseModel, ValidationError

from src.core.guardrails import ActionRequest, Approver, GuardrailEngine, RiskLevel

if TYPE_CHECKING:
    from src.memory.engine import MemoryEngine
    from src.rag.engine import RAGEngine
    from src.skills.mcp_client import McpManager
    from src.skills.registry import SkillsRegistry
    from src.subagents_bridge.registry import SubagentRegistry
    from src.subagents_bridge.telemetry import SwarmTelemetry

MAX_CAPTURED_CHARS = 20_000

# Risco de cada operação de skill (chave = "<skill>.<op>", normalizada como no GuardrailEngine).
# Operação ausente desta tabela cai em MEDIUM (falha segura).
SKILL_TOOL_RISKS: Mapping[str, RiskLevel] = {
    "issue_flow.view": RiskLevel.LOW,
    "issue_flow.list": RiskLevel.LOW,
    "issue_flow.create_issue": RiskLevel.MEDIUM,
    "issue_flow.commit": RiskLevel.MEDIUM,
    "issue_flow.create_pr": RiskLevel.MEDIUM,
    "doc_gen.generate": RiskLevel.MEDIUM,
    "google_workspace.read": RiskLevel.LOW,
    "google_workspace.create_event": RiskLevel.MEDIUM,
    "deep_search.search": RiskLevel.LOW,
    "deep_search.fetch": RiskLevel.LOW,
    "deep_search.research": RiskLevel.LOW,
    "swarm_inspector.status": RiskLevel.LOW,
    "swarm_inspector.health": RiskLevel.LOW,
    "swarm_inspector.kill": RiskLevel.MEDIUM,
    "swarm_inspector.restart": RiskLevel.MEDIUM,
    "quick_notes.sync_vault": RiskLevel.MEDIUM,
}


class SkillError(Exception):
    """Falha esperada de uma skill (entrada inválida, dependência ausente, comando falhou)."""


class UnknownSkillError(SkillError, LookupError):
    """Nenhuma skill registrada com esse nome."""


class SkillResult(BaseModel):
    ok: bool
    status: Literal["ok", "error", "awaiting_approval", "rejected"] = "ok"
    output: str = ""
    data: dict[str, Any] = {}
    error: str | None = None
    decision_id: str | None = None

    @classmethod
    def success(cls, output: str = "", **data: Any) -> SkillResult:
        return cls(ok=True, output=output, data=data)

    @classmethod
    def failure(cls, error: str, **data: Any) -> SkillResult:
        return cls(ok=False, status="error", error=error, data=data)


class SkillSpec(BaseModel):
    """Descrição de uma skill para o LLM/CLI: nome, função e schema JSON dos parâmetros."""

    name: str
    description: str
    category: str
    source: Literal["native", "plugin", "mcp"] = "native"
    input_schema: dict[str, Any]


# ------------------------------------------------------------------------------ comandos
@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class CommandRunner(Protocol):
    async def run(
        self, command: str | Sequence[str], *, cwd: Path | None = None, timeout: float = 60.0
    ) -> CommandResult: ...


def _clip(text: str) -> str:
    if len(text) <= MAX_CAPTURED_CHARS:
        return text
    return text[:MAX_CAPTURED_CHARS] + f"\n[... saída truncada em {MAX_CAPTURED_CHARS} caracteres]"


class SubprocessRunner:
    """Executa processos reais. `str` roda no shell do SO; lista roda direto (sem shell)."""

    async def run(
        self, command: str | Sequence[str], *, cwd: Path | None = None, timeout: float = 60.0
    ) -> CommandResult:
        options: dict[str, Any] = {
            "cwd": cwd,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        try:
            if isinstance(command, str):
                proc = await asyncio.create_subprocess_shell(command, **options)
            else:
                proc = await asyncio.create_subprocess_exec(*command, **options)
        except FileNotFoundError as exc:
            return CommandResult(127, stderr=f"Programa não encontrado: {exc.filename or exc}")
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            return CommandResult(-1, stderr=f"Tempo limite de {timeout:g}s excedido.", timed_out=True)
        return CommandResult(
            proc.returncode if proc.returncode is not None else -1,
            _clip(out.decode("utf-8", errors="replace")),
            _clip(err.decode("utf-8", errors="replace")),
        )


# -------------------------------------------------------------------------------- contexto
def _default_guardrails() -> GuardrailEngine:
    return GuardrailEngine(tool_risks=SKILL_TOOL_RISKS)


@dataclass
class SkillContext:
    """Dependências compartilhadas pelas skills (todas opcionais; faltou, a skill avisa)."""

    guardrails: GuardrailEngine = field(default_factory=_default_guardrails)
    approver: Approver | None = None
    runner: CommandRunner = field(default_factory=SubprocessRunner)
    workdir: Path = field(default_factory=Path.cwd)
    clock: Callable[[], datetime] = datetime.now
    memory: MemoryEngine | None = None
    rag: RAGEngine | None = None
    subagents: SubagentRegistry | None = None
    telemetry: SwarmTelemetry | None = None
    mcp: McpManager | None = None
    skills: SkillsRegistry | None = None
    vault_dir: Path | None = None
    options: dict[str, Any] = field(default_factory=dict)

    def require(self, attribute: str, label: str) -> Any:
        value = getattr(self, attribute)
        if value is None:
            raise SkillError(f"{label} não configurado(a) no contexto das skills.")
        return value

    def resolve_inside_workdir(self, relative: str | Path) -> Path:
        """Resolve um caminho garantindo que fica dentro de `workdir` (RNF-04)."""
        base = Path(self.workdir).resolve()
        target = (base / relative).resolve()
        if target != base and base not in target.parents:
            raise SkillError(f"Caminho fora do diretório de trabalho: {relative}")
        return target


# ----------------------------------------------------------------------------------- skill
class Skill(ABC):
    """Uma capacidade nomeada do Termina. Subclasses definem `Params` e implementam `run`."""

    name: ClassVar[str]
    description: ClassVar[str]
    category: ClassVar[str]
    Params: ClassVar[type[BaseModel]]
    source: ClassVar[Literal["native", "plugin", "mcp"]] = "native"

    def __init__(self, ctx: SkillContext) -> None:
        self.ctx = ctx

    # --- a sobrescrever -----------------------------------------------------------------
    @abstractmethod
    async def run(self, params: Any) -> SkillResult:
        """Executa a skill com parâmetros já validados."""

    def action(self, params: Any) -> ActionRequest | None:
        """Ação a ser submetida ao guardrail antes de `run`. None = não precisa de aprovação."""
        return None

    async def run_approved(self, params: Any, request: ActionRequest) -> SkillResult:
        """Chamado após aprovação. `request` pode ter sido modificado pelo usuário ([m])."""
        return await self.run(params)

    def parse_params(self, raw: Any) -> Any:
        try:
            return self.Params.model_validate(raw)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'parâmetros'}: {e['msg']}"
                for e in exc.errors()
            )
            raise SkillError(f"Parâmetros inválidos para '{self.name}': {problems}") from None

    def input_schema(self) -> dict[str, Any]:
        return self.Params.model_json_schema()

    # --- fluxo público ------------------------------------------------------------------
    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=self.name,
            description=self.description,
            category=self.category,
            source=self.source,
            input_schema=self.input_schema(),
        )

    async def execute(self, raw: Any) -> SkillResult:
        """Valida, passa pelo guardrail HITL e executa. Nunca levanta `SkillError`."""
        try:
            params = self.parse_params(raw)
            action = self.action(params)
            if action is None:
                return await self.run(params)

            guarded = await self.ctx.guardrails.guarded_execute(
                action, lambda request: self.run_approved(params, request), self.ctx.approver
            )
            if guarded.executed:
                return guarded.result
            decision = guarded.decision
            if decision.awaiting:
                return SkillResult(
                    ok=False,
                    status="awaiting_approval",
                    decision_id=decision.id,
                    error=(
                        f"Ação de risco {decision.risk.label} aguardando aprovação "
                        f"(id {decision.id}): {action.description}"
                    ),
                )
            return SkillResult(
                ok=False,
                status="rejected",
                decision_id=decision.id,
                error=f"Ação não autorizada ({decision.resolution}).",
            )
        except SkillError as exc:
            return SkillResult.failure(str(exc))
