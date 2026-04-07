from __future__ import annotations

"""
可挂载到 deepagents 的扩展技能（LangChain Tool）。

deepagents 自带的文件与 shell 能力由 Backend（如 LocalShellBackend）提供：
- ls / read_file / write_file / edit_file / glob / grep
- execute（本地 shell，见 deepagents 文档安全说明）

本模块提供公网检索技能 web_search。
"""

import os
from pathlib import Path

from langchain_core.tools import tool


def get_agent_workspace_dir() -> Path:
    """
    Agent 读写文件与 execute 默认工作目录（可被环境变量覆盖）。

    环境变量：AI_SECURITY_AGENT_WORKSPACE（绝对或相对路径均可）。
    """
    env = os.environ.get("AI_SECURITY_AGENT_WORKSPACE")
    if env:
        p = Path(env).expanduser().resolve()
    else:
        p = Path(__file__).resolve().parent.parent / "agent_workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """使用 DuckDuckGo 检索公开网页，获取 CVE、厂商公告、新闻等最新信息。query 用简短关键词或完整问句均可。"""
    try:
        from ddgs import DDGS
    except ImportError:
        return (
            "web_search 不可用：请安装依赖 `ddgs`（pip install ddgs）。"
        )

    max_results = max(1, min(int(max_results), 15))
    lines: list[str] = []
    try:
        with DDGS() as ddgs:
            for i, r in enumerate(ddgs.text(query, max_results=max_results)):
                title = (r.get("title") or "").strip()
                href = (r.get("href") or "").strip()
                body = (r.get("body") or "").strip()
                snippet = body[:400] + ("…" if len(body) > 400 else "")
                lines.append(f"{i + 1}. {title}\n   URL: {href}\n   {snippet}")
    except Exception as e:  # noqa: BLE001
        return f"web_search 执行失败: {e!s}"

    if not lines:
        return "未检索到结果，可尝试更换关键词。"
    return "\n\n".join(lines)
