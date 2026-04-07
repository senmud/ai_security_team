from __future__ import annotations

"""
最小可运行示例：使用 LangChain AI `deepagents` 的 `create_deep_agent`（经 `create_security_deep_agent` 封装）。

使用 LangGraph `stream_mode="messages"` + `version="v2"` 做 **LLM 流式输出**（逐 token/块打印）。

环境变量：
- OPENAI_API_KEY（必填）
- OPENAI_BASE_URL（可选，OpenAI 兼容网关）
- OPENAI_MODEL（可选，默认 glm-5-turbo）
- AI_SECURITY_LOCAL_SHELL：设为 `0` / `false` 时关闭本机工作区 + `execute`（退回默认 StateBackend）
- AI_SECURITY_AGENT_WORKSPACE：Agent 文件与 shell 工作区目录（默认项目下 `agent_workspace/`）
"""

import os
from typing import Any

from langchain_core.messages import AIMessageChunk
from langchain_openai import ChatOpenAI

from .agents import create_security_deep_agent


def _print_stream_text(content: Any) -> None:
    """将 LLM 流式块中的 content 打印为文本（兼容 str 与多模态块列表）。"""
    if not content:
        return
    if isinstance(content, str):
        print(content, end="", flush=True)
        return
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                print(block.get("text", ""), end="", flush=True)


def _plan_line_from_part(part: dict[str, Any]) -> str | None:
    """仅提取 write_todos 的计划内容与状态，屏蔽中间件噪声。"""
    ptype = part.get("type")
    data = part.get("data")

    if ptype == "updates":
        todos = _extract_todos(data)
        if todos:
            return "\n".join(_render_plan_todos(todos[:3]))
        return None

    if ptype == "tasks":
        if not isinstance(data, dict):
            return None
        name = str(data.get("name") or "unknown")
        # 只关注 write_todos，避免 PatchToolCallsMiddleware.before_agent 等噪声
        if name != "write_todos":
            return None
        todos = _extract_todos(data.get("input")) + _extract_todos(data.get("result")) + _extract_todos(data)
        if todos:
            return "\n".join(_render_plan_todos(todos[:3]))
        if "result" not in data and "error" not in data:
            return "> // 计划更新：进行中"
        if data.get("error"):
            return f"> // 计划更新：失败（{str(data.get('error'))[:120]}）"
        return "> // 计划更新：完成"
    return None


def _extract_todos(obj: Any) -> list[tuple[str, str]]:
    """从嵌套对象里提取 todos 的 content/status。"""
    found: list[tuple[str, str]] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            todos = x.get("todos")
            if isinstance(todos, list):
                for t in todos:
                    if not isinstance(t, dict):
                        continue
                    content = str(t.get("content") or "").strip()
                    status = str(t.get("status") or "").strip()
                    if content:
                        found.append((content, status or "pending"))
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)

    walk(obj)
    # 去重保序
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for s in found:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _render_plan_todos(todos: list[tuple[str, str]]) -> list[str]:
    lines: list[str] = []
    for content, status in todos:
        st = status.lower()
        if st == "completed":
            lines.append(f"- ✅ ~~{content}~~（已完成）")
        elif st == "failed":
            lines.append(f"- ❌ ~~{content}~~（失败）")
        elif st == "in_progress":
            lines.append(f"- 🔄 {content}（进行中）")
        else:
            lines.append(f"- ⏳ {content}（待处理）")
    return lines


def main() -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("请先设置 OPENAI_API_KEY 环境变量。")

    model_name = os.environ.get("OPENAI_MODEL", "glm-5-turbo")
    base_url = os.environ.get("OPENAI_BASE_URL")
    llm_kwargs: dict[str, object] = {
        "model": model_name,
        "temperature": 0.0,
        "streaming": True,
    }
    if base_url:
        llm_kwargs["base_url"] = base_url

    llm = ChatOpenAI(**llm_kwargs)

    use_local = os.environ.get("AI_SECURITY_LOCAL_SHELL", "1").lower() not in ("0", "false", "no")
    agent = create_security_deep_agent(llm, use_local_workspace_and_shell=use_local)

    user_input = os.environ.get(
        "DEMO_USER_MESSAGE",
        "查下港大的nanobot、hermes-agent最新release的版本" # "检测到一台生产服务器上有可疑加密进程行为，与勒索软件特征相似。",
    )

    print("=== Streaming Answer ===\n", flush=True)
    print("=== Plan Trace ===", flush=True)
    plan_lines: list[str] = []

    for part in agent.stream(
        {"messages": [{"role": "user", "content": user_input}]},
        stream_mode=["tasks", "updates", "messages"],
        version="v2",
    ):
        line = _plan_line_from_part(part)
        if line:
            plan_lines.append(line)
            print(line, flush=True)
            continue

        if part.get("type") == "messages":
            msg, _meta = part["data"]
            if isinstance(msg, AIMessageChunk):
                _print_stream_text(msg.content)

    print("\n\n=== Plan Trace Summary ===", flush=True)
    for ln in plan_lines[-30:]:
        print(ln, flush=True)
    print("", flush=True)


if __name__ == "__main__":
    main()
