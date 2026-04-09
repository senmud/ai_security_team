# Changelog

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

