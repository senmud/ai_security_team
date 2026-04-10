# AI Security Teams – LangChain Deep Agents 实现骨架

**版本：0.4.0**（与 `ai_security.__version__` 同步）

本仓库基于文档 `AI_Security_Teams_Architecture_and_Benchmarking.md` 与 `AI_Security_Teams_System_Architecture.md`，使用 **LangChain AI 官方 [`deepagents`](https://pypi.org/project/deepagents/) 包**（`create_deep_agent`）作为核心 harness，承载安全运营场景中的工具调用与多步推理；`ai_security/agents.py` 对其做了安全领域封装。

目前代码只实现了**可运行的最小骨架**，方便后续逐步扩展到生产级能力。

## 快速开始

**Python 版本**：LangChain 依赖链仍会加载 `pydantic.v1`；在 **Python 3.14+** 上 Pydantic 会提示「Core Pydantic V1…」的 `UserWarning`。本仓库在 `ai_security/__init__.py` 里对该条已知警告做了过滤；若希望从根源上规避，请使用 **Python 3.11–3.13** 创建虚拟环境。

建议使用虚拟环境（避免系统 Python 的 PEP 668 限制），并让编辑器使用该环境的解释器，这样 `langchain_openai` 等依赖可被正确解析：

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -r requirements.txt

export OPENAI_API_KEY=...  # 或配置你自己的 LLM 提供方
export OPENAI_BASE_URL=... # 可选：OpenAI 兼容网关地址
export OPENAI_MODEL=...    # 可选：覆盖默认模型

# 可选：本机工作区 + 内置 execute（shell）；设为 0 则仅用内存态 StateBackend（无真实磁盘与 shell）
export AI_SECURITY_LOCAL_SHELL=1
export AI_SECURITY_AGENT_WORKSPACE=/path/to/workspace   # 可选，默认 ./agent_workspace

python -m ai_security.demo_run
```

在 VS Code / Cursor 中：**Python: Select Interpreter** → 选择项目下的 `.venv/bin/python`。

## Deep Agents 技能配置

| 能力 | 实现方式 |
| :--- | :--- |
| **读写文件 / 目录检索** | `deepagents` 内置 `ls`、`read_file`、`write_file`、`edit_file`、`glob`、`grep`；默认通过 **`LocalShellBackend`** 映射到 `agent_workspace/`（`virtual_mode=True`）。 |
| **Shell 执行** | 内置工具 **`execute`**；需 Backend 实现 `SandboxBackendProtocol`，`LocalShellBackend` 会在本机用户权限下执行命令。**仅限可信环境**；生产请换隔离沙箱后端。 |
| **Web 搜索** | 扩展工具 **`web_search`**（`ddgs`），已并入 `create_security_deep_agent` 的 `tools`。 |
| **可安装扩展 Skills** | 见 `ai_security/skill_registry.py`：已安装技能放在 `<agent_workspace>/skills/installed/<skill_id>/`（可用环境变量 **`AI_SECURITY_SKILLS_DIR`** 覆盖根目录），通过 `manifest.json` + `tool.py`（导出 `get_tools()`）在运行时加载为 LangChain Tool，并与默认工具合并。内置示例目录为 `ai_security/skill_catalog/hello_echo`。 |

`demo_run` 会构建 **Deep Agent**（`CompiledStateGraph`），合并上述内置能力、`ai_security/skills.py` 中的扩展工具，以及**当前已安装的扩展 Skills**。**输出为流式**：`ChatOpenAI(streaming=True)` + `graph.stream(..., stream_mode="messages", version="v2")`。

更多架构细节请参考：

- `AI_Security_Teams_Architecture_and_Benchmarking.md`
- `AI_Security_Teams_System_Architecture.md`

## 飞书机器人接入（Socket Mode 长连接）

不使用 HTTP 回调，本仓库提供一个 Socket Mode（WebSocket 长连接）机器人进程：`ai_security/feishu_socket_bot.py`。

### 需要的飞书配置

- **应用类型**：企业自建应用（机器人）
- **事件订阅方式**：选择 **使用长连接接收事件（Socket Mode）**
- **订阅事件**：至少订阅接收消息事件（`im.message.receive_v1`）

### 环境变量

- **OpenAI/兼容网关**
  - `OPENAI_API_KEY`（必填）
  - `OPENAI_BASE_URL`（可选）
  - `OPENAI_MODEL`（可选）
- **飞书应用凭证（用于长连接与回消息）**
  - `FEISHU_APP_ID`（必填）
  - `FEISHU_APP_SECRET`（必填）
  - `FEISHU_BASE_URL`（可选，默认 `https://open.feishu.cn`）

### 启动（本地/服务器）

```bash
source .venv/bin/activate
pip install -r requirements.txt

python -m ai_security.feishu_socket_bot
```

> 说明：飞书长连接模式要求收到事件后 **3 秒内处理完成**，否则会重推。当前实现用后台线程执行 LLM 并回复，以避免阻塞 ACK；生产环境建议加队列与限流。

### 多 Agent 机制（子进程派发）

`ai_security/feishu_socket_bot.py` 内置一个轻量的多 agent 机制，用于处理**复杂/耗时/不好评估**的请求：

- **复杂度预判**：收到用户消息后，会根据文本长度、是否多段/多问句、是否包含 URL 以及关键词（如“分析/排查/设计/方案/评估”等）粗略判断任务复杂度。
  - 复杂或不确定任务：派生子 agent 执行（子进程）。
  - 简单任务：沿用主 agent 的同步流式执行路径。
- **通信通道**：主进程与子进程通过队列回传结果，子任务成功/失败都会回传，主 agent 再通过飞书回复用户。
- **任务列表**：主 agent 维护一个运行中任务列表，包含：
  - 任务 ID
  - 描述（10 字以内）
  - 已运行时间（秒）
- **子 agent 计划与状态同步**：子 agent 在执行过程中会把 `write_todos` 渲染出的**整段计划快照**通过“update”消息同步给主 agent；**每次更新用新快照整体替换旧快照**（不再按条目 key 合并去重）。
  - 主 agent 在任务项下方展示当前快照中的计划行（如“进行中/已完成/失败”等）。
- **超时策略**：任务超过 **10 分钟**会被强制终止（kill），并按失败处理。
- **即时反馈**：派生子 agent 后，机器人会立刻回复“正在处理”并展示当前任务列表。
- **主 agent 每次请求重建**：非多 agent 路径下，每次用户消息会重新构建 Deep Agent，以便**新安装的扩展 Skills**立即生效。

可选控制项：

- `FEISHU_FORCE_MULTI_AGENT=1`：强制所有请求走多 agent 路径（便于联调/压测）。

### 飞书命令：`/task`、`/skills`、`/skill`

**`/task`** — 查看运行中任务列表（任务 ID、描述、已运行秒数，以及子 agent 当前的计划快照）。

**`/skills`** — 列出已安装的扩展 Skills：**名称、版本、功能概述**（来自各技能目录下的 `manifest.json`）。

**`/skill install <技能ID>`** — 从仓库内置的 `ai_security/skill_catalog/<技能ID>/` 复制到已安装目录（例如先安装示例：`/skill install hello_echo`）。

**`/skill`** — 简要说明上述命令。


