# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Early design phase. `docs/` holds the full spec; `src/` is only empty `__init__.py` scaffolding (`src/`, `src/core/`, `src/rag/`). No `main.py`, no `tests/`, no `config.example.yaml` yet. Treat `docs/` as the source of truth and build to it. Docs and user-facing text are in Portuguese; keep that language for docs, CLI output and user-facing strings.

## Commands

Python >=3.11. Setup (Windows PowerShell: `.\.venv\Scripts\Activate.ps1`):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                                  # all tests (testpaths=tests, asyncio_mode=auto, pythonpath=".")
pytest tests/unit/test_x.py::test_name  # single test
pytest --cov=src                        # coverage (target >=80% on core/, rag/, skills/, guardrails)
```

Entry point is declared as `terminas = src.main:main` (`pyproject.toml`) but `src/main.py` does not exist yet. README plans `terminas` (interactive TUI) and `terminas run "<goal>"`. No linter/formatter configured; docs mention `mypy --strict` only as an example.

`docs/4` also plans `pytest-mock` and `respx`; they are not yet in the `dev` extra. Add when needed.

## Architecture (planned, see `docs/3 - Arquitetura.md`)

Hub-and-spoke. Termina is **only the Master Orchestrator**. Subagents (Dev, Research, ...) are **external, decoupled modules** built separately and plugged in through a contract. Do not implement concrete subagents here; implement `BaseSubagent`, the registry and the bus.

Flow: UI (`ui/cli.py`) -> Input Gateway (slash commands, deterministic ones skip the LLM) -> Planner (goal -> DAG) -> Dispatcher -> Guardrails (HITL) -> async A2A bus -> external workers -> Output Synthesizer -> UI. Planner also reads Memory, RAG and Skills.

Intended layout under `src/`: `core/` (engine, planner, dispatcher, guardrails), `rag/` (ingestion, chunking, embeddings, vector_store), `skills/` (registry, `native/`, `plugins/`, `mcp_client.py`), `memory/` (SQLite store, `config.yaml` loader), `subagents_bridge/` (`base.py`, `registry.py`, `schemas.py`), `ui/`, `main.py`.

Cross-cutting rules that span modules:
- **A2A contract**: typed JSON via Pydantic. Dispatch payload (`task_id`, `parent_task_id`, `assigned_agent`, `goal`, `context` incl. RAG chunks with file+lines, `constraints`: `max_steps`, `timeout_seconds`, `allowed_skills`); result payload (`task_id`, `status`, `summary`, `artifacts`, `token_usage`, `error`). Invalid result must raise `ValidationError` and be rejected (CT-07). Models: `AgentTask`, `AgentResult` (alias `AgentTaskResult`), in `src/subagents_bridge/schemas.py`.
- **Async everywhere**: `asyncio`; independent DAG nodes must start in the same event-loop cycle (<50ms apart, CT-02); UI must give feedback <200ms.
- **HITL**: every dangerous action (destructive shell, deletes, critical writes) passes `GuardrailEngine.evaluate()` first and goes to `AWAITING_USER_APPROVAL`; nothing executes before approval (CT-05). Applies to `shell-safe-runner` and to subagent requests.
- **LLM-agnostic via LiteLLM** (Claude, Gemini, OpenAI, Ollama). Cheap/fast models for triage. Tests must mock the LLM (zero token cost).
- **Memory**: 4 tiers (working volatile, short-term session window, episodic in SQLite, long-term rules/profile). RAG uses local CPU embeddings + SQLite (`sqlite-vec`) or DuckDB; no cloud services for persistence.
- **Audit**: all A2A messages, RAG retrievals and token usage go to structured JSONL logs (RNF-03).
- **Limits**: subagent `max_steps = 15` plus per-task timeout; retry/fallback route on failure with JSONL error log (CT-09).
- **Portability**: Windows 11 + Linux identical. Use `pathlib` only, no OS-specific shell calls; mixed separators must resolve identically (`PathHelper.normalize`, CT-08). Hardware target: i5 12th gen, 8 GB RAM; local models only 1-3B quantized or CPU embeddings.
- **9 native skills** (fixed ids): `issue-flow`, `doc-gen`, `google-workspace`, `deep-search`, `swarm-inspector`, `daily-briefing`, `rag-indexer`, `shell-safe-runner`, `quick-notes`. Extra skills via `src/skills/plugins/` discovery and MCP servers.
- **Slash commands**: `/plan`, `/run`, `/rag index|ask`, `/skills`, `/status`, `/agents`, `/memory`, `/behavior`, `/approve <id>`, `/cancel`, `/clear`. Behavior customizable through `config.yaml` or `/behavior`, without losing built-in defaults.

## Tests

Planned in `docs/4 - Plano-testes-software.md` as `tests/unit/` and `tests/integration/`, with CT-01..CT-09 mapped to requirements RF/RNF. Update `docs/5 - Registro-testes-software.md` (traceability matrix) when tests are added. UI tests capture output via `rich.console.Console` buffer.

## Git

Work branch `development`; PRs target `main`. Conventional Commits.
