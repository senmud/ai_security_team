from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import json


@dataclass
class FeishuCredentials:
    app_id: str
    app_secret: str
    base_url: str = "https://open.feishu.cn"


class FeishuAuthError(RuntimeError):
    pass


class FeishuClient:
    """
    极简 Feishu / Lark API 客户端：
    - 获取 tenant_access_token
    - 回复消息（reply）到原消息线程
    - 发送消息到 chat（create）
    """

    def __init__(self, creds: FeishuCredentials, *, timeout_sec: float = 10.0) -> None:
        self.creds = creds
        self._timeout = timeout_sec
        self._token: Optional[str] = None
        self._token_expire_at: float = 0.0

    def _now(self) -> float:
        return time.time()

    def _token_valid(self) -> bool:
        return bool(self._token) and self._now() < (self._token_expire_at - 30)

    def get_tenant_access_token(self) -> str:
        if self._token_valid():
            assert self._token is not None
            return self._token

        print("[FeishuClient] fetching tenant_access_token...", flush=True)
        url = f"{self.creds.base_url}/open-apis/auth/v3/tenant_access_token/internal"
        payload = {"app_id": self.creds.app_id, "app_secret": self.creds.app_secret}
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()

        if data.get("code") != 0:
            raise FeishuAuthError(f"Get tenant_access_token failed: {data}")

        token = data["tenant_access_token"]
        expire = int(data.get("expire", 3600))
        self._token = token
        self._token_expire_at = self._now() + expire
        return token

    def reply_text(self, message_id: str, text: str) -> dict[str, Any]:
        """
        回复文本消息到指定 message_id。
        API: POST /open-apis/im/v1/messages/{message_id}/reply
        """
        url = f"{self.creds.base_url}/open-apis/im/v1/messages/{message_id}/reply"
        token = self.get_tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        # 飞书 IM API 的 content 字段通常要求为 JSON 字符串（不是对象）
        payload = {"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}
        print(f"[FeishuClient] reply_text: message_id={message_id}, text_preview={text[:80]!r}", flush=True)
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(url, json=payload, headers=headers)
            if r.status_code >= 400:
                print(
                    f"[FeishuClient] reply HTTP {r.status_code}, body={r.text[:500]!r}",
                    flush=True,
                )
                r.raise_for_status()
            data = r.json()
            print(f"[FeishuClient] reply result code={data.get('code')}, msg={data.get('msg')!r}", flush=True)
            return data

    def send_text_chat(self, chat_id: str, text: str) -> dict[str, Any]:
        """
        直接向会话 chat_id 发送文本消息（更“显眼”，不依赖线程视图）。
        API: POST /open-apis/im/v1/messages?receive_id_type=chat_id
        """
        url = f"{self.creds.base_url}/open-apis/im/v1/messages"
        token = self.get_tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        params = {"receive_id_type": "chat_id"}
        print(f"[FeishuClient] send_text_chat: chat_id={chat_id}, text_preview={text[:80]!r}", flush=True)
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(url, params=params, json=payload, headers=headers)
            if r.status_code >= 400:
                print(f"[FeishuClient] send HTTP {r.status_code}, body={r.text[:500]!r}", flush=True)
                r.raise_for_status()
            data = r.json()
            print(f"[FeishuClient] send result code={data.get('code')}, msg={data.get('msg')!r}", flush=True)
            return data

    def reply_markdown(self, message_id: str, markdown: str) -> dict[str, Any]:
        """
        使用 interactive + lark_md 回复消息（支持 Markdown 渲染）。
        API: POST /open-apis/im/v1/messages/{message_id}/reply
        """
        url = f"{self.creds.base_url}/open-apis/im/v1/messages/{message_id}/reply"
        token = self.get_tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        md = _normalize_lark_md(markdown)
        card = {
            "config": {"wide_screen_mode": True},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": md}},
            ],
        }
        payload = {"msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)}
        print(f"[FeishuClient] reply_markdown: message_id={message_id}, md_preview={md[:80]!r}", flush=True)
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(url, json=payload, headers=headers)
            if r.status_code >= 400:
                print(f"[FeishuClient] reply_markdown HTTP {r.status_code}, body={r.text[:500]!r}", flush=True)
                r.raise_for_status()
            data = r.json()
            print(f"[FeishuClient] reply_markdown code={data.get('code')}, msg={data.get('msg')!r}", flush=True)
            return data

    def send_markdown_chat(self, chat_id: str, markdown: str) -> dict[str, Any]:
        """
        使用 interactive + lark_md 直接向 chat 发送消息（支持 Markdown 渲染）。
        API: POST /open-apis/im/v1/messages?receive_id_type=chat_id
        """
        url = f"{self.creds.base_url}/open-apis/im/v1/messages"
        token = self.get_tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        md = _normalize_lark_md(markdown)
        card = {
            "config": {"wide_screen_mode": True},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": md}},
            ],
        }
        payload = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        params = {"receive_id_type": "chat_id"}
        print(f"[FeishuClient] send_markdown_chat: chat_id={chat_id}, md_preview={md[:80]!r}", flush=True)
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(url, params=params, json=payload, headers=headers)
            if r.status_code >= 400:
                print(f"[FeishuClient] send_markdown HTTP {r.status_code}, body={r.text[:500]!r}", flush=True)
                r.raise_for_status()
            data = r.json()
            print(f"[FeishuClient] send_markdown code={data.get('code')}, msg={data.get('msg')!r}", flush=True)
            return data


def _normalize_lark_md(markdown: str) -> str:
    """
    尽量产出稳定可渲染的 lark_md 文本：
    - 统一换行
    - 折叠过多空行
    """
    text = (markdown or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text

