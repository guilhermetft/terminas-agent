"""Dublês e utilitários compartilhados pelos testes da UI e do engine."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

from rich.console import Console

from src.core.planner import ChatMessage
from src.memory.config import AppConfig, PathsConfig, SkillsConfig
from src.rag import HashingEmbedder
from src.skills import CommandResult
from src.subagents_bridge import AgentResult, BaseSubagent, SubagentInfo
from src.subagents_bridge.schemas import TokenUsage
from src.ui.cli import TerminasApp
from src.ui.services import Services, start_services


class ScriptedReader:
    """Entrega linhas pré-definidas; None (EOF) quando acabam. Registra cada prompt."""

    def __init__(self, *lines: str | BaseException | None) -> None:
        self.lines = list(lines)
        self.prompts: list[tuple[str, bool]] = []

    async def read_line(self, prompt: str, *, history: bool = True) -> str | None:
        self.prompts.append((prompt, history))
        if not self.lines:
            return None
        item = self.lines.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeLLM:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls: list[list[ChatMessage]] = []

    async def __call__(self, messages: list[ChatMessage]) -> str:
        self.calls.append(messages)
        return self.responses.pop(0)


class ForbiddenLLM:
    """Falha o teste se algum comando 'determinístico' chamar o LLM."""

    calls: list = []

    async def __call__(self, messages: list[ChatMessage]) -> str:
        raise AssertionError("o LLM não deveria ser chamado por este comando")


def plan_json(*nodes: tuple) -> str:
    """nodes: (id, agent, depends_on...)"""
    return json.dumps(
        {
            "nodes": [
                {"id": nid, "description": f"passo {nid}", "depends_on": list(deps), "agent": agent}
                for nid, agent, *deps in nodes
            ]
        }
    )


class Worker(BaseSubagent):
    """Subagente de teste configurável: sucesso, falha, exceção ou lentidão."""

    def __init__(self, name: str = "dev-agent", *, mode: str = "ok", delay: float = 0.0) -> None:
        super().__init__(SubagentInfo(name=name, capabilities=["work"], description="Agente de teste"))
        self.mode, self.delay = mode, delay
        self.tasks: list = []
        self.cancelled: list[str] = []

    async def execute(self, task):
        self.tasks.append(task)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.mode == "raise":
            raise RuntimeError("quebrou")
        if self.mode == "fail":
            return AgentResult(task_id=task.task_id, status="failed", error="não deu", summary="parcial")
        return AgentResult(
            task_id=task.task_id,
            status="success",
            summary=f"feito: {task.goal}",
            token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, model="m"),
        )

    async def cancel(self, task_id: str) -> None:
        self.cancelled.append(task_id)


class FakeRunner:
    def __init__(self, *results: CommandResult) -> None:
        self.calls: list = []
        self.results = list(results)

    async def run(self, command, *, cwd=None, timeout=60.0):
        self.calls.append({"command": command, "cwd": cwd, "timeout": timeout})
        return self.results.pop(0) if self.results else CommandResult(0, "ok\n")


def make_console(width: int = 120) -> Console:
    return Console(file=io.StringIO(), width=width, force_terminal=False, color_system=None)


def output(console: Console) -> str:
    return console.file.getvalue()


def make_config(root: Path, *, plugins_dir: str | None = None, **extra) -> AppConfig:
    return AppConfig(
        paths=PathsConfig(data_dir=str(root / ".terminas")),
        skills=SkillsConfig(plugins_dir=plugins_dir),
        **extra,
    )


async def build_app(
    root: Path,
    *reader_lines: str | None,
    llm=None,
    workers: tuple[Worker, ...] = (),
    runner=None,
    config: AppConfig | None = None,
) -> tuple[TerminasApp, Services, ScriptedReader]:
    services = await start_services(
        config or make_config(root),
        llm=llm or ForbiddenLLM(),
        embedder=HashingEmbedder(),
        runner=runner,
        workdir=root,
    )
    for worker in workers:
        services.subagents.register(worker)
    reader = ScriptedReader(*reader_lines)
    app = TerminasApp(services, make_console(), reader)
    services.memory.start_session("teste")
    return app, services, reader
