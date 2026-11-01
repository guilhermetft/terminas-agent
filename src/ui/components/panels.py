"""Painéis: cabeçalho de status, caixa de confirmação HITL e ajuda."""

from __future__ import annotations

from collections.abc import Iterable

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.core.guardrails import GuardrailDecision
from src.ui.components import theme


def header(
    version: str,
    *,
    session: str | None,
    busy: bool,
    workers: int,
    tokens: int,
    auto_remaining: float,
    model: str | None = None,
) -> Panel:
    line = Text()
    line.append("Master: ", style=theme.DIM)
    line.append("BUSY" if busy else "IDLE", style=theme.WARNING if busy else theme.SUCCESS)
    line.append(f" | Active Workers: {workers} | Total Tokens: {tokens:,}")
    line.append(f" | Model: {model or 'sem login'}", style=theme.PRIMARY if model else theme.WARNING)
    if auto_remaining > 0:
        line.append(f" | Auto: {int(auto_remaining // 60)}min", style=theme.WARNING)
    else:
        line.append(" | Auto: off", style=theme.DIM)
    title = f"Termina's Agent v{version}" + (f" [Session: #{session}]" if session else "")
    return Panel(line, title=Text(title, style=f"bold {theme.PRIMARY}"), title_align="left", border_style=theme.PRIMARY)


def hitl_box(decision: GuardrailDecision) -> Panel:
    """Modal de aprovação (docs/6, wireframe 6.2.2)."""
    request = decision.request
    style = theme.RISK_STYLE[decision.risk]
    rows = Table.grid(padding=(0, 1))
    rows.add_column(style="bold", no_wrap=True)
    rows.add_column()
    rows.add_row("Agente Solicitante:", Text(request.agent))
    rows.add_row("Risco:", Text(decision.risk.label, style=f"bold {style}"))
    if decision.reasons:
        rows.add_row("Motivos:", Text(", ".join(decision.reasons), style=theme.DIM))
    kind = "Comando de shell" if request.kind == "shell" else "Chamada de ferramenta"
    rows.add_row("Ação Solicitada:", Text(kind))
    rows.add_row("Comando:" if request.kind == "shell" else "Ferramenta:", Text(request.description, style="bold"))
    if request.justification:
        rows.add_row("Justificativa:", Text(request.justification))
    rows.add_row("ID:", Text(decision.id, style=theme.DIM))

    options = Text()
    options.append("[y]", style=f"bold {theme.SUCCESS}")
    options.append(" Aprovar uma vez   ")
    options.append("[a]", style=f"bold {theme.SUCCESS}")
    options.append(" Aprovar igual nesta sessão   ")
    options.append("[n]", style=f"bold {theme.ERROR}")
    options.append(" Negar")
    if request.kind == "shell":
        options.append("   ")
        options.append("[m]", style=f"bold {theme.WARNING}")
        options.append(" Modificar comando")
    question = Text("Deseja autorizar a execução desta ação?", style="bold")
    return Panel(
        Group(rows, Text(), question, options),
        title=Text("⚠ ALERTA DE SEGURANÇA: APROVAÇÃO NECESSÁRIA", style=f"bold {style}"),
        title_align="left",
        border_style=style,
    )


def help_panel(commands: Iterable[tuple[str, str]]) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(style=f"bold {theme.PRIMARY}", no_wrap=True)
    table.add_column()
    for usage, summary in commands:
        table.add_row(usage, summary)
    footer = Text("\nTexto sem '/' é tratado como /run <meta>. Ctrl+C cancela a execução em curso.", style=theme.DIM)
    return Panel(Group(table, footer), title="Comandos", title_align="left", border_style=theme.PRIMARY)
