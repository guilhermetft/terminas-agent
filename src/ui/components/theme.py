"""Paleta e símbolos do terminal (docs/6, seção 6.5)."""

from __future__ import annotations

from src.core.engine import NodeState
from src.core.guardrails import RiskLevel

PRIMARY = "#38BDF8"
SUCCESS = "#34D399"
WARNING = "#FBBF24"
ERROR = "#F87171"
DIM = "#94A3B8"

STATE_SYMBOL = {
    NodeState.PENDING: "○",
    NodeState.RUNNING: "●",
    NodeState.DONE: "✔",
    NodeState.FAILED: "✖",
    NodeState.SKIPPED: "⊘",
}
STATE_STYLE = {
    NodeState.PENDING: DIM,
    NodeState.RUNNING: PRIMARY,
    NodeState.DONE: SUCCESS,
    NodeState.FAILED: ERROR,
    NodeState.SKIPPED: WARNING,
}
STATE_LABEL = {
    NodeState.PENDING: "PENDENTE",
    NodeState.RUNNING: "RODANDO",
    NodeState.DONE: "CONCLUÍDO",
    NodeState.FAILED: "FALHOU",
    NodeState.SKIPPED: "IGNORADO",
}
RISK_STYLE = {RiskLevel.LOW: SUCCESS, RiskLevel.MEDIUM: WARNING, RiskLevel.CRITICAL: ERROR}
