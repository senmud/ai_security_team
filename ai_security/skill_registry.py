from __future__ import annotations

"""
已安装 Skills 的目录、安装（从 SKILL.md 来源导入）与运行时加载为 LangChain Tool。

目录约定（在 agent 工作区下）：
- `<workspace>/skills/installed/<skill_id>/SKILL.md`

环境变量 `AI_SECURITY_SKILLS_DIR` 可覆盖「已安装 skills」根目录（默认 `<workspace>/skills/installed`）。
"""

import os
import re
import shutil
import subprocess
import tempfile
import traceback
from collections.abc import Callable
from pathlib import Path
import httpx
from langchain_core.tools import BaseTool, tool

from .skills import get_agent_workspace_dir


def _workspace_root() -> Path:
    return get_agent_workspace_dir()


def get_installed_skills_root() -> Path:
    env = os.environ.get("AI_SECURITY_SKILLS_DIR")
    if env:
        return Path(env).expanduser().resolve()
    root = _workspace_root() / "skills" / "installed"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _extract_yaml_front_matter(skill_md: str) -> dict[str, str]:
    """
    提取 SKILL.md 顶部 `---` front matter 中的扁平键值（仅处理 `k: v`）。
    非严格 YAML 解析，足够覆盖常见 metadata 场景。
    """
    text = (skill_md or "").strip()
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    if not lines:
        return {}
    out: dict[str, str] = {}
    # 跳过第一行 `---`
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*:\s*(.+?)\s*$", line)
        if not m:
            continue
        k, v = m.groups()
        out[k.strip().lower()] = v.strip().strip("'\"")
    return out


def _extract_summary_from_skill_md(skill_md: str, fallback: str = "") -> str:
    """
    从 SKILL.md 提取功能概述：
    1) front matter: summary/description
    2) 首个 H1 标题后的第一段正文
    3) 文档第一段正文
    """
    fm = _extract_yaml_front_matter(skill_md)
    for key in ("summary", "description"):
        val = (fm.get(key) or "").strip()
        if val:
            return val[:220]

    lines = (skill_md or "").splitlines()
    h1_idx = -1
    for i, ln in enumerate(lines):
        if re.match(r"^\s*#\s+\S+", ln):
            h1_idx = i
            break
    search_from = h1_idx + 1 if h1_idx >= 0 else 0
    para: list[str] = []
    for ln in lines[search_from:]:
        s = ln.strip()
        if not s:
            if para:
                break
            continue
        if s.startswith("#"):
            if para:
                break
            continue
        para.append(s)
    if para:
        return " ".join(para)[:220]
    return (fallback or "").strip()[:220]


def _extract_version_from_skill_md(skill_md: str) -> str:
    """
    从 SKILL.md 提取版本号：
    1) front matter: version
    2) 文本中 `version: x.y.z` / `v1.2.3` / `版本：x.y.z`
    """
    fm = _extract_yaml_front_matter(skill_md)
    ver = (fm.get("version") or "").strip()
    if ver:
        return ver

    patterns = [
        r"(?im)^\s*version\s*[:=]\s*([0-9]+(?:\.[0-9]+){0,3}(?:[-+][A-Za-z0-9_.-]+)?)\s*$",
        r"(?i)\bv([0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9_.-]+)?)\b",
        r"(?im)^\s*版本\s*[：:]\s*([0-9]+(?:\.[0-9]+){0,3}(?:[-+][A-Za-z0-9_.-]+)?)\s*$",
    ]
    for pat in patterns:
        m = re.search(pat, skill_md or "")
        if m:
            return m.group(1).strip()
    return "unknown"


def list_installed_skills() -> list[dict[str, str]]:
    """
    返回已安装技能元数据列表：id、name、version、summary。
    /skills 展示从 `SKILL.md` 提取版本与功能概述。
    """
    root = get_installed_skills_root()
    out: list[dict[str, str]] = []
    if not root.is_dir():
        return out
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        sid = sub.name
        skill_md = ""
        md_path = sub / "SKILL.md"
        if md_path.is_file():
            try:
                skill_md = md_path.read_text(encoding="utf-8")
            except Exception:
                skill_md = ""
        name = _extract_title_from_skill_md(skill_md) if skill_md.strip() else ""
        if not name or name == "Imported Skill":
            name = sid
        version = _extract_version_from_skill_md(skill_md) if skill_md.strip() else "unknown"
        summary = _extract_summary_from_skill_md(skill_md, fallback="")
        out.append(
            {
                "id": sid,
                "name": name,
                "version": version,
                "summary": summary or "（未在 SKILL.md 中提取到功能概述）",
            }
        )
    return out


def _build_tool_from_skill_md(skill_id: str, skill_md: str) -> BaseTool:
    safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", skill_id)
    if not safe_name or safe_name[0].isdigit():
        safe_name = f"skill_{safe_name}"
    tool_name = f"skill_{safe_name}"

    @tool
    def _skill_tool(task: str) -> str:
        """执行该 Skill 的说明，并结合当前任务给出建议步骤。"""
        try:
            t = (task or "").strip()
            return "请按以下 Skill 指南执行。\n\n" + skill_md + "\n\n---\n" + f"当前任务: {t}"
        except Exception as e:  # noqa: BLE001
            return (
                f"[SkillError] skill=`{skill_id}` 执行失败：{e!s}\n\n"
                "请检查该 skill 的 SKILL.md 内容、脚本路径与命令格式。"
            )

    _skill_tool.name = tool_name
    return _skill_tool


def load_installed_skill_tools() -> list[BaseTool]:
    """扫描已安装目录，基于各技能 `SKILL.md` 动态生成工具列表。"""
    root = get_installed_skills_root()
    tools: list[BaseTool] = []
    if not root.is_dir():
        return tools
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        md_path = sub / "SKILL.md"
        if not md_path.is_file():
            continue
        try:
            skill_md = md_path.read_text(encoding="utf-8")
            if not skill_md.strip():
                continue
            tools.append(_build_tool_from_skill_md(sub.name, skill_md))
        except Exception as e:  # noqa: BLE001
            print(
                f"[SkillLoadError] skill_id={sub.name} path={md_path} error={e!s}\n"
                f"{traceback.format_exc()}",
                flush=True,
            )
            continue
    return tools


def _slugify_skill_id(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (value or "").strip().lower())
    s = s.strip("-_")
    return s or "imported_skill"


def _extract_title_from_skill_md(skill_md: str) -> str:
    for line in (skill_md or "").splitlines():
        t = line.strip()
        if t.startswith("#"):
            return t.lstrip("#").strip() or "Imported Skill"
    return "Imported Skill"


def _normalize_script_path(raw_path: str, *, skill_id: str) -> str:
    p = (raw_path or "").strip().strip("'\"")
    if not p:
        return p
    # 统一改写到 <skills_root>/<skill_id>/scripts/<filename>
    # 统一到 ./scripts/
    p = p.removeprefix("./")
    p = p.removeprefix("../")
    if p.startswith("scripts/"):
        p = p[len("scripts/") :]
    name = Path(p).name
    base = get_installed_skills_root() / skill_id / "scripts"
    if not name:
        return str(base)
    return str(base / name)


def _looks_like_path_token(token: str) -> bool:
    t = (token or "").strip().strip("'\"")
    if not t:
        return False
    if t.startswith(("./", "../", "/", "~/")):
        return True
    if "/" in t:
        return True
    if re.search(r"\.(?:py|sh|bash|zsh|js|mjs|cjs|ts|rb|pl)\b", t, flags=re.IGNORECASE):
        return True
    return False


def _rewrite_skill_md_script_paths(skill_md: str, *, skill_id: str) -> str:
    """
    将 SKILL.md 中**脚本文件路径**规范化到已安装目录下的 `scripts/`（绝对路径）。

    注意：**只改路径 token**，不在此函数中给 `uv` / `uv run` / `uv pip` 追加额外参数或
    把路径前缀误拼到 `uv` 可执行名上；已有 `uv run python …` 行仅通过兜底正则修正
    「`…/scripts/uv …`」类误替换。
    """
    text = skill_md or ""
    # 兼容有扩展名和无扩展名脚本（如 scripts/run），并覆盖 ~ / 绝对路径
    path_re = r"(?:~?/|/|(?:\./|\.\./))?[A-Za-z0-9_./-]+(?:\.[A-Za-z0-9_-]+)?"

    # 先重写 python/python3 为 uv run python，避免依赖当前 shell 的 venv 激活状态
    py_cmd_pat = re.compile(rf"(?P<prefix>\b(?:python|python3)\s+)(?P<path>{path_re})")
    text = py_cmd_pat.sub(
        lambda m: f"uv run python {_normalize_script_path(m.group('path'), skill_id=skill_id)}",
        text,
    )

    # pip/pip3 -> uv pip（仅替换命令前缀，保留参数）
    pip_cmd_line_pat = re.compile(r"(?m)^(\s*)(?:pip3?|python3?\s+-m\s+pip)\s+")
    text = pip_cmd_line_pat.sub(r"\1uv pip ", text)
    pip_inline_pat = re.compile(r"(?<!\S)(?:pip3?|python3?\s+-m\s+pip)\s+")
    text = pip_inline_pat.sub("uv pip ", text)

    # 其他脚本执行器仍保留原命令，但路径统一到 ./scripts/
    cmd_pat = re.compile(rf"(?P<prefix>\b(?:bash|sh|node|deno)\s+)(?P<path>{path_re})")
    text = cmd_pat.sub(
        lambda m: f"{m.group('prefix')}{_normalize_script_path(m.group('path'), skill_id=skill_id)}",
        text,
    )

    md_link_pat = re.compile(rf"\((?P<path>{path_re})\)")
    text = md_link_pat.sub(
        lambda m: f"({_normalize_script_path(m.group('path'), skill_id=skill_id)})"
        if _looks_like_path_token(m.group("path"))
        else m.group(0),
        text,
    )

    code_tick_pat = re.compile(rf"`(?P<path>{path_re})`")
    text = code_tick_pat.sub(
        lambda m: f"`{_normalize_script_path(m.group('path'), skill_id=skill_id)}`"
        if _looks_like_path_token(m.group("path"))
        else m.group(0),
        text,
    )
    # 兜底：避免把整条命令前缀误改成 `<...>/scripts/uv ...`
    text = re.sub(
        r"(?<!\S)(?:\./scripts/|[^\s`)]*/scripts/)(uv run(?:\s+--with-requirements\s+[^\s]+\s+)?python\s+)",
        r"\1",
        text,
    )
    text = re.sub(r"(?<!\S)(?:\./scripts/|[^\s`)]*/scripts/)(uv pip\s+)", r"\1", text)
    return text


def _copy_local_scripts_if_exists(source: str, dst_scripts_dir: Path) -> None:
    src = Path((source or "").strip()).expanduser()
    if not src.is_file():
        return
    src_scripts = src.parent / "scripts"
    if not src_scripts.is_dir():
        return
    for child in src_scripts.iterdir():
        target = dst_scripts_dir / child.name
        if child.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(child, target)
        elif child.is_file():
            shutil.copy2(child, target)


def _copy_requirements_if_exists(
    candidates: list[Path],
    *,
    dst_scripts_dir: Path,
) -> Path | None:
    for req in candidates:
        if not req.is_file():
            continue
        dst = dst_scripts_dir / "requirements.txt"
        shutil.copy2(req, dst)
        return dst
    return None


def _copy_local_requirements_if_exists(source: str, dst_scripts_dir: Path) -> Path | None:
    src = Path((source or "").strip()).expanduser()
    if not src.is_file():
        return None
    req_candidates = [
        src.parent / "requirements.txt",
        src.parent / "scripts" / "requirements.txt",
    ]
    return _copy_requirements_if_exists(req_candidates, dst_scripts_dir=dst_scripts_dir)


def _fetch_skill_md_from_github_url(url: str) -> tuple[bool, str]:
    raw = (url or "").strip()
    if not raw:
        return False, "空 URL"

    candidates: list[str] = []
    if "raw.githubusercontent.com" in raw:
        candidates.append(raw)
    elif "github.com" in raw:
        # 支持：
        # - https://github.com/<owner>/<repo>
        # - https://github.com/<owner>/<repo>/blob/<ref>/<path>
        m_blob = re.match(r"^https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$", raw)
        if m_blob:
            owner, repo, ref, path = m_blob.groups()
            candidates.append(f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}")
        m_repo = re.match(r"^https?://github\.com/([^/]+)/([^/]+)/?$", raw)
        if m_repo:
            owner, repo = m_repo.groups()
            candidates.extend(
                [
                    f"https://raw.githubusercontent.com/{owner}/{repo}/main/SKILL.md",
                    f"https://raw.githubusercontent.com/{owner}/{repo}/master/SKILL.md",
                ]
            )
    else:
        return False, "不是 GitHub URL"

    with httpx.Client(timeout=12.0, follow_redirects=True) as client:
        for u in candidates:
            try:
                r = client.get(u, headers={"User-Agent": "ai-security-team/skills-installer"})
            except Exception:
                continue
            if r.status_code == 200 and "text" in (r.headers.get("content-type") or "").lower():
                text = r.text
                if text.strip():
                    return True, text
    return False, "无法从 GitHub 下载 SKILL.md，请提供可访问的 SKILL.md 原始链接。"


def _read_skill_md_source(source: str) -> tuple[bool, str, str]:
    """
    读取 SKILL.md 来源。
    返回 (ok, content_or_error, source_kind)。
    """
    src = (source or "").strip()
    if not src:
        return False, "来源为空", ""

    if src.startswith("http://") or src.startswith("https://"):
        if "github.com" in src or "raw.githubusercontent.com" in src:
            ok, text_or_err = _fetch_skill_md_from_github_url(src)
            return ok, text_or_err, "github"
        try:
            r = httpx.get(src, timeout=12.0, follow_redirects=True)
            if r.status_code != 200:
                return False, f"下载失败，HTTP {r.status_code}", "url"
            text = r.text
            if not text.strip():
                return False, "下载内容为空", "url"
            return True, text, "url"
        except Exception as e:  # noqa: BLE001
            return False, f"下载失败: {e!s}", "url"

    p = Path(src).expanduser()
    if p.is_file():
        try:
            text = p.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            return False, f"读取本地文件失败: {e!s}", "file"
        if not text.strip():
            return False, "本地文件为空", "file"
        return True, text, "file"
    return False, "既不是 URL 也不是本地可读文件", ""


def _is_git_repo_source(source: str) -> bool:
    s = (source or "").strip()
    if not s:
        return False
    if s.startswith("git@"):
        return True
    if s.startswith(("http://", "https://")):
        # 明确的 SKILL.md 链接应走 md 读取逻辑
        if s.lower().endswith("/skill.md") or "raw.githubusercontent.com" in s or "/blob/" in s:
            return False
        # 常见 git 仓库 URL（GitHub/GitLab/Bitbucket 等）
        if s.endswith(".git"):
            return True
        if "/tree/" in s:
            return True
        if re.match(r"^https?://[^/]+/[^/]+/[^/]+/?$", s):
            return True
    return False


def _clone_git_repo_to_temp(source: str) -> tuple[bool, str]:
    workspace_tmp = _workspace_root() / "tmp"
    workspace_tmp.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="skill_clone_", dir=str(workspace_tmp)))
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", source, str(tmp_dir / "repo")],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False, f"clone 仓库失败: {e!s}"
    return True, str(tmp_dir / "repo")


def _locate_repo_skill_md(repo_dir: Path) -> Path | None:
    direct = repo_dir / "SKILL.md"
    if direct.is_file():
        return direct
    # 兜底：递归查找首个 SKILL.md
    for p in repo_dir.rglob("SKILL.md"):
        if p.is_file():
            return p
    return None


def _emit_validation_step(
    cb: Callable[[str], None] | None,
    text: str,
    *,
    max_len: int = 3800,
) -> None:
    """安装自检进度回调（如飞书逐条推送）；失败不影响安装主流程。"""
    if cb is None:
        return
    t = (text or "").strip()
    if len(t) > max_len:
        t = t[: max_len - 24].rstrip() + "\n…（内容过长已截断）"
    try:
        cb(t)
    except Exception:
        pass


def _run_subprocess(
    cmd: list[str],
    *,
    cwd: Path,
    timeout_sec: int = 180,
) -> tuple[bool, str]:
    try:
        cp = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"命令执行异常: {' '.join(cmd)}\n{e!s}"
    if cp.returncode != 0:
        out = (cp.stdout or "").strip()
        err = (cp.stderr or "").strip()
        return (
            False,
            f"命令失败({cp.returncode}): {' '.join(cmd)}\n"
            f"{('stdout:\n' + out + '\n') if out else ''}"
            f"{('stderr:\n' + err) if err else ''}",
        )
    return True, (cp.stdout or "").strip()


def _uv_run_python_cmd(*, scripts_dir: Path, python_argv: list[str]) -> list[str]:
    """
    构建 `uv run [ --with-requirements requirements.txt ] python <args…>`。
    当 `scripts/requirements.txt` 存在时附加 `--with-requirements`，与 SKILL.md 中改写后的命令一致。
    """
    cmd: list[str] = ["uv", "run"]
    req = scripts_dir / "requirements.txt"
    if req.is_file():
        cmd.extend(["--with-requirements", req.name])
    cmd.append("python")
    cmd.extend(python_argv)
    return cmd


def _validate_installed_skill_scripts(
    *,
    scripts_dir: Path,
    requirements_path: Path | None,
    on_validation_step: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    # 在 scripts 目录显式创建 .venv，避免 uv pip / uv run 落到不确定的全局或上级环境
    if not (scripts_dir / ".venv").is_dir():
        _emit_validation_step(
            on_validation_step,
            "【自检 1/4】虚拟环境：正在执行 `uv venv`…",
        )
        ok, msg = _run_subprocess(
            ["uv", "venv"],
            cwd=scripts_dir,
            timeout_sec=120,
        )
        venv_dir = scripts_dir / ".venv"
        if not ok or not venv_dir.is_dir():
            _emit_validation_step(
                on_validation_step,
                f"【自检 1/4】虚拟环境：失败（.venv 存在={venv_dir.is_dir()}）\n{msg}",
            )
            return False, f"创建虚拟环境失败（uv venv）"
        _emit_validation_step(
            on_validation_step,
            "【自检 1/4】虚拟环境：成功（已创建 `scripts/.venv`）",
        )
    else:
        _emit_validation_step(
            on_validation_step,
            "【自检 1/4】虚拟环境：跳过（`scripts/.venv` 已存在）",
        )

    if requirements_path and requirements_path.is_file():
        _emit_validation_step(
            on_validation_step,
            f"【自检 2/4】依赖：正在执行 `uv pip install -r {requirements_path.name}`…",
        )
        ok, msg = _run_subprocess(
            ["uv", "pip", "install", "-r", requirements_path.name],
            cwd=scripts_dir,
            timeout_sec=300,
        )
        if not ok:
            _emit_validation_step(
                on_validation_step,
                f"【自检 2/4】依赖：失败\n{msg}",
            )
            return False, f"安装依赖失败（requirements.txt）"
        _emit_validation_step(on_validation_step, "【自检 2/4】依赖：成功")
    else:
        _emit_validation_step(
            on_validation_step,
            "【自检 2/4】依赖：跳过（未复制到 `requirements.txt`）",
        )

    _emit_validation_step(
        on_validation_step,
        "【自检 3/4】语法：正在执行 `uv run [--with-requirements requirements.txt] python -m compileall`…",
    )
    ok, msg = _run_subprocess(
        _uv_run_python_cmd(scripts_dir=scripts_dir, python_argv=["-m", "compileall", "-q", "."]),
        cwd=scripts_dir,
        timeout_sec=180,
    )
    if not ok:
        _emit_validation_step(
            on_validation_step,
            f"【自检 3/4】语法：失败\n{msg}",
        )
        return False, f"脚本自检失败（uv run python -m compileall）"
    _emit_validation_step(on_validation_step, "【自检 3/4】语法：成功")

    # 真实导入自检：执行每个 .py 的顶层 import，可尽早暴露缺失依赖
    import_check_code = """
import importlib.util
import os
import pathlib
import sys
import traceback

root = pathlib.Path(".").resolve()
files = sorted([p for p in root.rglob("*.py") if p.is_file()])
failed = []
sys.path.insert(0, str(root))
os.environ["AI_SECURITY_SKILL_VALIDATE"] = "1"

for p in files:
    try:
        spec = importlib.util.spec_from_file_location(f"_skill_validate_{p.stem}", p)
        if not spec or not spec.loader:
            raise RuntimeError("cannot create module spec")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001
        failed.append((str(p), "".join(traceback.format_exception_only(type(e), e)).strip()))

if failed:
    details = "\\n".join([f"{a}: {b}" for a, b in failed])
    raise SystemExit("import-check failed:\\n" + details)

print(f"import-check ok: {len(files)} python files")
""".strip()

    _emit_validation_step(
        on_validation_step,
        "【自检 4/4】导入：正在执行顶层 import 检查…",
    )
    ok, msg = _run_subprocess(
        _uv_run_python_cmd(scripts_dir=scripts_dir, python_argv=["-c", import_check_code]),
        cwd=scripts_dir,
        timeout_sec=240,
    )
    if not ok:
        _emit_validation_step(
            on_validation_step,
            f"【自检 4/4】导入：失败\n{msg}",
        )
        return False, f"脚本导入自检失败（uv run python import-check）"
    _emit_validation_step(
        on_validation_step,
        f"【自检 4/4】导入：成功\n{(msg or '').strip() or '（无额外输出）'}",
    )
    return True, "ok"


def materialize_skill_from_markdown(
    skill_md: str,
    *,
    skill_id: str,
    source: str = "",
    on_validation_step: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """
    将 SKILL.md 内容写入已安装目录。
    skill_id 需已规范化（见 _slugify_skill_id）。
    """
    skill_id = _slugify_skill_id(skill_id)
    if not skill_id:
        return False, "无效的技能 ID"

    dst = get_installed_skills_root() / skill_id
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)

    scripts_dir = dst / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    _copy_local_scripts_if_exists(source, scripts_dir)
    copied_requirements = _copy_local_requirements_if_exists(source, scripts_dir)

    rewritten_md = _rewrite_skill_md_script_paths(skill_md, skill_id=skill_id)
    (dst / "SKILL.md").write_text(rewritten_md, encoding="utf-8")
    ok_validate, validate_msg = _validate_installed_skill_scripts(
        scripts_dir=scripts_dir,
        requirements_path=copied_requirements,
        on_validation_step=on_validation_step,
    )
    if not ok_validate:
        shutil.rmtree(dst, ignore_errors=True)
        return False, f"安装技能失败 `{skill_id}`"
    return True, skill_id


def install_skill_from_skill_md(
    source: str,
    *,
    on_validation_step: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """
    从 GitHub 或任意 SKILL.md（URL/本地文件）安装技能。
    安装时会写入 SKILL.md，并在运行时由加载器基于 SKILL.md 动态生成工具。
    """
    ok, content_or_err, source_kind = _read_skill_md_source(source)
    if not ok:
        return False, content_or_err

    skill_md = content_or_err
    src_tail = Path((source or "").strip()).name or "skill"
    skill_id = _slugify_skill_id(f"imported-{src_tail}")
    ok2, sid_or_err = materialize_skill_from_markdown(
        skill_md,
        skill_id=skill_id,
        source=source,
        on_validation_step=on_validation_step,
    )
    if not ok2:
        return False, sid_or_err
    return True, f"已安装技能 `{sid_or_err}`（来源: {source_kind}）。"


def install_skill_from_git_repo(
    source: str,
    *,
    on_validation_step: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """
    从 git 仓库安装技能：
    1) 在 agent_workspace 下临时目录 clone 仓库
    2) 复制 SKILL.md 与 scripts 到安装目录
    3) 重写 SKILL.md 命令/路径
    4) 删除临时目录
    """
    ok, repo_or_err = _clone_git_repo_to_temp(source)
    if not ok:
        return False, repo_or_err

    repo_dir = Path(repo_or_err)
    tmp_root = repo_dir.parent
    try:
        md_path = _locate_repo_skill_md(repo_dir)
        if not md_path or not md_path.is_file():
            return False, "仓库中未找到 SKILL.md。"
        skill_md = md_path.read_text(encoding="utf-8")
        if not skill_md.strip():
            return False, "仓库中的 SKILL.md 为空。"

        repo_name = repo_dir.name
        if repo_name == "repo":
            # clone 到固定子目录时，取 URL 最后一段作为 repo 名
            tail = Path((source or "").rstrip("/")).name
            repo_name = tail.replace(".git", "") or "repo"
        skill_id = _slugify_skill_id(f"repo-{repo_name}")
        dst = get_installed_skills_root() / skill_id
        if dst.exists():
            shutil.rmtree(dst)
        dst.mkdir(parents=True, exist_ok=True)

        scripts_dst = dst / "scripts"
        scripts_dst.mkdir(parents=True, exist_ok=True)

        # 优先复制 SKILL.md 同级 scripts，其次仓库根 scripts
        candidate_scripts = [md_path.parent / "scripts", repo_dir / "scripts"]
        copied = False
        for src_scripts in candidate_scripts:
            if src_scripts.is_dir():
                for child in src_scripts.iterdir():
                    target = scripts_dst / child.name
                    if child.is_dir():
                        if target.exists():
                            shutil.rmtree(target)
                        shutil.copytree(child, target)
                    elif child.is_file():
                        shutil.copy2(child, target)
                copied = True
                break
        if not copied:
            # 没有 scripts 目录也允许安装，保持空目录
            pass

        requirements_src_candidates = [
            md_path.parent / "requirements.txt",
            md_path.parent / "scripts" / "requirements.txt",
            repo_dir / "scripts" / "requirements.txt",
            repo_dir / "requirements.txt",
        ]
        copied_requirements = _copy_requirements_if_exists(
            requirements_src_candidates,
            dst_scripts_dir=scripts_dst,
        )

        rewritten_md = _rewrite_skill_md_script_paths(skill_md, skill_id=skill_id)
        (dst / "SKILL.md").write_text(rewritten_md, encoding="utf-8")

        ok_validate, validate_msg = _validate_installed_skill_scripts(
            scripts_dir=scripts_dst,
            requirements_path=copied_requirements,
            on_validation_step=on_validation_step,
        )
        if not ok_validate:
            shutil.rmtree(dst, ignore_errors=True)
            return False, f"安装技能失败 `{skill_id}`：\n{validate_msg}"

        return True, f"已安装技能 `{skill_id}`（来源: git 仓库，依赖安装与脚本自检通过）。"
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def install_skill_from_clawhub_slug(
    slug: str,
    *,
    on_validation_step: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """从 ClawHub（经 Layer API）拉取 SKILL.md 并安装为本地扩展 Skill。"""
    from .clawhub_client import fetch_skill_markdown_from_clawhub

    slug = (slug or "").strip()
    if not slug or not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$", slug):
        return False, "无效的 skill slug。"

    ok, md_or_err = fetch_skill_markdown_from_clawhub(slug)
    if not ok:
        return False, md_or_err

    skill_md = md_or_err
    skill_id = _slugify_skill_id(f"clawhub-{slug}")
    ok2, sid_or_err = materialize_skill_from_markdown(
        skill_md,
        skill_id=skill_id,
        source="",
        on_validation_step=on_validation_step,
    )
    if not ok2:
        return False, sid_or_err
    return True, f"已从 ClawHub 安装技能 `{sid_or_err}`（slug: `{slug}`）。**当前会话**需重新创建 Agent 后新工具才会加载；飞书机器人每条消息会重建 Agent，可直接生效。"


def install_skill(
    skill_source: str,
    *,
    on_validation_step: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """
    统一安装入口：
    - 按 GitHub / SKILL.md 来源安装。
    """
    src = (skill_source or "").strip()
    if _is_git_repo_source(src):
        return install_skill_from_git_repo(src, on_validation_step=on_validation_step)
    return install_skill_from_skill_md(src, on_validation_step=on_validation_step)


def format_skills_list_markdown() -> str:
    """供 /skills 命令展示的 Markdown 文本。"""
    items = list_installed_skills()
    if not items:
        return (
            "当前未安装任何扩展 Skill。\n\n"
            "使用 `/skill install <GitHub链接|SKILL.md路径|SKILL.md链接>` 导入。"
        )
    lines = ["## 已安装 Skills", "", f"共 {len(items)} 个", ""]
    for it in items:
        lines.append(f"### {it['name']}")
        lines.append(f"- ID：`{it['id']}`")
        lines.append(f"- 版本：`{it['version']}`")
        lines.append(f"- 功能概述：{it['summary']}")
        lines.append("")
    return "\n".join(lines).rstrip()
