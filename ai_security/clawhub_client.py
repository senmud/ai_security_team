from __future__ import annotations

"""
ClawHub 技能集市 HTTP 客户端。

默认使用社区维护的 ClawHub Layer API（与 OpenClaw 文档中的 `clawhub` CLI 同源数据缓存），
可通过环境变量 `AI_SECURITY_CLAWHUB_API_BASE` 覆盖基址（与 `CLAWHUB_REGISTRY` 概念类似）。
"""

import json
import os
from typing import Any

import httpx


def get_clawhub_api_base() -> str:
    return (os.environ.get("AI_SECURITY_CLAWHUB_API_BASE") or "https://clawhub.atomicbot.ai").rstrip("/")


def _summary_text(summary: Any) -> str:
    if summary is None:
        return ""
    if isinstance(summary, str):
        return summary.strip()
    try:
        return json.dumps(summary, ensure_ascii=False)
    except Exception:
        return str(summary)


def search_clawhub_skills(query: str, *, limit: int = 8) -> str:
    """
    在 ClawHub 检索技能，返回供模型阅读的纯文本列表。
    """
    q = (query or "").strip()
    if not q:
        return "请提供非空的检索关键词。"

    limit = max(1, min(int(limit), 25))
    url = f"{get_clawhub_api_base()}/api/skills"
    params = {"q": q, "limit": limit, "sort": "relevance", "page": 1, "nonSuspiciousOnly": True}
    try:
        r = httpx.get(
            url,
            params=params,
            timeout=15.0,
            headers={"User-Agent": "ai-security-team/clawhub-search"},
        )
    except Exception as e:  # noqa: BLE001
        return f"ClawHub 检索请求失败: {e!s}"

    if r.status_code != 200:
        return f"ClawHub 检索失败，HTTP {r.status_code}。"

    try:
        data = r.json()
    except Exception as e:  # noqa: BLE001
        return f"ClawHub 返回非 JSON: {e!s}"

    items = data.get("items") or []
    if not items:
        return f"ClawHub 未找到与「{q}」相关的技能，可换关键词再试。"

    lines: list[str] = [
        f"ClawHub 检索「{q}」命中 {len(items)} 条（最多展示 {limit} 条）：",
        "",
        "若本地已安装技能可覆盖任务，请**不要**重复安装；仅在无合适本地技能时使用 `clawhub_install_skill`。",
        "",
    ]
    for i, it in enumerate(items, 1):
        slug = str(it.get("slug") or "").strip()
        name = str(it.get("displayName") or slug)
        sm = _summary_text(it.get("summary"))
        stars = (it.get("stats") or {}).get("stars")
        lines.append(f"{i}. **{name}** — `slug={slug}`")
        if sm:
            lines.append(f"   {sm[:500]}{'…' if len(sm) > 500 else ''}")
        if stars is not None:
            lines.append(f"   stars: {stars}")
        lines.append("")
    lines.append("安装示例：`clawhub_install_skill` 参数填上表中的 `slug`。")
    return "\n".join(lines).rstrip()


def fetch_skill_markdown_from_clawhub(slug: str) -> tuple[bool, str]:
    """拉取指定 slug 的 SKILL.md 正文。"""
    slug = (slug or "").strip()
    if not slug:
        return False, "slug 为空"

    base = get_clawhub_api_base()
    headers = {"User-Agent": "ai-security-team/clawhub-fetch"}

    detail_url = f"{base}/api/skills/{slug}"
    try:
        r = httpx.get(detail_url, timeout=20.0, headers=headers, follow_redirects=True)
    except Exception as e:  # noqa: BLE001
        return False, f"拉取技能详情失败: {e!s}"

    if r.status_code != 200:
        return False, f"技能 `{slug}` 不存在或不可访问（HTTP {r.status_code}）。"

    try:
        data = r.json()
    except Exception as e:  # noqa: BLE001
        return False, f"详情非 JSON: {e!s}"

    md = data.get("skillMd")
    if isinstance(md, str) and md.strip():
        return True, md

    # 回退：单独请求文件
    file_url = f"{base}/api/skills/{slug}/files"
    try:
        r2 = httpx.get(
            file_url,
            params={"path": "SKILL.md"},
            timeout=20.0,
            headers=headers,
            follow_redirects=True,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"拉取 SKILL.md 失败: {e!s}"

    if r2.status_code != 200:
        return False, f"无法读取 `{slug}` 的 SKILL.md（HTTP {r2.status_code}）。"

    text = r2.text
    if r2.headers.get("content-type", "").startswith("application/json"):
        try:
            parsed = r2.json()
            if isinstance(parsed, str):
                text = parsed
        except Exception:
            pass

    if not (text or "").strip():
        return False, f"技能 `{slug}` 的 SKILL.md 为空。"

    return True, text
