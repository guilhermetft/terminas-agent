"""Catálogo de skills do Termina's Agent: 9 nativas, plugins e ferramentas MCP."""

from src.skills.base import (
    SKILL_TOOL_RISKS,
    CommandResult,
    Skill,
    SkillContext,
    SkillError,
    SkillResult,
    SkillSpec,
    SubprocessRunner,
    UnknownSkillError,
)
from src.skills.mcp_client import (
    McpError,
    McpManager,
    McpServerConfig,
    McpTool,
    McpToolResult,
    load_mcp_configs,
)
from src.skills.registry import (
    DuplicateSkillError,
    McpToolSkill,
    PluginReport,
    SkillsRegistry,
    build_default_registry,
)

__all__ = [
    "SKILL_TOOL_RISKS",
    "CommandResult",
    "DuplicateSkillError",
    "McpError",
    "McpManager",
    "McpServerConfig",
    "McpTool",
    "McpToolResult",
    "McpToolSkill",
    "PluginReport",
    "Skill",
    "SkillContext",
    "SkillError",
    "SkillResult",
    "SkillSpec",
    "SkillsRegistry",
    "SubprocessRunner",
    "UnknownSkillError",
    "build_default_registry",
    "load_mcp_configs",
]
