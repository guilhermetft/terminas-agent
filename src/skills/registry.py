"""SkillsRegistry: catálogo central de skills nativas, plugins e ferramentas MCP."""

from __future__ import annotations

import importlib.util
import inspect
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from src.core.guardrails import ActionRequest
from src.skills.base import (
    Skill,
    SkillContext,
    SkillError,
    SkillResult,
    SkillSpec,
    UnknownSkillError,
)
from src.skills.mcp_client import McpManager, McpTool


class DuplicateSkillError(SkillError):
    """Já existe uma skill com esse nome."""


@dataclass
class PluginReport:
    loaded: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)  # arquivo -> motivo


class McpToolSkill(Skill):
    """Ferramenta de um servidor MCP exposta como skill, com o schema do próprio servidor."""

    category: ClassVar[str] = "mcp"
    source: ClassVar[str] = "mcp"
    Params: ClassVar[type[BaseModel]] = BaseModel  # não usado: o schema vem do servidor

    def __init__(self, ctx: SkillContext, tool: McpTool) -> None:
        super().__init__(ctx)
        self.tool = tool
        self.name = tool.qualified_name  # type: ignore[misc]
        self.description = tool.description or f"Ferramenta MCP {tool.name}"  # type: ignore[misc]

    def input_schema(self) -> dict[str, Any]:
        return self.tool.input_schema

    def parse_params(self, raw: Any) -> dict[str, Any]:
        arguments = {} if raw is None else raw
        if not isinstance(arguments, dict):
            raise SkillError(f"'{self.name}' espera um objeto de argumentos.")
        try:
            import jsonschema

            jsonschema.validate(arguments, self.tool.input_schema)
        except ImportError:  # sem jsonschema: o próprio servidor valida
            pass
        except jsonschema.ValidationError as exc:
            where = ".".join(str(p) for p in exc.absolute_path) or "argumentos"
            raise SkillError(f"Argumentos inválidos para '{self.name}': {where}: {exc.message}") from None
        return arguments

    def action(self, params: dict[str, Any]) -> ActionRequest:
        # Ferramenta externa de efeito desconhecido: o guardrail trata como risco Médio.
        return ActionRequest.tool_call(self.name, agent="master", **params)

    async def run(self, params: dict[str, Any]) -> SkillResult:
        manager = self.ctx.require("mcp", "Gerenciador MCP")
        result = await manager.call_tool(self.tool.server, self.tool.name, params)
        if result.is_error:
            return SkillResult.failure(result.text or "Ferramenta MCP retornou erro.")
        return SkillResult.success(result.text, structured=result.structured)


class SkillsRegistry:
    def __init__(self, ctx: SkillContext | None = None) -> None:
        self.ctx = ctx or SkillContext()
        self.ctx.skills = self
        self._skills: dict[str, Skill] = {}

    # ------------------------------------------------------------------ registro
    def register(self, skill: Skill, *, replace: bool = False) -> None:
        if not isinstance(skill, Skill):
            raise TypeError(f"Esperado Skill, recebido {type(skill).__name__}.")
        if skill.name in self._skills and not replace:
            raise DuplicateSkillError(f"Skill '{skill.name}' já registrada.")
        self._skills[skill.name] = skill

    def unregister(self, name: str) -> Skill:
        try:
            return self._skills.pop(name)
        except KeyError:
            raise UnknownSkillError(f"Skill '{name}' não registrada.") from None

    def get(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError:
            raise UnknownSkillError(f"Skill '{name}' não registrada.") from None

    def __contains__(self, name: object) -> bool:
        return name in self._skills

    def __len__(self) -> int:
        return len(self._skills)

    def __iter__(self) -> Iterator[Skill]:
        return iter(self._skills.values())

    def names(self, source: str | None = None) -> list[str]:
        return [s.name for s in self if source is None or s.source == source]

    def specs(self) -> list[SkillSpec]:
        return [s.spec for s in self]

    async def invoke(self, name: str, params: Any = None) -> SkillResult:
        """Dispara a skill pelo nome (valida, passa pelo guardrail e executa)."""
        return await self.get(name).execute({} if params is None else params)

    # ------------------------------------------------------------------ fontes
    def register_native(self) -> list[str]:
        from src.skills.native import NATIVE_SKILLS

        for skill_class in NATIVE_SKILLS:
            self.register(skill_class(self.ctx))
        return [cls.name for cls in NATIVE_SKILLS]

    def discover_plugins(self, directory: str | Path) -> PluginReport:
        """Carrega `*.py` do diretório e registra cada subclasse concreta de `Skill`.

        ATENÇÃO: executa o código dos arquivos; use só diretórios de sua confiança.
        Um plugin quebrado é reportado e não impede os demais.
        """
        report = PluginReport()
        folder = Path(directory)
        if not folder.is_dir():
            return report
        for file in sorted(folder.glob("*.py")):
            if file.name.startswith("_"):
                continue
            module_name = f"terminas_plugin_{file.stem}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, file)
                if spec is None or spec.loader is None:
                    raise ImportError("não foi possível carregar o módulo")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                found = [
                    cls
                    for _, cls in inspect.getmembers(module, inspect.isclass)
                    if issubclass(cls, Skill)
                    and cls.__module__ == module_name
                    and not inspect.isabstract(cls)
                ]
                if not found:
                    raise ImportError("nenhuma subclasse concreta de Skill encontrada")
                for cls in found:
                    skill = cls(self.ctx)
                    skill.source = "plugin"  # type: ignore[misc]
                    self.register(skill)
                    report.loaded.append(skill.name)
            except Exception as exc:  # plugin de terceiros: isola qualquer falha
                sys.modules.pop(module_name, None)
                report.errors[file.name] = f"{type(exc).__name__}: {exc}"
        return report

    async def register_mcp_tools(self, manager: McpManager | None = None) -> list[str]:
        """Registra como skills todas as ferramentas dos servidores MCP conectados."""
        manager = manager or self.ctx.require("mcp", "Gerenciador MCP")
        self.ctx.mcp = manager
        names: list[str] = []
        for tool in await manager.list_tools():
            self.register(McpToolSkill(self.ctx, tool), replace=True)
            names.append(tool.qualified_name)
        return names


def build_default_registry(
    ctx: SkillContext | None = None, plugins_dir: str | Path | None = None
) -> SkillsRegistry:
    """Registry pronto: 9 skills nativas carregadas (+ plugins, se `plugins_dir` existir)."""
    registry = SkillsRegistry(ctx)
    registry.register_native()
    if plugins_dir is not None:
        registry.discover_plugins(plugins_dir)
    return registry
