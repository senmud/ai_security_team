from __future__ import annotations

"""
AI Security Teams 核心编排：使用 LangChain AI 官方 `deepagents` 包中的 `create_deep_agent`。

能力说明（与需求文档对齐的「技能」）：
- **文件读写 / 检索**：由 deepagents 内置工具提供（ls、read_file、write_file、edit_file、glob、grep），
  需配置 **LocalShellBackend**（或 FilesystemBackend）将虚拟路径映射到本机目录；默认使用项目下 `agent_workspace/`。
- **Shell 执行**：内置工具 **execute**，仅在 Backend 实现 `SandboxBackendProtocol` 时可用；`LocalShellBackend` 提供本机 shell（高风险，仅限可信环境）。
- **公网搜索**：扩展工具 **web_search**（DuckDuckGo，见 `skills.py`）。
- **ClawHub**：扩展工具 **clawhub_search_skills** / **clawhub_install_skill**（见 `clawhub_client.py`）；**排在最后**：先匹配已安装技能，再用内置文件/shell 与 `web_search`，仍不足时再检索集市。

参见：https://docs.langchain.com/oss/python/deepagents
"""

from pathlib import Path
from typing import List

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import BackendFactory, BackendProtocol
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool, tool
from langgraph.graph.state import CompiledStateGraph

from .skills import clawhub_install_skill, clawhub_search_skills, get_agent_workspace_dir, web_search


def _installed_skill_tools() -> List[BaseTool]:
    try:
        from .skill_registry import load_installed_skill_tools

        return load_installed_skill_tools()
    except Exception:
        return []

SECURITY_DEEP_SYSTEM_PROMPT = """你是企业安全运营「AI Security Teams」中的 Deep Agent 协调者。

## 你已具备的能力

1. **Deep Agents 内置（无需重复实现）**
   - 任务规划：`write_todos` 分解与跟踪步骤。
   - **文件**：`ls`、`read_file`、`write_file`、`edit_file`、`glob`、`grep`（路径相对于当前配置的工作区根目录；勿访问工作区外的敏感路径）。
   - **Shell**：`execute` 在本地执行命令（可配合 `read_file` 处理输出文件）；**仅在明确需要时使用**，并注意命令安全性。

2. **本场景扩展工具**（下列第 1–3 点的**使用顺序**见下方「工具与技能使用优先级」）
   - **已安装的扩展 Skills**：以独立工具出现在列表中（来自各技能目录的 `manifest.json` / `tool.py`）。
   - `threat_feed_connector` / `log_analyzer` / `deep_entity_trace`：安全运营占位接口（可对接 SIEM/情报/图谱）。
   - `web_search`：需要**最新公开信息**时（CVE、厂商通告、漏洞别名等）使用。
   - `clawhub_search_skills` / `clawhub_install_skill`：在 **ClawHub** 集市检索并安装公开 skill（安装后下一轮 Agent 生命周期才会出现对应工具）；**仅在前两步仍不足时使用**。

## 工具与技能使用优先级（必须遵守）

1. **先核对已安装的扩展 Skills**：判断任务是否与某个已安装技能工具的描述相符；若相符，**优先调用该技能工具**处理对应部分，不要跳过直接去搜网页或 ClawHub。
2. **再用内置文件 / Shell 与 `web_search`**：若无匹配技能、或技能不足以覆盖全部需求，再综合使用 **文件类工具**（`ls`、`read_file`、`write_file`、`edit_file`、`glob`、`grep`）、**`execute`（shell）** 与 **`web_search`**；需要事实与资讯时多用 `web_search`，需要本地读写与命令时用文件工具与 `execute`（`execute` 仅在确有需求时使用并注意安全性）。
3. **最后才看 ClawHub**：仅当你判断 **已安装技能 + 上述内置能力与 `web_search` 仍无法**满足所需能力或缺少可执行流程时，才调用 `clawhub_search_skills`；可选 `clawhub_install_skill` 安装后再继续。

## 工作要求

1. **强制先规划**：在任何非闲聊任务中，第一步必须调用 `write_todos`，生成至少 3 条具体计划（可执行、可验证）。
2. **执行中持续更新计划**：每完成一个关键步骤、发生失败或策略变更时，必须再次调用 `write_todos` 更新状态（pending / in_progress / completed / failed）。
3. 理解用户描述的安全事件或研究任务；**遵循上方优先级**：先已装技能，再文件/`execute`/`web_search`，最后 ClawHub。
4. 输出简洁、可执行（风险判断、下一步动作、需人工确认点）。
5. 回答使用中文。"""


# === 占位安全工具（可替换为真实连接器）===


@tool
def threat_feed_connector(query: str) -> str:
    """从威胁情报源中检索与 query 相关的 IOC/TTP（占位实现）。"""
    return f"[ThreatIntel] mock result for {query}"


@tool
def log_analyzer(incident_hint: str) -> str:
    """对日志做初步分析并输出可疑事件摘要（占位实现）。"""
    return f"[Triage] suspicious activity around {incident_hint}"


@tool
def deep_entity_trace(seed: str) -> str:
    """模拟 Deep Agent 的跨实体溯源（占位实现）。"""
    return f"[DeepTrace] expanded graph around {seed}"


def default_security_tools() -> List[BaseTool]:
    """自定义工具列表（会与 deepagents 内置工具合并）。"""
    return [
        *_installed_skill_tools(),
        threat_feed_connector,
        log_analyzer,
        deep_entity_trace,
        web_search,
        clawhub_search_skills,
        clawhub_install_skill,
    ]


def build_local_workspace_backend(
    workspace_dir: str | Path | None = None,
    *,
    inherit_env: bool = True,
    execute_timeout_sec: int = 120,
) -> LocalShellBackend:
    """
    为本机构建 **磁盘文件 + execute(shell)** 后端。

    - `virtual_mode=True`：内置文件类工具的路径锚定在工作区根目录内（见 deepagents 文档；**不**等同于沙箱）。
    - `execute` 仍在本机用户权限下运行，请勿对不可信输入开放。
    """
    root = Path(workspace_dir).resolve() if workspace_dir else get_agent_workspace_dir()
    root.mkdir(parents=True, exist_ok=True)
    return LocalShellBackend(
        root_dir=str(root),
        virtual_mode=True,
        inherit_env=inherit_env,
        timeout=execute_timeout_sec,
    )


def create_security_deep_agent(
    model: BaseChatModel,
    *,
    tools: List[BaseTool] | None = None,
    system_prompt: str | None = None,
    backend: BackendProtocol | BackendFactory | None = None,
    use_local_workspace_and_shell: bool = True,
) -> CompiledStateGraph:
    """
    使用 `deepagents.create_deep_agent` 构建安全场景 Deep Agent。

    - `use_local_workspace_and_shell=True`（默认）：挂载 `LocalShellBackend`，启用真实目录下的读写与 `execute`。
    - `False`：使用 deepagents 默认 `StateBackend`（进程内虚拟文件，`execute` 不可用）。
    - 也可显式传入 `backend` 覆盖上述行为。
    """
    resolved_backend: BackendProtocol | BackendFactory | None
    if backend is not None:
        resolved_backend = backend
    elif use_local_workspace_and_shell:
        resolved_backend = build_local_workspace_backend()
    else:
        resolved_backend = None

    return create_deep_agent(
        model=model,
        tools=tools or default_security_tools(),
        system_prompt=system_prompt or SECURITY_DEEP_SYSTEM_PROMPT,
        backend=resolved_backend,
    )
