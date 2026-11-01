"""Tabelas de dados: telemetria de subagentes, skills, agentes, memória e comportamento."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from rich.table import Table
from rich.text import Text

from src.memory.config import BehaviorConfig
from src.skills.base import SkillSpec
from src.subagents_bridge.schemas import SubagentInfo
from src.subagents_bridge.telemetry import AgentStats
from src.ui.components import theme


def _table(title: str, *columns: str) -> Table:
    table = Table(title=title, title_justify="left", header_style=f"bold {theme.PRIMARY}", border_style=theme.DIM)
    for column in columns:
        table.add_column(column)
    return table


def telemetry_table(
    stats: Mapping[str, AgentStats], health: Mapping[str, bool] | None = None
) -> Table:
    """Uma linha por subagente: saúde, chamadas, falhas, latência média, tokens e ativas."""
    table = _table("Subagentes", "Agente", "Saúde", "Chamadas", "Falhas", "Latência média", "Tokens", "Ativas")
    for name in sorted(stats):
        s = stats[name]
        if health is None or name not in health:
            status = Text("—", style=theme.DIM)
        else:
            status = Text("OK", style=theme.SUCCESS) if health[name] else Text("OFF", style=theme.ERROR)
        table.add_row(
            Text(name), status, str(s.calls), str(s.failures),
            f"{s.avg_latency * 1000:.0f} ms", f"{s.total_tokens:,}", str(len(s.active)),
        )  # fmt: skip
    return table


def skills_table(specs: Sequence[SkillSpec]) -> Table:
    table = _table("Skills", "Nome", "Categoria", "Origem", "Descrição")
    for spec in specs:
        table.add_row(Text(spec.name, style=f"bold {theme.PRIMARY}"), Text(spec.category), Text(spec.source), Text(spec.description))
    return table


def agents_table(infos: Iterable[SubagentInfo]) -> Table:
    table = _table("Agentes registrados", "Nome", "Capacidades", "Modelos recomendados", "Descrição")
    for info in infos:
        table.add_row(
            Text(info.name, style=f"bold {theme.PRIMARY}"),
            Text(", ".join(info.capabilities)),
            Text(", ".join(info.recommended_models) or "—"),
            Text(info.description or "—"),
        )
    return table


def memory_table(
    *,
    session: str | None,
    context_size: int,
    preferences: Mapping[str, Any],
    notes: int,
    episodes: Sequence[Mapping[str, Any]],
) -> Table:
    table = _table("Memória", "Item", "Valor")
    table.add_row("Sessão ativa", Text(session or "—"))
    table.add_row("Mensagens no contexto", str(context_size))
    table.add_row("Notas salvas", str(notes))
    for key, value in sorted(preferences.items()):
        table.add_row(Text(f"preferência: {key}"), Text(str(value)))
    for episode in episodes:
        table.add_row(Text(f"episódio #{episode['id']} ({episode['status']})"), Text(str(episode["goal"])))
    return table


def behavior_table(behavior: BehaviorConfig) -> Table:
    table = _table("Comportamento", "Campo", "Valor")
    table.add_row("tom", Text(behavior.tone))
    table.add_row("detalhe", Text(behavior.verbosity))
    table.add_row("idioma", Text(behavior.language))
    if behavior.rules:
        for number, rule in enumerate(behavior.rules, start=1):
            table.add_row(f"regra {number}", Text(rule))
    else:
        table.add_row("regras", Text("nenhuma", style=theme.DIM))
    return table
