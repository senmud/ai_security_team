from __future__ import annotations

from langchain_core.tools import tool


@tool
def echo_sample(text: str) -> str:
    """示例技能：回显用户输入，用于验证扩展 Skill 安装与调用是否正常。"""
    return f"[hello_echo] {text}"


def get_tools():
    return [echo_sample]
