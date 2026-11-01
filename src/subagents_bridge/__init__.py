"""Contratos e registro de subagentes externos (protocolo A2A)."""

from src.subagents_bridge.base import BaseSubagent
from src.subagents_bridge.registry import (
    AgentNotFoundError,
    DuplicateAgentError,
    SubagentRegistry,
    SubagentRegistryError,
)
from src.subagents_bridge.telemetry import AgentStats, SwarmTelemetry, run_tracked
from src.subagents_bridge.schemas import (
    AgentResult,
    AgentTask,
    AgentTaskResult,
    Artifact,
    RagChunk,
    SubagentInfo,
    TaskConstraints,
    TaskContext,
    TaskStatus,
    TokenUsage,
)

__all__ = [
    "AgentNotFoundError",
    "AgentStats",
    "AgentResult",
    "AgentTask",
    "AgentTaskResult",
    "Artifact",
    "BaseSubagent",
    "DuplicateAgentError",
    "RagChunk",
    "SubagentInfo",
    "SubagentRegistry",
    "SubagentRegistryError",
    "SwarmTelemetry",
    "TaskConstraints",
    "TaskContext",
    "TaskStatus",
    "TokenUsage",
    "run_tracked",
]
