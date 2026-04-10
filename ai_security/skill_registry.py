from __future__ import annotations

"""
已安装 Skills 的目录、安装（从内置 catalog 复制）与运行时加载为 LangChain Tool。

目录约定（在 agent 工作区下）：
- `<workspace>/skills/installed/<skill_id>/manifest.json`
- `<workspace>/skills/installed/<skill_id>/<tool_module>.py`（默认 tool.py，导出 `get_tools()`）

环境变量 `AI_SECURITY_SKILLS_DIR` 可覆盖「已安装 skills」根目录（默认 `<workspace>/skills/installed`）。
"""

import importlib.util
import json
import os
import shutil
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

from .skills import get_agent_workspace_dir


def _workspace_root() -> Path:
    return get_agent_workspace_dir()


def get_installed_skills_root() -> Path:
    env = os.environ.get("AI_SECURITY_SKILLS_DIR")
    if env:
        return Path(env).expanduser().resolve()
    root = _workspace_root() / "skills" / "installed"
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_skill_catalog_root() -> Path:
    """内置可安装技能包目录（随包发布）。"""
    return Path(__file__).resolve().parent / "skill_catalog"


def _read_manifest(skill_dir: Path) -> dict[str, Any] | None:
    mf = skill_dir / "manifest.json"
    if not mf.is_file():
        return None
    try:
        return json.loads(mf.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_installed_skills() -> list[dict[str, str]]:
    """
    返回已安装技能元数据列表：id、name、version、summary。
    无法读取 manifest 的目录会跳过。
    """
    root = get_installed_skills_root()
    out: list[dict[str, str]] = []
    if not root.is_dir():
        return out
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        data = _read_manifest(sub)
        if not data:
            continue
        sid = str(data.get("id") or sub.name).strip() or sub.name
        out.append(
            {
                "id": sid,
                "name": str(data.get("name") or sid),
                "version": str(data.get("version") or "0.0.0"),
                "summary": str(data.get("summary") or data.get("description") or ""),
            }
        )
    return out


def _load_tools_from_skill_dir(skill_dir: Path, manifest: dict[str, Any]) -> list[BaseTool]:
    mod_name = str(manifest.get("tool_module") or "tool").replace(".py", "").strip() or "tool"
    py_path = skill_dir / f"{mod_name}.py"
    if not py_path.is_file():
        return []
    spec = importlib.util.spec_from_file_location(f"ai_security_skill_{skill_dir.name}_{mod_name}", py_path)
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    entry = str(manifest.get("tool_entry") or "get_tools").strip() or "get_tools"
    fn = getattr(module, entry, None)
    if not callable(fn):
        return []
    raw = fn()
    if raw is None:
        return []
    if isinstance(raw, BaseTool):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [x for x in raw if isinstance(x, BaseTool)]
    return []


def load_installed_skill_tools() -> list[BaseTool]:
    """扫描已安装目录，加载各技能 `get_tools()` 返回的工具列表。"""
    root = get_installed_skills_root()
    tools: list[BaseTool] = []
    if not root.is_dir():
        return tools
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        manifest = _read_manifest(sub)
        if not manifest:
            continue
        try:
            tools.extend(_load_tools_from_skill_dir(sub, manifest))
        except Exception:
            continue
    return tools


def install_skill_from_catalog(skill_id: str) -> tuple[bool, str]:
    """
    从内置 `skill_catalog/<skill_id>` 复制到已安装目录。
    成功返回 (True, 提示)；失败返回 (False, 错误信息)。
    """
    skill_id = (skill_id or "").strip()
    if not skill_id or ".." in skill_id or "/" in skill_id or "\\" in skill_id:
        return False, "无效的技能 ID（仅允许字母数字、下划线、连字符）。"

    src = get_skill_catalog_root() / skill_id
    if not src.is_dir():
        return False, f"内置目录中不存在技能: {skill_id}"

    dst = get_installed_skills_root() / skill_id
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return True, f"已安装技能 `{skill_id}`（来源: skill_catalog）。"


def format_skills_list_markdown() -> str:
    """供 /skills 命令展示的 Markdown 文本。"""
    items = list_installed_skills()
    if not items:
        return "当前未安装任何扩展 Skill。\n\n使用 `/skill install <技能ID>` 从内置目录安装（例如 `hello_echo`）。"
    lines = ["已安装的扩展 Skills：", ""]
    for it in items:
        lines.append(f"- **{it['name']}**（`{it['id']}`） v{it['version']}")
        if it["summary"]:
            lines.append(f"  - {it['summary']}")
        else:
            lines.append("  - （无概述）")
        lines.append("")
    return "\n".join(lines).rstrip()
