from __future__ import annotations

"""
轻量版 Harness 占位实现：
- PromptImmunityService: 对 LLM 调用做基本的 prompt 过滤与审计 hook
- ArkclawIdentityService: 为 Agent 上下文附加 identity / policy

这里重点是与 LangChain / LangGraph 的集成形态，而不是安全策略细节。
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

from langchain_core.language_models import BaseLanguageModel
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import Runnable, RunnableLambda


@dataclass
class AgentIdentity:
    name: str
    role: str
    permissions: Dict[str, Any]


class PromptImmunityService:
    """包装 LLM，作为所有 Agent 的统一入口。"""

    def __init__(self, llm: BaseLanguageModel):
        self._llm = llm

    def _sanitize(self, message: str) -> str:
        # TODO: 在这里挂接真正的提示词免疫 / 敏感信息检测策略
        return message

    def as_runnable(self) -> Runnable:
        def _call(messages: list[BaseMessage]) -> str:
            sanitized = []
            for m in messages:
                if isinstance(m, HumanMessage):
                    sanitized.append(HumanMessage(content=self._sanitize(m.content)))
                else:
                    sanitized.append(m)
            result = self._llm.invoke(sanitized)
            return result.content

        return RunnableLambda(_call)


class ArkclawIdentityService:
    """为每次 Deep Agent 运行附加一个轻量的身份 / policy 上下文。"""

    def issue_identity(self, agent_name: str, role: str, *, permissions: Optional[Dict[str, Any]] = None) -> AgentIdentity:
        return AgentIdentity(
            name=agent_name,
            role=role,
            permissions=permissions or {},
        )

