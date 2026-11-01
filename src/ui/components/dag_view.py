"""Árvore de status do DAG: uma etapa por nível; nós da mesma etapa rodam em paralelo."""

from __future__ import annotations

from collections.abc import Mapping

from rich.panel import Panel
from rich.text import Text
from rich.tree import Tree

from src.core.dag import TaskDAG
from src.core.engine import NodeOutcome, NodeState
from src.ui.components import theme


def _node_label(
    dag: TaskDAG, node_id: str, state: NodeState, outcome: NodeOutcome | None
) -> Text:
    node = dag.get(node_id)
    label = Text()
    label.append(f"{theme.STATE_SYMBOL[state]} ", style=theme.STATE_STYLE[state])
    label.append(f"{node.id}", style="bold")
    label.append(f"  {node.description}")
    label.append(f"  [{node.agent or 'master'}]", style=theme.PRIMARY)
    if node.depends_on:
        label.append(f"  ← {', '.join(node.depends_on)}", style=theme.DIM)
    if outcome is not None and outcome.duration:
        label.append(f"  ({outcome.duration:.1f}s)", style=theme.DIM)
    label.append(f"  {theme.STATE_LABEL[state]}", style=theme.STATE_STYLE[state])
    if outcome is not None and outcome.error:
        label.append(f"\n    {outcome.error}", style=theme.STATE_STYLE[state])
    return label


def render_dag(
    dag: TaskDAG,
    states: Mapping[str, NodeState] | None = None,
    outcomes: Mapping[str, NodeOutcome] | None = None,
    title: str = "Plano de Execução (DAG)",
) -> Panel:
    """Painel com a árvore do plano. Sem `states`, todos os nós aparecem como pendentes."""
    states = states or {}
    outcomes = outcomes or {}
    tree = Tree(Text(dag.goal, style="bold"), guide_style=theme.DIM)
    for number, level in enumerate(dag.levels(), start=1):
        heading = Text(f"Etapa {number}", style=theme.PRIMARY)
        if len(level) > 1:
            heading.append(f" — {len(level)} em paralelo", style=theme.DIM)
        branch = tree.add(heading)
        for node_id in level:
            state = states.get(node_id, NodeState.PENDING)
            branch.add(_node_label(dag, node_id, state, outcomes.get(node_id)))
    return Panel(tree, title=title, border_style=theme.PRIMARY, title_align="left")
