"""Guardrails Human-in-the-Loop: classifica o risco de ações e exige aprovação do usuário.

Princípios:
- **Falha segura**: comando desconhecido nunca é "baixo risco"; só uma allowlist de comandos
  de leitura roda sem interrupção.
- **Heurística, não sandbox**: a classificação é por padrões de texto. Ela reduz acidentes,
  mas não substitui isolamento de privilégios (RNF-04). Falsos positivos (ex.: `rm -rf` dentro
  de uma string) são aceitos de propósito; falsos negativos são o que se evita.
- Ação aguardando aprovação **não é executada** até ser aprovada explicitamente.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RiskLevel(IntEnum):
    LOW = 1
    MEDIUM = 2
    CRITICAL = 3

    @property
    def label(self) -> str:
        return {1: "Baixo", 2: "Médio", 3: "Crítico"}[self.value]


class ApprovalState(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    AWAITING_USER_APPROVAL = "AWAITING_APPROVAL"  # nome usado em docs/4 (CT-05): mesmo estado


class ApprovalScope(StrEnum):
    ONCE = "once"
    SESSION = "session"  # mesma ação (assinatura) até o fim da sessão


class GuardrailError(Exception):
    """Erro base dos guardrails."""


class UnknownDecisionError(GuardrailError, LookupError):
    """Nenhuma decisão com esse id."""


class InvalidTransitionError(GuardrailError):
    """A decisão já foi resolvida; só AWAITING_APPROVAL pode ser aprovada/rejeitada."""


# --------------------------------------------------------------------------- requisição
class ActionRequest(BaseModel):
    """Ação que um subagente (ou o Master) quer executar."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["shell", "tool"]
    agent: str = "master"
    command: str | None = None  # kind == "shell"
    tool: str | None = None  # kind == "tool"
    args: dict[str, Any] = Field(default_factory=dict)
    justification: str = ""

    @model_validator(mode="after")
    def _check_kind(self) -> ActionRequest:
        if self.kind == "shell" and not self.command:
            raise ValueError("Ação 'shell' exige 'command' não vazio.")
        if self.kind == "tool" and not self.tool:
            raise ValueError("Ação 'tool' exige 'tool' não vazio.")
        return self

    @classmethod
    def shell(cls, command: str, *, agent: str = "master", justification: str = "") -> ActionRequest:
        return cls(kind="shell", command=command, agent=agent, justification=justification)

    @classmethod
    def tool_call(
        cls, tool: str, *, agent: str = "master", justification: str = "", **args: Any
    ) -> ActionRequest:
        return cls(kind="tool", tool=tool, args=args, agent=agent, justification=justification)

    @property
    def description(self) -> str:
        if self.kind == "shell":
            return str(self.command)
        return f"{self.tool}({', '.join(f'{k}={v!r}' for k, v in sorted(self.args.items()))})"


# ------------------------------------------------------------------------ matriz de risco
@dataclass(frozen=True)
class Rule:
    id: str
    description: str
    pattern: re.Pattern[str]


def _rule(rule_id: str, description: str, pattern: str) -> Rule:
    return Rule(rule_id, description, re.compile(pattern, re.IGNORECASE))


_SEP = r"[^|;&\n]*"  # "qualquer coisa dentro do mesmo comando" (não cruza ; | &)

CRITICAL_RULES: tuple[Rule, ...] = (
    _rule("rm-recursive", "remoção recursiva (rm -r/-rf)", rf"\brm\b{_SEP}\s-(?:[a-z]*r[a-z]*|-recursive)\b"),
    _rule("rm-no-preserve-root", "rm --no-preserve-root", r"--no-preserve-root"),
    _rule(
        "powershell-recurse-delete",
        "Remove-Item/del com -Recurse",
        rf"\b(?:remove-item|ri|del|erase|rm|rmdir|rd){_SEP}\s-recurse\b",
    ),
    _rule("cmd-delete-tree", "del /s, rd /s", rf"\b(?:del|erase|rd|rmdir){_SEP}\s/[sq]\b"),
    _rule(
        "delete-root-or-home",
        "exclusão da raiz, home ou unidade inteira",
        rf"\b(?:rm|del|erase|rmdir|rd|remove-item|ri){_SEP}\s(?:/\*?|~/?\*?|[a-z]:\\?\*?)(?=\s|$)",
    ),
    _rule("format-disk", "formatação de disco", r"\bmkfs(?:\.\w+)?\b|\bformat(?:-volume)?\s+[a-z]:|\bdiskpart\b"),
    _rule("raw-disk-write", "escrita direta em disco", rf"\bdd\b{_SEP}\bof=|>\s*/dev/(?:sd|nvme|hd|disk)"),
    _rule(
        "power-state",
        "desligar/reiniciar a máquina",
        r"\b(?:shutdown|reboot|halt|poweroff|stop-computer|restart-computer)\b",
    ),
    _rule(
        "git-force-push",
        "git push forçado",
        rf"\bgit\s+push\b{_SEP}\s(?:--force(?:-with-lease)?|-[a-z]*f[a-z]*)\b",
    ),
    _rule("git-reset-hard", "git reset --hard", r"\bgit\s+reset\b[^|;&\n]*--hard\b"),
    _rule("git-clean-force", "git clean -f", rf"\bgit\s+clean\b{_SEP}\s-[a-z]*f"),
    _rule("git-discard-changes", "descartar alterações (checkout/restore .)", rf"\bgit\s+(?:checkout|restore){_SEP}\s\.(?=\s|$)"),
    _rule("git-delete-branch", "git branch -D", r"\bgit\s+branch\b[^|;&\n]*\s(?-i:-D)\b"),
    _rule(
        "pipe-to-shell",
        "baixar e executar (curl | sh)",
        r"\b(?:curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b[^;&\n]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh|python\d*|iex|invoke-expression)\b",
    ),
    _rule("iex-download", "Invoke-Expression sobre conteúdo remoto", r"\b(?:iex|invoke-expression)\b[^;&\n]*\b(?:iwr|irm|downloadstring|invoke-webrequest)\b"),
    _rule("sql-drop", "DROP TABLE/DATABASE/SCHEMA", r"\bdrop\s+(?:table|database|schema)\b"),
    _rule("sql-truncate", "TRUNCATE TABLE", r"\btruncate\s+table\b"),
    _rule("sql-delete-all", "DELETE sem WHERE", r"\bdelete\s+from\s+\w+\s*(?:;|$)"),
    _rule("fork-bomb", "fork bomb", r":\s*\(\s*\)\s*\{"),
    _rule("recursive-perms-root", "chmod/chown recursivo na raiz ou home", rf"\b(?:chmod|chown){_SEP}\s-r\b{_SEP}\s(?:/|~|\\)(?=\s|$)"),
    _rule("kill-all", "encerrar todos os processos", r"\bkill\s+-9\s+-1\b|\bkillall5\b"),
    _rule("registry-delete", "reg delete", r"\breg\s+delete\b"),
)

MEDIUM_RULES: tuple[Rule, ...] = (
    _rule("delete-file", "exclusão de arquivo", r"(?<![\w.-])(?:rm|del|erase|rmdir|rd|remove-item|ri|unlink|shred)\b"),
    _rule(
        "modify-fs",
        "mover/copiar/criar/alterar arquivos",
        r"(?<![\w.-])(?:mv|move|ren|rename|cp|copy|xcopy|robocopy|move-item|copy-item|set-content|add-content|out-file|new-item|mkdir|md|touch|tee|truncate|ln)\b|\bsed\s+(?:-\w*i|--in-place)",
    ),
    _rule("find-mutating", "find -delete/-exec", r"\bfind\b[^|;&\n]*\s-(?:delete|exec|execdir|ok)\b"),
    _rule(
        "git-mutating",
        "git que altera repositório/remoto",
        r"\bgit\s+(?:commit|push|merge|rebase|checkout|switch|pull|stash|tag|branch|add|rm|mv|cherry-pick|revert|am|apply|init|clone|fetch|remote|config|submodule|worktree)\b",
    ),
    _rule(
        "package-manager",
        "instalação/remoção de pacotes",
        r"(?<![\w.-])(?:pip3?|npm|pnpm|yarn|brew|choco|winget|cargo|gem|poetry|uv)\s+(?:install|add|remove|uninstall|update|upgrade|sync)\b|(?<![\w.-])(?:apt-get|apt|dnf|yum|pacman|zypper)\b",
    ),
    _rule("network-fetch", "download/requisição de rede", r"(?<![\w.-])(?:curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod|scp|rsync|ssh|ftp)\b"),
    _rule("process-control", "controle de processos/serviços", r"(?<![\w.-])(?:kill|pkill|killall|taskkill|stop-process|systemctl|service|sc|launchctl)\b"),
    _rule("containers", "docker/containers", r"(?<![\w.-])(?:docker|podman|kubectl)\b"),
    _rule("permissions", "permissões/propriedade", r"(?<![\w.-])(?:chmod|chown|chgrp|icacls|takeown|setx|set-acl)\b"),
    _rule("privilege", "elevação de privilégio", r"(?<![\w.-])(?:sudo|runas|su|doas)\b"),
    _rule("shell-wrapper", "interpretador aninhado (bash -c, eval, cmd /c)", r"(?<![\w.-])(?:eval|exec)\b|(?<![\w.-])(?:bash|sh|zsh|dash|ksh|powershell|pwsh|cmd)(?:\.exe)?\s+(?:-\w*c\b|-command\b|/[ck]\b)"),
    _rule("command-substitution", "substituição de comando", r"\$\(|`|<\("),
)

_WRITE_REDIRECT = re.compile(r">")
_SAFE_REDIRECTS = re.compile(r"\d*>&\d+|\d*>>?\s*(?:/dev/null|nul)\b", re.IGNORECASE)
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_]\w*=\S*$")

LOW_PROGRAMS = frozenset(
    {
        "ls", "dir", "pwd", "cd", "echo", "cat", "type", "head", "tail", "less", "more", "grep",
        "egrep", "fgrep", "rg", "find", "fd", "tree", "wc", "sort", "uniq", "diff", "stat", "file",
        "which", "where", "whoami", "date", "hostname", "printenv", "basename", "dirname",
        "get-childitem", "gci", "get-content", "gc", "get-location", "gl", "select-string", "sls",
        "test-path", "get-item", "get-date", "write-output", "write-host", "pytest", "mypy",
        "ruff",
    }
)  # fmt: skip
GIT_READ_SUBCOMMANDS = frozenset(
    {"status", "diff", "log", "show", "blame", "ls-files", "rev-parse", "describe", "shortlog"}
)
_LOW_PYTHON = re.compile(
    r"^(?:python3?|py)(?:\.exe)?\s+(?:--version|-v\b|-m\s+(?:pytest|mypy)\b)", re.IGNORECASE
)
_LOW_PIP = re.compile(r"^pip3?\s+(?:list|show|freeze|--version)\b", re.IGNORECASE)

DEFAULT_TOOL_RISKS: Mapping[str, RiskLevel] = {
    # leitura
    "read_file": RiskLevel.LOW, "read": RiskLevel.LOW, "list_dir": RiskLevel.LOW,
    "ls": RiskLevel.LOW, "glob": RiskLevel.LOW, "grep": RiskLevel.LOW, "search": RiskLevel.LOW,
    "web_search": RiskLevel.LOW, "fetch_page": RiskLevel.LOW, "rag_query": RiskLevel.LOW,
    "get_status": RiskLevel.LOW,
    # escrita / efeitos colaterais
    "write_file": RiskLevel.MEDIUM, "edit_file": RiskLevel.MEDIUM, "create_file": RiskLevel.MEDIUM,
    "move_file": RiskLevel.MEDIUM, "copy_file": RiskLevel.MEDIUM, "delete_file": RiskLevel.MEDIUM,
    "git_commit": RiskLevel.MEDIUM, "install_package": RiskLevel.MEDIUM,
    "send_email": RiskLevel.MEDIUM, "create_event": RiskLevel.MEDIUM,
    # destrutivas
    "delete_dir": RiskLevel.CRITICAL, "delete_directory": RiskLevel.CRITICAL,
    "rm_rf": RiskLevel.CRITICAL, "drop_database": RiskLevel.CRITICAL,
    "format_disk": RiskLevel.CRITICAL,
}  # fmt: skip
SHELL_TOOLS = frozenset(
    {"shell", "shell_runner", "shell_safe_runner", "bash", "powershell", "run_command", "terminal"}
)


@dataclass(frozen=True)
class Classification:
    risk: RiskLevel
    reasons: tuple[str, ...]  # ids das regras que dispararam


def _program(segment: str) -> tuple[str, str]:
    """(programa normalizado, restante) ignorando `VAR=valor` iniciais."""
    tokens = segment.split()
    while tokens and _ENV_ASSIGN.match(tokens[0]):
        tokens.pop(0)
    if not tokens:
        return "", ""
    program = re.split(r"[\\/]", tokens[0])[-1].lower()
    program = program.removesuffix(".exe")
    return program, " ".join(tokens[1:])


def _is_low_segment(segment: str) -> bool:
    program, rest = _program(segment)
    if not program:
        return True  # segmento vazio (ex.: `;` final)
    if program == "git":
        sub = next((t for t in rest.split() if not t.startswith("-")), "")
        return sub.lower() in GIT_READ_SUBCOMMANDS
    if program in {"python", "python3", "py", "pip", "pip3"}:
        full = f"{program} {rest}".strip()
        return bool(_LOW_PYTHON.match(full) or _LOW_PIP.match(full))
    if program == "ruff":
        return rest.lower().startswith("check")
    return program in LOW_PROGRAMS


def classify_shell(command: str) -> Classification:
    """Classifica um comando de shell (bash, PowerShell ou cmd) em Baixo/Médio/Crítico."""
    reasons: list[str] = [r.id for r in CRITICAL_RULES if r.pattern.search(command)]
    if reasons:
        return Classification(RiskLevel.CRITICAL, tuple(reasons))

    medium: list[str] = []
    for segment in _SEGMENT_SPLIT.split(command):
        segment = segment.strip()
        hit = [r.id for r in MEDIUM_RULES if r.pattern.search(segment)]
        if _WRITE_REDIRECT.search(_SAFE_REDIRECTS.sub("", segment)):
            hit.append("write-redirect")
        if not hit and not _is_low_segment(segment):
            hit.append("unknown-command")
        medium.extend(h for h in hit if h not in medium)
    if medium:
        return Classification(RiskLevel.MEDIUM, tuple(medium))
    return Classification(RiskLevel.LOW, ())


# ------------------------------------------------------------------------------ decisão
@dataclass
class GuardrailDecision:
    id: str
    request: ActionRequest
    risk: RiskLevel
    state: ApprovalState
    reasons: tuple[str, ...] = ()
    resolution: str = ""  # auto | user | session | modified | <motivo da rejeição>
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def allowed(self) -> bool:
        return self.state is ApprovalState.APPROVED

    @property
    def awaiting(self) -> bool:
        return self.state is ApprovalState.AWAITING_APPROVAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.request.agent,
            "kind": self.request.kind,
            "action": self.request.description,
            "risk": self.risk.label,
            "state": self.state.value,
            "reasons": list(self.reasons),
            "resolution": self.resolution,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class Approval:
    """Resposta do usuário ao prompt HITL ([y] uma vez, [a] sessão, [n] negar, [m] modificar)."""

    choice: Literal["once", "session", "deny", "modify"]
    command: str | None = None  # novo comando, quando choice == "modify"
    reason: str = ""


Approver = Callable[[GuardrailDecision], Awaitable[Approval]]
Executor = Callable[[ActionRequest], Awaitable[Any]]
AuditSink = Callable[[dict[str, Any]], None]

MAX_MODIFICATIONS = 3


@dataclass
class GuardedResult:
    decision: GuardrailDecision
    executed: bool
    result: Any = None


class GuardrailEngine:
    """Interceptador de ações. Nada com risco >= `approval_threshold` roda sem aprovação."""

    def __init__(
        self,
        *,
        approval_threshold: RiskLevel = RiskLevel.MEDIUM,
        tool_risks: Mapping[str, RiskLevel] | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        self.approval_threshold = approval_threshold
        self.tool_risks = {**DEFAULT_TOOL_RISKS, **(tool_risks or {})}
        self._audit = audit
        self._decisions: dict[str, GuardrailDecision] = {}
        self._session_allowed: set[str] = set()

    # ------------------------------------------------------------- classificação
    def classify(self, request: ActionRequest) -> Classification:
        if request.kind == "shell":
            return classify_shell(str(request.command))

        name = str(request.tool).lower().replace("-", "_")
        if name in SHELL_TOOLS:
            command = request.args.get("command")
            if not isinstance(command, str) or not command.strip():
                return Classification(RiskLevel.MEDIUM, ("shell-tool-without-command",))
            return classify_shell(command)
        if name in self.tool_risks:
            risk = self.tool_risks[name]
            return Classification(risk, (f"tool:{name}",) if risk > RiskLevel.LOW else ())
        return Classification(RiskLevel.MEDIUM, ("unknown-tool",))

    @staticmethod
    def _signature(request: ActionRequest, risk: RiskLevel) -> str:
        """Chave da aprovação "desta sessão". Críticas: ação exata. Médias: família da ação."""
        if request.kind == "shell":
            command = " ".join(str(request.command).split())
            if risk >= RiskLevel.CRITICAL:
                return f"shell:{command}"
            return "shell:" + " ".join(command.split()[:2]).lower()
        if risk >= RiskLevel.CRITICAL:
            return f"tool:{request.description}"
        return f"tool:{str(request.tool).lower()}"

    # ----------------------------------------------------------------- decisões
    def evaluate(self, request: ActionRequest) -> GuardrailDecision:
        classification = self.classify(request)
        decision = GuardrailDecision(
            id=f"apr-{uuid.uuid4().hex[:8]}",
            request=request,
            risk=classification.risk,
            state=ApprovalState.AWAITING_APPROVAL,
            reasons=classification.reasons,
        )
        if classification.risk < self.approval_threshold:
            decision.state, decision.resolution = ApprovalState.APPROVED, "auto"
        elif self._signature(request, classification.risk) in self._session_allowed:
            decision.state, decision.resolution = ApprovalState.APPROVED, "session"
        self._decisions[decision.id] = decision
        self._emit("evaluated", decision)
        return decision

    def get(self, decision_id: str) -> GuardrailDecision:
        try:
            return self._decisions[decision_id]
        except KeyError:
            raise UnknownDecisionError(f"Decisão '{decision_id}' não encontrada.") from None

    def pending(self) -> list[GuardrailDecision]:
        return [d for d in self._decisions.values() if d.awaiting]

    def _resolve(self, decision_id: str) -> GuardrailDecision:
        decision = self.get(decision_id)
        if not decision.awaiting:
            raise InvalidTransitionError(
                f"Decisão '{decision_id}' já está {decision.state.value}; não pode ser alterada."
            )
        return decision

    def approve(
        self, decision_id: str, scope: ApprovalScope = ApprovalScope.ONCE
    ) -> GuardrailDecision:
        decision = self._resolve(decision_id)
        decision.state = ApprovalState.APPROVED
        decision.resolution = "user" if scope is ApprovalScope.ONCE else "user-session"
        if scope is ApprovalScope.SESSION:
            self._session_allowed.add(self._signature(decision.request, decision.risk))
        self._emit("approved", decision)
        return decision

    def reject(self, decision_id: str, reason: str = "negado pelo usuário") -> GuardrailDecision:
        decision = self._resolve(decision_id)
        decision.state = ApprovalState.REJECTED
        decision.resolution = reason
        self._emit("rejected", decision)
        return decision

    def modify(self, decision_id: str, new_command: str) -> GuardrailDecision:
        """[m] Modificar: rejeita a original e reavalia o comando novo do zero."""
        decision = self._resolve(decision_id)
        if decision.request.kind != "shell":
            raise ValueError("Só ações 'shell' podem ser modificadas.")
        if not new_command.strip():
            raise ValueError("O comando modificado não pode ser vazio.")
        decision.state = ApprovalState.REJECTED
        decision.resolution = "modified"
        self._emit("rejected", decision)
        return self.evaluate(decision.request.model_copy(update={"command": new_command}))

    def revoke_session_approvals(self) -> None:
        self._session_allowed.clear()

    # ------------------------------------------------------------- interrupção
    async def guarded_execute(
        self,
        request: ActionRequest,
        executor: Executor,
        approver: Approver | None = None,
    ) -> GuardedResult:
        """Avalia e só chama `executor` se a ação estiver APPROVED.

        Sem `approver`, ações que exigem aprovação ficam em AWAITING_APPROVAL (não executam);
        aprove depois via `approve(id)`/`/approve <id>`.
        """
        decision = self.evaluate(request)
        modifications = 0
        while decision.awaiting and approver is not None:
            answer = await approver(decision)
            if answer.choice == "once":
                self.approve(decision.id)
            elif answer.choice == "session":
                self.approve(decision.id, ApprovalScope.SESSION)
            elif answer.choice == "deny":
                self.reject(decision.id, answer.reason or "negado pelo usuário")
            elif (
                answer.choice == "modify"
                and answer.command
                and answer.command.strip()
                and modifications < MAX_MODIFICATIONS
            ):
                modifications += 1
                decision = self.modify(decision.id, answer.command)
            else:  # modificação sem comando ou além do limite
                self.reject(decision.id, "modificação inválida ou limite excedido")
        if decision.allowed:
            return GuardedResult(decision, True, await executor(decision.request))
        return GuardedResult(decision, False)

    def _emit(self, event: str, decision: GuardrailDecision) -> None:
        if self._audit is not None:
            self._audit({"event": event, **decision.to_dict()})
