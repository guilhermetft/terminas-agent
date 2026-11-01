"""daily-briefing: consolida agenda, issues prioritárias e pendências da memória."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.skills.base import Skill, SkillResult

PENDING_TAG = "pendente"


class DailyBriefingParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: Literal["morning", "evening"] = "morning"
    issue_labels: list[str] = Field(default_factory=list, description="Ex.: ['priority: high'].")
    max_items: int = Field(default=10, ge=1, le=50)


class DailyBriefing(Skill):
    name: ClassVar[str] = "daily-briefing"
    description: ClassVar[str] = (
        f"Briefing matinal/noturno: compromissos (Google Agenda), issues abertas e notas "
        f"com a tag '{PENDING_TAG}'."
    )
    category: ClassVar[str] = "Rotina & Produtividade"
    Params: ClassVar[type[BaseModel]] = DailyBriefingParams

    async def _source(self, skill: str, params: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        """Chama outra skill; falha dela vira seção 'indisponível', nunca derruba o briefing."""
        registry = self.ctx.skills
        if registry is None or skill not in registry:
            return False, f"skill '{skill}' indisponível", {}
        result = await registry.invoke(skill, params)
        if not result.ok:
            return False, result.error or "erro desconhecido", {}
        return True, result.output, result.data

    async def run(self, params: DailyBriefingParams) -> SkillResult:
        now = self.ctx.clock()
        morning = params.period == "morning"
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start.replace(hour=23, minute=59, second=59)

        calendar_args = {
            "time_min": day_start.isoformat(),
            "time_max": day_end.isoformat(),
        }
        calendar = await self._source(
            "google-workspace", {"service": "calendar", "action": "list_events", "arguments": calendar_args}
        )
        issues = await self._source(
            "issue-flow", {"op": "list", "state": "open", "limit": params.max_items, "labels": params.issue_labels}
        )
        notes = await self._source(
            "quick-notes", {"op": "list", "tag": PENDING_TAG, "limit": params.max_items}
        )

        def section(title: str, source: tuple[bool, str, dict[str, Any]], empty: str) -> str:
            ok, text, _ = source
            if not ok:
                return f"## {title}\n_Indisponível: {text}_"
            return f"## {title}\n{text.strip() or empty}"

        heading = "Briefing da manhã" if morning else "Fechamento do dia"
        body = "\n\n".join(
            [
                f"# {heading} — {now:%Y-%m-%d}",
                section("Agenda de hoje" if morning else "Agenda de hoje (resumo)", calendar, "Sem compromissos."),
                section("Issues abertas", self._format_issues(issues), "Nenhuma issue aberta."),
                section("Pendências" if morning else "O que ficou pendente", notes, "Nada pendente."),
            ]
        )
        return SkillResult.success(
            body,
            period=params.period,
            sources={"calendar": calendar[0], "issues": issues[0], "notes": notes[0]},
        )

    @staticmethod
    def _format_issues(source: tuple[bool, str, dict[str, Any]]) -> tuple[bool, str, dict[str, Any]]:
        ok, text, data = source
        items = data.get("json")
        if not ok or not isinstance(items, list):
            return source
        lines = [
            f"- #{i['number']} {i['title']}" + (f" [{', '.join(lb['name'] for lb in i.get('labels', []))}]" if i.get("labels") else "")
            for i in items
        ]
        return ok, "\n".join(lines), data
