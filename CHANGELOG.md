# Changelog

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

