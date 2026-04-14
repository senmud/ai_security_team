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
import uuid
import threading
import time
import multiprocessing as mp
from dataclasses import dataclass
from typing import Optional, Callable

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from langchain_core.messages import AIMessageChunk
from langchain_openai import ChatOpenAI

from .agents import stream_security_agent_with_fallback
from .feishu_client import FeishuClient, FeishuCredentials
from .skill_registry import format_skills_list_markdown, install_skill

TASK_TIMEOUT_SECONDS = 1800


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

    直接读取 lark_oapi 已反序列化后的 SDK 模型字段；**不要**对整包事件调用
    `JSON.marshal(data)`：部分事件（mentions 等嵌套字段异常、或 SDK 版本差异）在
    marshal 时可能触发 ``NoneType`` 相关属性错误，导致长连接回调失败。
    """
    try:
        ev = getattr(data, "event", None)
        if ev is None:
            return None, None, None
        msg = getattr(ev, "message", None)
        if msg is None:
            return None, None, None

        chat_id = getattr(msg, "chat_id", None)
        ev_chat = getattr(ev, "chat_id", None)
        if not chat_id and isinstance(ev_chat, str):
            chat_id = ev_chat

        message_id = getattr(msg, "message_id", None)
        if not message_id:
            return None, chat_id, None

        msg_type = getattr(msg, "message_type", None) or getattr(msg, "msg_type", None)
        if msg_type != "text":
            return message_id, chat_id, None

        content_raw = getattr(msg, "content", None)
        if content_raw is None:
            content_raw = "{}"
        if not isinstance(content_raw, str):
            content = {"text": str(content_raw)}
        else:
            try:
                content = json.loads(content_raw)
            except Exception:
                content = {"text": content_raw}
        text = (content.get("text") or "").strip()
        return message_id, chat_id, text or None
    except Exception as e:  # noqa: BLE001
        print(f"[FeishuSocketBot] failed to extract context from event: {e!s}", flush=True)
        return None, None, None


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


def _short_desc(text: str, max_len: int = 10) -> str:
    raw = re.sub(r"\s+", " ", (text or "").strip())
    if not raw:
        return "空任务"
    return raw[:max_len]


def _should_dispatch_multi_agent(text: str) -> tuple[bool, str]:
    t = (text or "").strip()
    force = (_env("FEISHU_FORCE_MULTI_AGENT", "0") or "0").lower() not in ("0", "false", "no")
    if force:
        return True, "force_env"

    # 明确的简单问题：强制走主 agent（避免无意义的子进程开销）
    simple_patterns = [
        r"^(你好|在吗|hi|hello|ping|test|ok|好的|收到|谢谢|thanks)[\s!！。]*$",
        r"^\s*(1\+1|2\+2|3\+3)\s*=?\s*\d*\s*$",
    ]
    for pat in simple_patterns:
        if re.search(pat, t, flags=re.IGNORECASE):
            return False, "simple_whitelist"

    # 很短且无明显复杂信号的问句，倾向主 agent
    if len(t) <= 30:
        qn = t.count("?") + t.count("？")
        has_url = any(x in t for x in ("http://", "https://", "github.com", "docs.", "console"))
        if qn <= 1 and not has_url:
            return False, "short_simple"
    if len(t) >= 80:
        return True, "len>=80"
    # 多段/多问题往往不好评估耗时
    if "\n" in t or t.count("?") + t.count("？") >= 2:
        return True, "multi_part"
    if any(x in t for x in ("http://", "https://", "github.com", "docs.", "console")):
        return True, "has_url"
    hard_keywords = [
        "分析",
        "排查",
        "设计",
        "方案",
        "架构",
        "优化",
        "评估",
        "调研",
        "对比",
        "复杂",
        "不确定",
        "why",
        "root cause",
    ]
    lowered = t.lower()
    if any(k in t or k in lowered for k in hard_keywords):
        return True, "keyword"
    return False, "simple"


def _run_child_agent_task(task_id: str, user_text: str, out_q: "mp.Queue[dict[str, str]]") -> None:
    """
    子进程执行函数：重建 LLM/Agent，执行任务并通过 Queue 回传结果。
    """
    try:
        llm = _build_llm()
        answer_chunks: list[str] = []
        plan_lines: list[str] = []
        for part in stream_security_agent_with_fallback(
            llm,
            {"messages": [{"role": "user", "content": user_text}]},
            stream_mode=["tasks", "updates", "messages"],
            version="v2",
        ):
            line = _plan_line_from_part(part)
            if line:
                plan_lines.append(line)
                snapshot = str(line).strip()
                if snapshot:
                    out_q.put({"task_id": task_id, "status": "update", "plan_text": snapshot})
                continue
            if part.get("type") != "messages":
                continue
            msg, _meta = part["data"]  # type: ignore[index]
            if isinstance(msg, AIMessageChunk):
                chunk = _extract_text_from_chunk_content(msg.content)
                if chunk:
                    answer_chunks.append(chunk)
        reply = "".join(answer_chunks).strip() or "子Agent未生成有效回复。"
        out_q.put({"task_id": task_id, "status": "success", "reply": reply})
    except Exception as e:  # noqa: BLE001
        out_q.put({"task_id": task_id, "status": "failed", "error": str(e)})


@dataclass
class RunningTask:
    task_id: str
    description: str
    started_at: float
    status: str
    # 子 Agent 最近一次 write_todos 的完整计划快照（每次更新整体替换）
    plan_snapshot: str
    message_id: str
    chat_id: Optional[str]
    process: "mp.Process"
    queue: "mp.Queue[dict[str, str]]"


class TaskRegistry:
    def __init__(self, on_finish: Callable[[str, str, str, Optional[str], str], None]) -> None:
        self._tasks: dict[str, RunningTask] = {}
        self._lock = threading.Lock()
        self._on_finish = on_finish
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def add(self, message_id: str, chat_id: Optional[str], user_text: str) -> str:
        task_id = uuid.uuid4().hex[:8]
        q: "mp.Queue[dict[str, str]]" = mp.Queue()
        p = mp.Process(target=_run_child_agent_task, args=(task_id, user_text, q), daemon=True)
        task = RunningTask(
            task_id=task_id,
            description=_short_desc(user_text),
            started_at=time.time(),
            status="running",
            plan_snapshot="",
            message_id=message_id,
            chat_id=chat_id,
            process=p,
            queue=q,
        )
        with self._lock:
            self._tasks[task_id] = task
        try:
            p.start()
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._tasks.pop(task_id, None)
            raise RuntimeError(f"spawn child agent failed: {e!s}") from e
        return task_id

    def list_lines(self) -> list[str]:
        now = time.time()
        with self._lock:
            items = list(self._tasks.values())
        if not items:
            return ["- （无运行中任务）"]
        lines: list[str] = []
        for t in items:
            elapsed = int(now - t.started_at)
            header = f"- {t.task_id} | {t.description} | {elapsed}s | {t.status}"
            lines.append(header)
            if t.plan_snapshot:
                for pl in t.plan_snapshot.splitlines():
                    pl = pl.strip()
                    if pl:
                        lines.append(f"  {pl}")
        return lines

    def _finalize(self, task: RunningTask, status: str, payload: str) -> None:
        with self._lock:
            self._tasks.pop(task.task_id, None)
        try:
            if task.process.is_alive():
                task.process.terminate()
        except Exception:
            pass
        self._on_finish(task.task_id, status, payload, task.chat_id, task.message_id)

    def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(1)
            now = time.time()
            with self._lock:
                tasks = list(self._tasks.values())
            for task in tasks:
                # 1) 强制超时 kill
                if now - task.started_at > TASK_TIMEOUT_SECONDS:
                    try:
                        if task.process.is_alive():
                            task.process.terminate()
                    except Exception:
                        pass
                    self._finalize(task, "failed", "任务执行超过30分钟，已强制终止。")
                    continue
                # 2) 子进程回传结果
                try:
                    msg = task.queue.get_nowait()
                except Exception:
                    msg = None
                if msg:
                    kind = msg.get("status")
                    if kind == "update":
                        snap = str(msg.get("plan_text") or "").strip()
                        if snap:
                            with self._lock:
                                cur = self._tasks.get(task.task_id)
                                if cur:
                                    cur.plan_snapshot = snap
                                    cur.status = "running"
                    elif kind == "success":
                        with self._lock:
                            cur = self._tasks.get(task.task_id)
                            if cur:
                                cur.status = "success"
                        self._finalize(task, "success", msg.get("reply") or "")
                    else:
                        with self._lock:
                            cur = self._tasks.get(task.task_id)
                            if cur:
                                cur.status = "failed"
                        self._finalize(task, "failed", msg.get("error") or "子Agent执行失败")
                    continue
                # 3) 子进程异常退出但未回传
                if not task.process.is_alive():
                    self._finalize(task, "failed", "子Agent异常退出（未返回结果）。")


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
    feishu = _build_feishu_client()

    def _send_with_mode(
        *,
        message_id: str,
        chat_id: Optional[str],
        out: str,
        send_mode: str,
        render_mode: str,
    ) -> None:
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
                if use_markdown:
                    feishu.reply_markdown(message_id=message_id, markdown=out)
                else:
                    feishu.reply_text(message_id=message_id, text=out)

    send_mode = (_env("FEISHU_SEND_MODE", "send") or "send").lower()
    render_mode = (_env("FEISHU_RENDER_MODE", "markdown") or "markdown").lower()

    def _notify_task_finish(task_id: str, status: str, payload: str, chat_id: Optional[str], message_id: str) -> None:
        if status == "success":
            reply = _format_markdown_for_feishu(payload or "任务完成，但无有效输出。")
            text = f"任务 {task_id} 已完成：\n\n{reply}"
        else:
            text = f"任务 {task_id} 执行失败：{payload}"
        _send_with_mode(
            message_id=message_id,
            chat_id=chat_id,
            out=text,
            send_mode=send_mode,
            render_mode=render_mode,
        )

    registry = TaskRegistry(on_finish=_notify_task_finish)

    def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
        print("[FeishuSocketBot] received im.message.receive_v1 event", flush=True)
        message_id, chat_id, text = _extract_context_from_event(data)
        print(f"[FeishuSocketBot] parsed message_id={message_id}, chat_id={chat_id}, text={text!r}", flush=True)
        if not message_id or not text:
            print("[FeishuSocketBot] no valid text to handle, skip.", flush=True)
            return

        def _send_text(out: str) -> None:
            _send_with_mode(
                message_id=message_id,
                chat_id=chat_id,
                out=out,
                send_mode=send_mode,
                render_mode=render_mode,
            )

        tstrip = text.strip()
        tlower = tstrip.lower()

        # /skills：列出已安装扩展 Skills
        if tlower == "/skills" or tlower.startswith("/skills "):
            _send_text(format_skills_list_markdown())
            return

        # /skill install <source>：支持 GitHub / SKILL.md 文件或链接
        m_install = re.match(r"^/skill\s+install\s+(.+?)\s*$", tstrip, flags=re.IGNORECASE)
        if m_install:
            source = m_install.group(1).strip()
            ok, msg = install_skill(source)
            _send_text(msg if ok else f"安装失败：{msg}")
            return

        if tlower == "/skill" or tlower.startswith("/skill "):
            _send_text(
                "Skill 命令：\n"
                "- `/skills` — 列出已安装技能（名称、版本、概述）\n"
                "- `/skill install <GitHub链接>` — 从 GitHub 导入 SKILL.md\n"
                "- `/skill install <SKILL.md路径或链接>` — 从本地/远程 SKILL.md 导入"
            )
            return

        # /task 命令：查看运行中任务列表
        if tlower.startswith("/task"):
            lines = registry.list_lines()
            _send_text("运行中任务列表：\n" + "\n".join(lines))
            return

        # 复杂任务：派生子 agent 执行，并先返回任务列表
        dispatch, reason = _should_dispatch_multi_agent(text)
        print(f"[FeishuSocketBot] dispatch_decision={dispatch}, reason={reason}, len={len(text)}", flush=True)
        if dispatch:
            try:
                task_id = registry.add(message_id=message_id, chat_id=chat_id, user_text=text)
            except Exception as e:  # noqa: BLE001
                _send_text(f"子Agent派发失败，改用主Agent处理：{e!s}")
                dispatch = False
            else:
                lines = registry.list_lines()
                _send_text(
                    "请求较复杂，已派生子Agent处理。\n"
                    f"原因: {reason}\n"
                    f"任务ID: {task_id}\n\n"
                    "运行中任务列表：\n"
                    + "\n".join(lines)
                )
                return

        # 飞书要求 3 秒内处理完，否则会重推；这里用后台线程异步回复，避免阻塞 ACK。
        def _work() -> None:
            try:
                print("[FeishuSocketBot] invoking agent...", flush=True)
                answer_chunks: list[str] = []
                plan_lines: list[str] = []
                plan_stream = (_env("FEISHU_PLAN_STREAM", "1") or "1").lower() not in ("0", "false", "no")

                if plan_stream:
                    _send_text("已收到请求，开始规划与执行。")

                for part in stream_security_agent_with_fallback(
                    llm,
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

