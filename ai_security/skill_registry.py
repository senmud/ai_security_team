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
import re
import shutil
from pathlib import Path
from typing import Any

import httpx
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


def _slugify_skill_id(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (value or "").strip().lower())
    s = s.strip("-_")
    return s or "imported_skill"


def _extract_title_from_skill_md(skill_md: str) -> str:
    for line in (skill_md or "").splitlines():
        t = line.strip()
        if t.startswith("#"):
            return t.lstrip("#").strip() or "Imported Skill"
    return "Imported Skill"


def _fetch_skill_md_from_github_url(url: str) -> tuple[bool, str]:
    raw = (url or "").strip()
    if not raw:
        return False, "空 URL"

    candidates: list[str] = []
    if "raw.githubusercontent.com" in raw:
        candidates.append(raw)
    elif "github.com" in raw:
        # 支持：
        # - https://github.com/<owner>/<repo>
        # - https://github.com/<owner>/<repo>/blob/<ref>/<path>
        m_blob = re.match(r"^https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$", raw)
        if m_blob:
            owner, repo, ref, path = m_blob.groups()
            candidates.append(f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}")
        m_repo = re.match(r"^https?://github\.com/([^/]+)/([^/]+)/?$", raw)
        if m_repo:
            owner, repo = m_repo.groups()
            candidates.extend(
                [
                    f"https://raw.githubusercontent.com/{owner}/{repo}/main/SKILL.md",
                    f"https://raw.githubusercontent.com/{owner}/{repo}/master/SKILL.md",
                ]
            )
    else:
        return False, "不是 GitHub URL"

    with httpx.Client(timeout=12.0, follow_redirects=True) as client:
        for u in candidates:
            try:
                r = client.get(u, headers={"User-Agent": "ai-security-team/skills-installer"})
            except Exception:
                continue
            if r.status_code == 200 and "text" in (r.headers.get("content-type") or "").lower():
                text = r.text
                if text.strip():
                    return True, text
    return False, "无法从 GitHub 下载 SKILL.md，请提供可访问的 SKILL.md 原始链接。"


def _read_skill_md_source(source: str) -> tuple[bool, str, str]:
    """
    读取 SKILL.md 来源。
    返回 (ok, content_or_error, source_kind)。
    """
    src = (source or "").strip()
    if not src:
        return False, "来源为空", ""

    if src.startswith("http://") or src.startswith("https://"):
        if "github.com" in src or "raw.githubusercontent.com" in src:
            ok, text_or_err = _fetch_skill_md_from_github_url(src)
            return ok, text_or_err, "github"
        try:
            r = httpx.get(src, timeout=12.0, follow_redirects=True)
            if r.status_code != 200:
                return False, f"下载失败，HTTP {r.status_code}", "url"
            text = r.text
            if not text.strip():
                return False, "下载内容为空", "url"
            return True, text, "url"
        except Exception as e:  # noqa: BLE001
            return False, f"下载失败: {e!s}", "url"

    p = Path(src).expanduser()
    if p.is_file():
        try:
            text = p.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            return False, f"读取本地文件失败: {e!s}", "file"
        if not text.strip():
            return False, "本地文件为空", "file"
        return True, text, "file"
    return False, "既不是 URL 也不是本地可读文件", ""


def materialize_skill_from_markdown(
    skill_md: str,
    *,
    skill_id: str,
    name: str,
    summary: str,
    description: str,
) -> tuple[bool, str]:
    """
    将 SKILL.md 内容写入已安装目录并生成 manifest.json + tool.py。
    skill_id 需已规范化（见 _slugify_skill_id）。
    """
    skill_id = _slugify_skill_id(skill_id)
    if not skill_id:
        return False, "无效的技能 ID"

    dst = get_installed_skills_root() / skill_id
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)

    tool_fn = f"skill_{_slugify_skill_id(skill_id).replace('-', '_')}"
    skill_text_literal = repr(skill_md)
    tool_code = (
        "from __future__ import annotations\n\n"
        "from langchain_core.tools import tool\n\n"
        f"SKILL_TEXT = {skill_text_literal}\n\n"
        "@tool\n"
        f"def {tool_fn}(task: str) -> str:\n"
        '    """执行该 Skill 的说明，并结合当前任务给出建议步骤。"""\n'
        "    t = (task or '').strip()\n"
        "    return (\n"
        "        '请按以下 Skill 指南执行。\\n\\n'\n"
        "        + SKILL_TEXT\n"
        "        + '\\n\\n---\\n'\n"
        "        + f'当前任务: {t}'\n"
        "    )\n\n"
        "def get_tools():\n"
        f"    return [{tool_fn}]\n"
    )
    manifest = {
        "id": skill_id,
        "name": name,
        "version": "0.1.0",
        "summary": summary,
        "description": description,
        "tool_module": "tool",
        "tool_entry": "get_tools",
    }
    (dst / "SKILL.md").write_text(skill_md, encoding="utf-8")
    (dst / "tool.py").write_text(tool_code, encoding="utf-8")
    (dst / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return True, skill_id


def install_skill_from_skill_md(source: str) -> tuple[bool, str]:
    """
    从 GitHub 或任意 SKILL.md（URL/本地文件）安装技能。
    安装时会生成 manifest.json + tool.py，使其可被当前技能加载器识别。
    """
    ok, content_or_err, source_kind = _read_skill_md_source(source)
    if not ok:
        return False, content_or_err

    skill_md = content_or_err
    title = _extract_title_from_skill_md(skill_md)
    src_tail = Path((source or "").strip()).name or "skill"
    skill_id = _slugify_skill_id(f"{title}-{src_tail}")
    ok2, sid_or_err = materialize_skill_from_markdown(
        skill_md,
        skill_id=skill_id,
        name=title,
        summary="Installed from SKILL.md source",
        description=f"Imported from {source_kind}: {source}",
    )
    if not ok2:
        return False, sid_or_err
    return True, f"已安装技能 `{sid_or_err}`（来源: {source_kind}）。"


def install_skill_from_clawhub_slug(slug: str) -> tuple[bool, str]:
    """从 ClawHub（经 Layer API）拉取 SKILL.md 并安装为本地扩展 Skill。"""
    from .clawhub_client import fetch_skill_markdown_from_clawhub

    slug = (slug or "").strip()
    if not slug or not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$", slug):
        return False, "无效的 skill slug。"

    ok, md_or_err = fetch_skill_markdown_from_clawhub(slug)
    if not ok:
        return False, md_or_err

    skill_md = md_or_err
    title = _extract_title_from_skill_md(skill_md)
    skill_id = _slugify_skill_id(f"clawhub-{slug}")
    ok2, sid_or_err = materialize_skill_from_markdown(
        skill_md,
        skill_id=skill_id,
        name=title,
        summary=f"ClawHub: {slug}",
        description=f"Imported from ClawHub slug `{slug}`",
    )
    if not ok2:
        return False, sid_or_err
    return True, f"已从 ClawHub 安装技能 `{sid_or_err}`（slug: `{slug}`）。**当前会话**需重新创建 Agent 后新工具才会加载；飞书机器人每条消息会重建 Agent，可直接生效。"


def install_skill(skill_source: str) -> tuple[bool, str]:
    """
    统一安装入口：
    - 若参数匹配内置 catalog 技能 ID，则从 catalog 安装；
    - 否则尝试按 GitHub / SKILL.md 来源安装。
    """
    sid = (skill_source or "").strip()
    if sid and (get_skill_catalog_root() / sid).is_dir():
        return install_skill_from_catalog(sid)
    return install_skill_from_skill_md(sid)


def format_skills_list_markdown() -> str:
    """供 /skills 命令展示的 Markdown 文本。"""
    items = list_installed_skills()
    if not items:
        return (
            "当前未安装任何扩展 Skill。\n\n"
            "使用 `/skill install <技能ID>` 从内置目录安装（例如 `hello_echo`），"
            "或 `/skill install <GitHub链接|SKILL.md路径|SKILL.md链接>` 导入。"
        )
    lines = ["已安装的扩展 Skills：", ""]
    for it in items:
        lines.append(f"- **{it['name']}**（`{it['id']}`） v{it['version']}")
        if it["summary"]:
            lines.append(f"  - {it['summary']}")
        else:
            lines.append("  - （无概述）")
        lines.append("")
    return "\n".join(lines).rstrip()
