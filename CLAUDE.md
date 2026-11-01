# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Issues #1-#8, #17 (one-line installers) and #20 (provider login) of the project board are implemented: scaffolding, memory, A2A contracts, planner/DAG, RAG, guardrails (HITL), 9 native skills + MCP client, the Rich/prompt_toolkit terminal, installers, and `/login`/`/logout`/`/model`. `docs/` is the source of truth. Docs and user-facing text are in Portuguese; keep that language for docs, CLI output and user-facing strings.

Not built yet: a real A2A dispatcher/bus (the engine calls registered `BaseSubagent` objects directly), any loader that plugs external subagents into the app (the registry starts empty, so `/run` nodes with no executor are `skipped`), an LLM-based Output Synthesizer (`MasterEngine.synthesize` is deterministic), cron routines (RF-12), and the Telegram companion.

## Commands

Python >=3.11. Setup (Windows PowerShell: `.\.venv\Scripts\Activate.ps1`):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                 # dev pulls pytest, pypdf and mcp; add ".[rag]" for fastembed
terminas                                # interactive terminal (/help lists commands)
terminas run "<goal>"                   # plan + run once; exit 0 ok, 1 partial/skipped, 2 error
terminas --config other.yaml            # default is ./config.yaml, else built-in defaults
bash install.sh / ./install.ps1         # one-line installers (uv-based); TERMINAS_SOURCE=<dir> installs a local checkout
pytest -W error                         # all tests; the suite must stay warning-free
pytest -m "not integration"             # fast unit suite (~14s)
pytest tests/unit/test_x.py::test_name  # single test
pytest --cov=src --cov-branch           # ~99% today
```

`integration` tests use real resources: the FastEmbed model (first run downloads ~87 MB into `.terminas/models`, gitignored) and a real stdio MCP server (`tests/fixtures/mcp_fake_server.py`). Optional extras: `rag` (fastembed, pypdf), `mcp` (mcp>=2). No linter/formatter is configured.

Installers: `install.sh` and `install.ps1` at the repo root are what the README one-liners download from `raw.githubusercontent.com/.../main/`. Keep them pure ASCII with LF endings (`.gitattributes` enforces LF; PowerShell 5.1 mangles non-ASCII in `irm | iex`). In `install.ps1` never call `exit` unless the running file is `install.ps1` itself (inside `iex` it would close the user's session). They install with `uv tool install "terminas-agent[extras] @ <github zip>"`, so git is not needed. `tests/unit/test_installers.py` simulates every branch with a fake `uv`/`curl` (works in Git Bash and Linux); the real installs run in `.github/workflows/install.yml`. For a real Linux check from Windows use `wsl -d Ubuntu` with `HOME` pointed at a temp dir.

Test isolation: `tests/conftest.py` points `TERMINAS_HOME` at a temp dir, removes provider env vars and stubs the (slow) Windows `icacls` call for every test (mark with `real_permissions` to exercise real permissions). `tests.helpers.build_app` logs in via a fake env key by default (`logged_in=False` for the blocked state). Installer tests must NEVER reach a real `uv`: the sandbox filters real `uv` out of PATH and points all `UV_*` dirs at temp folders (a leak once partially uninstalled a developer's real install).

Windows gotchas seen in this repo: do not pipe non-ASCII text or backslash escapes (`\uXXXX`) through bash/python heredocs (they get mangled, and long heredocs break the shell tool; use the file-writing tools, or `chr(92)` in Python); Windows PowerShell 5.1 prefixes a UTF-8 BOM to text piped into a native program (`PlainReader` strips it); never name a test helper `test_*` in a module that tests import (pytest collects it); `write_text` converts `
` to `
`.

## Architecture (see `docs/3 - Arquitetura.md`)

Hub-and-spoke. Termina is **only the Master Orchestrator**. Subagents (Dev, Research, ...) are **external, decoupled modules** built separately and plugged in through a contract. Do not implement concrete subagents here; implement `BaseSubagent`, the registry and the bus.

Flow: UI (`ui/cli.py`, built by `ui/services.py`) -> Input Gateway (slash commands, deterministic ones skip the LLM) -> Planner (goal -> DAG) -> Dispatcher -> Guardrails (HITL) -> async A2A bus -> external workers -> Output Synthesizer -> UI. Planner also reads Memory, RAG and Skills.

Layout under `src/`: `auth/` (provider login: `providers`, `store`, `validation`, `manager`, `client`), `utils/` (`http`, `secrets`), `core/` (`dag`, `planner`, `engine` = MasterEngine, `guardrails`, `audit`), `rag/`, `skills/` (`base`, `registry`, `mcp_client`, `native/`, `plugins/`), `memory/` (`store`, `engine`, `config` = config.yaml loader + `/behavior` overrides), `subagents_bridge/` (`base`, `registry`, `schemas`, `telemetry`), `ui/` (`cli` app + slash-command parser, `prompt` readers, `services` wiring, `components/`), `main.py`. Tests share dublês in `tests/helpers.py` (`build_app`, `ScriptedReader`, `Worker`, `FakeLLM`, `ForbiddenLLM`).

Cross-cutting rules that span modules:
- **A2A contract**: typed JSON via Pydantic. Dispatch payload (`task_id`, `parent_task_id`, `assigned_agent`, `goal`, `context` incl. RAG chunks with file+lines, `constraints`: `max_steps`, `timeout_seconds`, `allowed_skills`); result payload (`task_id`, `status`, `summary`, `artifacts`, `token_usage`, `error`). Invalid result must raise `ValidationError` and be rejected (CT-07). Models: `AgentTask`, `AgentResult` (alias `AgentTaskResult`), in `src/subagents_bridge/schemas.py`.
- **Async everywhere**: `asyncio`; independent DAG nodes must start in the same event-loop cycle (<50ms apart, CT-02); UI must give feedback <200ms.
- **HITL**: every dangerous action (destructive shell, deletes, critical writes) passes `GuardrailEngine.evaluate()` first and goes to `AWAITING_APPROVAL` (alias `AWAITING_USER_APPROVAL`); nothing executes before approval (CT-05). Every `Skill.execute` goes through the guardrail; skill operations declare their risk in `SKILL_TOOL_RISKS` and unknown ones default to Medium. `/auto` (`GuardrailEngine.enable_auto`) waives Medium only, never Critical, and expires on its own. Classification is a text heuristic, not a sandbox.
- **Provider login (`src/auth`, docs/7)**: the agent is blocked until a provider is connected; only `ALLOWED_WITHOUT_LOGIN` commands (`/help /login /logout /status /skills /exit`) work. Login is API key only (Claude/Codex/Gemini): hidden prompt (`LineReader.read_secret`), validated by listing models (key in a header, never in the URL), stored in `~/.terminas/.env` (`TERMINAS_HOME` overrides; 0600/ACL) with non-secret state in `auth.json`. Env vars still count as login and win over stored keys. OAuth is deliberately not implemented (Anthropic forbids it for third parties; see docs/7). The planner calls `authenticated_llm`, which resolves the active model at call time. Keys must never reach the screen, memory, history or audit log: `utils/secrets.redact/detect_secret` guard `TerminasApp._say`, `handle_line`, `JsonlAuditLog` and the prompt history; keep that when adding output paths.
- **LLM-agnostic via LiteLLM** (Claude, Gemini, OpenAI, Ollama). Cheap/fast models for triage. Tests must mock the LLM (zero token cost).
- **Concurrency/threads**: SQLite connections are thread-bound; only the RAG store is opened with `check_same_thread=False` (guarded by a lock in `RAGEngine`) because `rag-indexer` runs it in a worker thread. `McpManager` must be opened and closed in the same asyncio task (anyio cancel scopes). The run task is awaited through `asyncio.shield` and agents are notified (`cancel_all`) *before* the task is cancelled, otherwise telemetry has already forgotten the active tasks.
- **Memory**: 4 tiers (working volatile, short-term session window, episodic in SQLite, long-term rules/profile). RAG uses local CPU embeddings + SQLite (`sqlite-vec`) or DuckDB; no cloud services for persistence.
- **Audit**: guardrail events are written to `<data_dir>/logs/audit.jsonl` (`JsonlAuditLog`); A2A messages, RAG retrievals and token usage are not logged yet (RNF-03).
- **Limits**: subagent `max_steps = 15` plus per-task timeout; retry/fallback route on failure with JSONL error log (CT-09).
- **Portability**: Windows 11 + Linux identical. Use `pathlib` only, no OS-specific shell calls; mixed separators must resolve identically (`PathHelper.normalize`, CT-08). Hardware target: i5 12th gen, 8 GB RAM; local models only 1-3B quantized or CPU embeddings.
- **9 native skills** (fixed ids): `issue-flow`, `doc-gen`, `google-workspace`, `deep-search`, `swarm-inspector`, `daily-briefing`, `rag-indexer`, `shell-safe-runner`, `quick-notes`. Extra skills via `src/skills/plugins/` discovery and MCP servers.
- **Slash commands**: `/plan`, `/run`, `/rag index|ask`, `/skills`, `/status`, `/agents`, `/memory`, `/behavior`, `/approve <id>`, `/cancel`, `/clear`. Behavior customizable through `config.yaml` or `/behavior`, without losing built-in defaults.

## Tests

`docs/4 - Plano-testes-software.md` defines CT-01..CT-09; CT-01..CT-07 are covered (CT-08 path portability and CT-09 retry/fallback are not). `docs/5 - Registro-testes-software.md` (traceability matrix) has not been updated yet. UI tests render into a `rich.console.Console(file=StringIO)` and drive the app with `ScriptedReader`; real LLM calls are always mocked.

## Git

Work branch `development`; PRs target `main`. Conventional Commits.
