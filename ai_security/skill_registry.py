from __future__ import annotations

"""
已安装 Skills 的目录、安装（从 SKILL.md 来源导入）与运行时加载为 LangChain Tool。

目录约定（在 agent 工作区下）：
- `<workspace>/skills/installed/<skill_id>/SKILL.md`

环境变量 `AI_SECURITY_SKILLS_DIR` 可覆盖「已安装 skills」根目录（默认 `<workspace>/skills/installed`）。
"""

import os
import re
import shutil
from pathlib import Path
import httpx
from langchain_core.tools import BaseTool, tool

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


def _extract_yaml_front_matter(skill_md: str) -> dict[str, str]:
    """
    提取 SKILL.md 顶部 `---` front matter 中的扁平键值（仅处理 `k: v`）。
    非严格 YAML 解析，足够覆盖常见 metadata 场景。
    """
    text = (skill_md or "").strip()
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    if not lines:
        return {}
    out: dict[str, str] = {}
    # 跳过第一行 `---`
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*:\s*(.+?)\s*$", line)
        if not m:
            continue
        k, v = m.groups()
        out[k.strip().lower()] = v.strip().strip("'\"")
    return out


def _extract_summary_from_skill_md(skill_md: str, fallback: str = "") -> str:
    """
    从 SKILL.md 提取功能概述：
    1) front matter: summary/description
    2) 首个 H1 标题后的第一段正文
    3) 文档第一段正文
    """
    fm = _extract_yaml_front_matter(skill_md)
    for key in ("summary", "description"):
        val = (fm.get(key) or "").strip()
        if val:
            return val[:220]

    lines = (skill_md or "").splitlines()
    h1_idx = -1
    for i, ln in enumerate(lines):
        if re.match(r"^\s*#\s+\S+", ln):
            h1_idx = i
            break
    search_from = h1_idx + 1 if h1_idx >= 0 else 0
    para: list[str] = []
    for ln in lines[search_from:]:
        s = ln.strip()
        if not s:
            if para:
                break
            continue
        if s.startswith("#"):
            if para:
                break
            continue
        para.append(s)
    if para:
        return " ".join(para)[:220]
    return (fallback or "").strip()[:220]


def _extract_version_from_skill_md(skill_md: str) -> str:
    """
    从 SKILL.md 提取版本号：
    1) front matter: version
    2) 文本中 `version: x.y.z` / `v1.2.3` / `版本：x.y.z`
    """
    fm = _extract_yaml_front_matter(skill_md)
    ver = (fm.get("version") or "").strip()
    if ver:
        return ver

    patterns = [
        r"(?im)^\s*version\s*[:=]\s*([0-9]+(?:\.[0-9]+){0,3}(?:[-+][A-Za-z0-9_.-]+)?)\s*$",
        r"(?i)\bv([0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9_.-]+)?)\b",
        r"(?im)^\s*版本\s*[：:]\s*([0-9]+(?:\.[0-9]+){0,3}(?:[-+][A-Za-z0-9_.-]+)?)\s*$",
    ]
    for pat in patterns:
        m = re.search(pat, skill_md or "")
        if m:
            return m.group(1).strip()
    return "unknown"


def list_installed_skills() -> list[dict[str, str]]:
    """
    返回已安装技能元数据列表：id、name、version、summary。
    /skills 展示从 `SKILL.md` 提取版本与功能概述。
    """
    root = get_installed_skills_root()
    out: list[dict[str, str]] = []
    if not root.is_dir():
        return out
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        sid = sub.name
        skill_md = ""
        md_path = sub / "SKILL.md"
        if md_path.is_file():
            try:
                skill_md = md_path.read_text(encoding="utf-8")
            except Exception:
                skill_md = ""
        name = _extract_title_from_skill_md(skill_md) if skill_md.strip() else ""
        if not name or name == "Imported Skill":
            name = sid
        version = _extract_version_from_skill_md(skill_md) if skill_md.strip() else "unknown"
        summary = _extract_summary_from_skill_md(skill_md, fallback="")
        out.append(
            {
                "id": sid,
                "name": name,
                "version": version,
                "summary": summary or "（未在 SKILL.md 中提取到功能概述）",
            }
        )
    return out


def _build_tool_from_skill_md(skill_id: str, skill_md: str) -> BaseTool:
    safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", skill_id)
    if not safe_name or safe_name[0].isdigit():
        safe_name = f"skill_{safe_name}"
    tool_name = f"skill_{safe_name}"

    @tool
    def _skill_tool(task: str) -> str:
        """执行该 Skill 的说明，并结合当前任务给出建议步骤。"""
        t = (task or "").strip()
        return "请按以下 Skill 指南执行。\n\n" + skill_md + "\n\n---\n" + f"当前任务: {t}"

    _skill_tool.name = tool_name
    return _skill_tool


def load_installed_skill_tools() -> list[BaseTool]:
    """扫描已安装目录，基于各技能 `SKILL.md` 动态生成工具列表。"""
    root = get_installed_skills_root()
    tools: list[BaseTool] = []
    if not root.is_dir():
        return tools
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        md_path = sub / "SKILL.md"
        if not md_path.is_file():
            continue
        try:
            skill_md = md_path.read_text(encoding="utf-8")
            if not skill_md.strip():
                continue
            tools.append(_build_tool_from_skill_md(sub.name, skill_md))
        except Exception:
            continue
    return tools


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
) -> tuple[bool, str]:
    """
    将 SKILL.md 内容写入已安装目录。
    skill_id 需已规范化（见 _slugify_skill_id）。
    """
    skill_id = _slugify_skill_id(skill_id)
    if not skill_id:
        return False, "无效的技能 ID"

    dst = get_installed_skills_root() / skill_id
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)

    (dst / "SKILL.md").write_text(skill_md, encoding="utf-8")
    return True, skill_id


def install_skill_from_skill_md(source: str) -> tuple[bool, str]:
    """
    从 GitHub 或任意 SKILL.md（URL/本地文件）安装技能。
    安装时会写入 SKILL.md，并在运行时由加载器基于 SKILL.md 动态生成工具。
    """
    ok, content_or_err, source_kind = _read_skill_md_source(source)
    if not ok:
        return False, content_or_err

    skill_md = content_or_err
    src_tail = Path((source or "").strip()).name or "skill"
    skill_id = _slugify_skill_id(f"imported-{src_tail}")
    ok2, sid_or_err = materialize_skill_from_markdown(
        skill_md,
        skill_id=skill_id,
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
    skill_id = _slugify_skill_id(f"clawhub-{slug}")
    ok2, sid_or_err = materialize_skill_from_markdown(
        skill_md,
        skill_id=skill_id,
    )
    if not ok2:
        return False, sid_or_err
    return True, f"已从 ClawHub 安装技能 `{sid_or_err}`（slug: `{slug}`）。**当前会话**需重新创建 Agent 后新工具才会加载；飞书机器人每条消息会重建 Agent，可直接生效。"


def install_skill(skill_source: str) -> tuple[bool, str]:
    """
    统一安装入口：
    - 按 GitHub / SKILL.md 来源安装。
    """
    return install_skill_from_skill_md((skill_source or "").strip())


def format_skills_list_markdown() -> str:
    """供 /skills 命令展示的 Markdown 文本。"""
    items = list_installed_skills()
    if not items:
        return (
            "当前未安装任何扩展 Skill。\n\n"
            "使用 `/skill install <GitHub链接|SKILL.md路径|SKILL.md链接>` 导入。"
        )
    lines = ["## 已安装 Skills", "", f"共 {len(items)} 个", ""]
    for it in items:
        lines.append(f"### {it['name']}")
        lines.append(f"- ID：`{it['id']}`")
        lines.append(f"- 版本：`{it['version']}`")
        lines.append(f"- 功能概述：{it['summary']}")
        lines.append("")
    return "\n".join(lines).rstrip()
