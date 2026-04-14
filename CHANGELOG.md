# Changelog

## 0.4.9 - 2026-04-14

- Feishu Socket Mode: read `message_id` / `chat_id` / text from `P2ImMessageReceiveV1` fields directly; avoid `JSON.marshal` on the full event object to prevent websocket dispatch failures on some payloads (e.g. `NoneType` / `name` during event handling).
- Skill install validation: run `uv venv` under `<skill>/scripts` when `.venv` is missing, before `uv pip install` and subsequent `uv run` self-checks, so installs and runs share one virtual environment.
- Skill install: discover `requirements.txt` from additional locations (for example `scripts/requirements.txt` next to `SKILL.md` or under the repo root) when materializing local and git-based installs.

## 0.4.8 - 2026-04-13

- Skill observability improvements:
  - print `[SkillLoadError]` with `skill_id`/`SKILL.md` path and traceback when installed skill loading fails,
  - return structured `[SkillError]` message from runtime generated skill tools when execution fails.
- Fallback diagnostics improved:
  - print `[SkillExecutionError]` when primary skill toolset run fails before fallback,
  - raise combined error message if both primary and fallback runs fail.

## 0.4.7 - 2026-04-13

- Skill install now supports **git repository source**:
  - create temp dir under `agent_workspace/tmp/`,
  - clone repo (`--depth 1`),
  - copy `SKILL.md` and `scripts/` into installed skill path,
  - rewrite script paths/commands in `SKILL.md`,
  - delete temp directory after install.

## 0.4.6 - 2026-04-13

- Skill install hardening:
  - create `<skill_id>/scripts/` during install,
  - rewrite script paths in `SKILL.md` to `./scripts/...`,
  - rewrite Python script calls to `uv run python ./scripts/...`,
  - rewrite `pip` / `python -m pip` commands to `uv pip ...`.
- Local-file installs now copy sibling `scripts/` directory into installed skill path when present.
- Feishu multi-agent timeout increased from 10 minutes to 30 minutes.

## 0.4.5 - 2026-04-13

- Skills registry simplified to **SKILL.md-only**:
  - removed built-in catalog and manifest-based metadata/runtime dependencies,
  - removed `tool.py` generation/loading path.
- Runtime skill loading now scans installed skill directories and dynamically builds one LangChain tool per `SKILL.md`.
- `/skills` display uses installed directory IDs + parsed `SKILL.md` (title/version/summary) for Feishu-friendly output.
- README and command/docs updated to reflect SKILL.md-only install/load flow.

## 0.4.4 - 2026-04-12

- **Tool split**: `default_security_tools()` no longer includes installed skills or `web_search`; it only returns placeholder security tools + ClawHub installers.
- **Primary agent**: `create_security_deep_agent(tools=None)` now defaults to **`primary_security_tools()`** (installed skills + `web_search` only).
- **`stream_security_agent_with_fallback`**: streams with primary tools first; on **exception**, rebuilds with `default_security_tools()` and retries once. Used by `demo_run` and Feishu bot streaming paths.
- System prompt adds a **tool-binding** note so the model does not call tools absent in the current round.

## 0.4.3 - 2026-04-12

- System prompt: tool/skill priority is now **installed extension skills → built-in file tools + `execute` + `web_search` → ClawHub**; README and tool docstrings aligned.

## 0.4.2 - 2026-04-12

- Agent tools: **`clawhub_search_skills`** and **`clawhub_install_skill`** (ClawHub Layer API, default `https://clawhub.atomicbot.ai`, overridable via **`AI_SECURITY_CLAWHUB_API_BASE`**).
- System prompt and tool ordering: **installed extension skills load first**; model is instructed to use local skills when relevant and only fall back to ClawHub search when no suitable installed skill exists.
- `skill_registry.materialize_skill_from_markdown` refactors SKILL.md installs; **`install_skill_from_clawhub_slug`** fetches `SKILL.md` from the registry API and materializes into `skills/installed/`.

## 0.4.1 - 2026-04-10

- Skill install command now supports multiple sources through `/skill install <source>`:
  - built-in catalog IDs (existing behavior),
  - GitHub URLs (repo root / blob / raw),
  - local or remote `SKILL.md` files.
- Added generic SKILL.md importer in `ai_security/skill_registry.py` that persists original `SKILL.md` and generates runtime-loadable `manifest.json` + `tool.py`.
- Updated `/skill` help text and README command docs accordingly.

## 0.4.0 - 2026-04-10

- Extension Skills registry (`ai_security/skill_registry.py`): install from built-in `skill_catalog` into `agent_workspace/skills/installed/`, optional `AI_SECURITY_SKILLS_DIR`, merge `get_tools()` output into `default_security_tools()`.
- Feishu bot: `/skills` lists installed skills (name, version, summary); `/skill install <id>` and `/skill` help; main agent recreated per message so new skills apply without restart.
- Multi-agent task progress: store a single **plan snapshot** per running task; each child `update` **replaces** the previous snapshot (no per-line key map).

## 0.3.0 - 2026-04-09

- Add multi-agent dispatch for Feishu Socket Mode bot:
  - Heuristic complexity prediction with explicit “simple question” bypass.
  - Spawn child agent in a separate process with queue-based result reporting.
  - Task registry with `/task` command to view running tasks.
  - Automatic timeout kill for tasks running longer than 10 minutes.
  - Stream child-agent task breakdown/progress updates back to the main agent.
  - De-duplicate and replace task breakdown lines instead of appending duplicates.
  - Optional `FEISHU_FORCE_MULTI_AGENT=1` to force dispatch for testing.

## 0.2.0 - 2026-04-08

- Add package version `0.2.0`.

## 0.1.0 - 2026-04-08

- Initial project skeleton.

