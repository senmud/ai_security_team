from __future__ import annotations

"""
飞书 Socket Mode（WebSocket 长连接）机器人。

特点：
- **不需要**公网回调地址/内网穿透
- 通过 `lark-oapi` SDK 与开放平台建立长连接接收事件（im.message.receive_v1）
- 收到文本消息后调用本项目 Deep Agent 生成回复，并 reply 回原消息线程

飞书控制台需要把「事件订阅方式」切换为：使用长连接接收事件（Socket Mode）。
"""

import json
import os
import re
import threading
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.core import JSON
from langchain_core.messages import AIMessageChunk
from langchain_openai import ChatOpenAI

from .agents import create_security_deep_agent
from .feishu_client import FeishuClient, FeishuCredentials


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _build_llm() -> ChatOpenAI:
    api_key = _env("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required")
    model_name = _env("OPENAI_MODEL", "glm-5-turbo")
    base_url = _env("OPENAI_BASE_URL")
    kwargs: dict[str, object] = {"model": model_name, "temperature": 0.0}
    if base_url:
        kwargs["base_url"] = base_url
    return ChatOpenAI(**kwargs)


def _build_feishu_client() -> FeishuClient:
    app_id = _env("FEISHU_APP_ID")
    app_secret = _env("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET are required")
    base_url = _env("FEISHU_BASE_URL", "https://open.feishu.cn")
    return FeishuClient(FeishuCredentials(app_id=app_id, app_secret=app_secret, base_url=base_url))


def _extract_context_from_event(
    data: P2ImMessageReceiveV1,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    从接收消息事件中提取 (message_id, chat_id, text)。

    这里不依赖 SDK 的属性封装，而是利用 JSON.marshal 得到官方结构，
    再按文档字段解析，避免版本差异导致属性缺失（如 msg_type）。
    """
    try:
        raw = JSON.marshal(data)
        payload = json.loads(raw)
    except Exception:
        print("[FeishuSocketBot] failed to marshal event JSON", flush=True)
        return None, None, None

    event = (payload or {}).get("event") or {}
    chat_id = event.get("message", {}).get("chat_id") or (event.get("chat_id") if isinstance(event.get("chat_id"), str) else None)
    msg = event.get("message") or {}
    message_id = msg.get("message_id")
    if not message_id:
        return None, chat_id, None

    msg_type = msg.get("msg_type") or msg.get("message_type")
    if msg_type != "text":
        return message_id, chat_id, None

    content_raw = msg.get("content") or "{}"
    try:
        content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
    except Exception:
        content = {"text": str(content_raw)}
    text = (content.get("text") or "").strip()
    return message_id, chat_id, text or None


def _extract_text_from_chunk_content(content: object) -> str:
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                out.append(str(block.get("text", "")))
        return "".join(out)
    return str(content)


def _plan_line_from_part(part: dict[str, object]) -> str | None:
    """
    仅提取 write_todos 的计划内容与状态，屏蔽中间件噪声。
    """
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
        if name != "write_todos":
            return None
        todos = _extract_todos(data.get("input")) + _extract_todos(data.get("result")) + _extract_todos(data)
        if todos:
            return "\n".join(_render_plan_todos(todos[:3]))
        if "result" not in data and "error" not in data:
            return "> // 计划更新：进行中"
        err = data.get("error")
        if err:
            return f"> // 计划更新：失败（{str(err)[:120]}）"
        return "> // 计划更新：完成"
    return None


def _extract_todos(obj: object) -> list[tuple[str, str]]:
    """从嵌套对象里提取 todos 的 content/status。"""
    found: list[tuple[str, str]] = []

    def walk(x: object) -> None:
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


def _format_markdown_for_feishu(text: str) -> str:
    """
    将模型原始 Markdown 调整为飞书更稳定的 lark_md 展示格式：
    1) 给中间出现的标题标记补换行（避免“说明#### 3...”粘连）
    2) 将 # 标题转换为加粗文本（飞书卡片中比标题语法更稳定）
    """
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    # 若行内突然出现标题起始（### / ## 等），强制断行
    # 兼容 "### 1." 和 "###1." 两种写法
    s = re.sub(r"([^\n])\s*(#{1,6}\s*)", r"\1\n\2", s)
    # 规范化无空格标题：###1. -> ### 1.
    s = re.sub(r"(?m)^(#{1,6})([^\s#])", r"\1 \2", s)
    # 将 ATX 标题转换为加粗行，减少飞书渲染差异
    s = re.sub(r"(?m)^\s*#{1,6}\s*(.+?)\s*$", r"**\1**", s)
    # 兜底：清理残留的标题标记（如 "## 查询结果汇总：" 仍未被转换的场景）
    s = re.sub(r"(?m)^\s*#{1,6}\s*", "", s)
    # 兜底：行内残留标题前缀（避免“文本## 标题”）
    s = re.sub(r"(?<!`)#{2,6}\s+", "", s)
    return s.strip()


def main() -> None:
    """
    启动 Socket Mode 客户端（阻塞）。

    必需环境变量：
    - FEISHU_APP_ID
    - FEISHU_APP_SECRET
    - OPENAI_API_KEY
    """
    app_id = _env("FEISHU_APP_ID")
    app_secret = _env("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET are required")

    llm = _build_llm()
    agent = create_security_deep_agent(llm)
    feishu = _build_feishu_client()

    def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
        print("[FeishuSocketBot] received im.message.receive_v1 event", flush=True)
        message_id, chat_id, text = _extract_context_from_event(data)
        print(f"[FeishuSocketBot] parsed message_id={message_id}, chat_id={chat_id}, text={text!r}", flush=True)
        if not message_id or not text:
            print("[FeishuSocketBot] no valid text to handle, skip.", flush=True)
            return

        send_mode = (_env("FEISHU_SEND_MODE", "send") or "send").lower()
        render_mode = (_env("FEISHU_RENDER_MODE", "markdown") or "markdown").lower()

        def _send_text(out: str) -> None:
            use_markdown = render_mode in ("markdown", "md", "interactive")
            if send_mode == "reply":
                if use_markdown:
                    feishu.reply_markdown(message_id=message_id, markdown=out)
                else:
                    feishu.reply_text(message_id=message_id, text=out)
            else:
                if chat_id:
                    if use_markdown:
                        feishu.send_markdown_chat(chat_id=chat_id, markdown=out)
                    else:
                        feishu.send_text_chat(chat_id=chat_id, text=out)
                else:
                    print("[FeishuSocketBot] missing chat_id, fallback to reply", flush=True)
                    if use_markdown:
                        feishu.reply_markdown(message_id=message_id, markdown=out)
                    else:
                        feishu.reply_text(message_id=message_id, text=out)

        # 飞书要求 3 秒内处理完，否则会重推；这里用后台线程异步回复，避免阻塞 ACK。
        def _work() -> None:
            try:
                print("[FeishuSocketBot] invoking agent...", flush=True)
                answer_chunks: list[str] = []
                plan_lines: list[str] = []
                plan_stream = (_env("FEISHU_PLAN_STREAM", "1") or "1").lower() not in ("0", "false", "no")

                if plan_stream:
                    _send_text("已收到请求，开始规划与执行。")

                for part in agent.stream(
                    {"messages": [{"role": "user", "content": text}]},
                    stream_mode=["tasks", "updates", "messages"],
                    version="v2",
                ):
                    line = _plan_line_from_part(part)
                    if line:
                        plan_lines.append(line)
                        if plan_stream:
                            _send_text("计划轨迹:\n" + line)
                        continue
                    if part.get("type") == "messages":
                        msg, _meta = part["data"]  # type: ignore[index]
                        if isinstance(msg, AIMessageChunk):
                            chunk = _extract_text_from_chunk_content(msg.content)
                            if chunk:
                                answer_chunks.append(chunk)

                reply = "".join(answer_chunks).strip()
                if not reply:
                    reply = "已收到消息，但未生成有效回复。"
                reply = _format_markdown_for_feishu(reply)
                show_plan_in_final = (_env("FEISHU_SHOW_PLAN_TRACE", "0") or "0").lower() not in ("0", "false", "no")
                if show_plan_in_final:
                    if plan_lines:
                        tail = "\n".join(plan_lines[-8:])
                        reply = f"{reply}\n\n---\n计划轨迹（摘要）:\n{tail}"
                print(f"[FeishuSocketBot] reply ready, length={len(reply)}", flush=True)
                _send_text("最终结果:\n" + reply)
            except Exception as e:  # noqa: BLE001
                # 避免异常导致线程崩溃；生产应接入日志系统
                print(f"[FeishuSocketBot] error while handling message: {e!s}", flush=True)
                try:
                    err_msg = f"处理失败：{e!s}"
                    _send_text(err_msg)
                except Exception:
                    pass

        threading.Thread(target=_work, daemon=True).start()

    # Socket Mode 的 handler builder 两个参数必须是空字符串
    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
        .build()
    )

    cli = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )
    print("[FeishuSocketBot] starting websocket client...", flush=True)
    cli.start()


if __name__ == "__main__":
    main()

