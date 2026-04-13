"""
AI Security Teams - LangChain Deep Agents skeleton package.
"""

from __future__ import annotations

__version__ = "0.4.5"

import warnings

# LangChain 仍会间接使用 pydantic.v1；在 Python 3.14+ 上 pydantic 会发出此 UserWarning。
# 在导入 langchain_core / deepagents 之前过滤，避免每次运行刷屏。
# 生产环境更稳妥的做法是使用 Python 3.11–3.13，直到上游完全弃用 Pydantic v1 路径。
warnings.filterwarnings(
    "ignore",
    message="Core Pydantic V1 functionality isn't compatible with Python 3.14 or greater.",
    category=UserWarning,
)
