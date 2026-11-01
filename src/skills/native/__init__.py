"""As 9 skills nativas de fábrica do Termina's Agent."""

from src.skills.native.daily_briefing import DailyBriefing
from src.skills.native.deep_search import DeepSearch
from src.skills.native.doc_gen import DocGen
from src.skills.native.google_workspace import GoogleWorkspace
from src.skills.native.issue_flow import IssueFlow
from src.skills.native.quick_notes import QuickNotes
from src.skills.native.rag_indexer import RagIndexer
from src.skills.native.shell_safe_runner import ShellSafeRunner
from src.skills.native.swarm_inspector import SwarmInspector

NATIVE_SKILLS = (
    IssueFlow,
    DocGen,
    GoogleWorkspace,
    DeepSearch,
    SwarmInspector,
    DailyBriefing,
    RagIndexer,
    ShellSafeRunner,
    QuickNotes,
)

__all__ = [
    "NATIVE_SKILLS",
    "DailyBriefing",
    "DeepSearch",
    "DocGen",
    "GoogleWorkspace",
    "IssueFlow",
    "QuickNotes",
    "RagIndexer",
    "ShellSafeRunner",
    "SwarmInspector",
]
