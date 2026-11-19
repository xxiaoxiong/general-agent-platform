"""
Skill 注册与装载（DeepAgents 原生 Skills 机制，P2-05）。

Skill = 目录 + SKILL.md（YAML frontmatter: name/description/version）
+ 附属文件（scripts/、references/ 等任意相对路径文件，标准 Agent Skills 形态）。
StateBackend 下 skill 文件以 files 注入；FilesystemBackend 下由 SkillsMiddleware
从 backend 读取（source 路径 /skills/<name>/），渐进式加载。

Registry（§4.6）：
- 来源范围：内置（builtin）/ 用户（user）/ Agent（agent）/ 组织（org），
  外部目录经 AGENT_SKILLS_DIR 扫描（按目录归属判定 scope）；
- 元数据：版本、启用状态、来源、范围、审计字段（to_audit_fields）；
- 安全边界：Skill 只是文件内容（指令 + 附属脚本/引用，prompt/文件注入面），
  **不得自行提升工具权限**——工具面始终由 Capability Registry / ToolPolicy
  决定，Skill 内容无法授权工具；脚本执行能力由 execute/python_exec 的
  fail-closed 策略独立控制。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from app.core.config import settings

logger = logging.getLogger(__name__)

# ── 附属文件注入上限（模块级常量：控制进入运行态 checkpoint 的体积） ──────────
MAX_SKILL_FILE_BYTES = 256 * 1024       # 单附属文件（注入内容编码后）
MAX_SKILL_TOTAL_BYTES = 1024 * 1024     # 单技能附属文件总量
MAX_SKILL_FILE_COUNT = 50               # 单技能附属文件数量
MAX_SKILL_RELPATH_LEN = 200             # 附属文件相对路径长度

# 目录扫描跳过的目录/文件名（隐藏文件与字节码不注入）
_SKIP_DIRS = {"__pycache__", ".git", "node_modules"}
# 首尾为字母/数字，中间允许下划线/连字符（Agent Skills 规范口径）
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?$")


def validate_skill_name(name: str) -> str:
    """校验技能名（防 /skills/ 注入路径逃逸）；非法抛 ValueError。

    仅允许字母/数字/下划线/连字符（1-64 位）；只在管理面写入时校验，
    存量/目录技能名不追溯。
    """
    name = (name or "").strip()
    if not _SKILL_NAME_RE.match(name):
        raise ValueError("技能名仅允许字母/数字/下划线/连字符（1-64 位）")
    return name


def validate_skill_relpath(path: str) -> str:
    """校验技能附属文件相对路径（POSIX），返回规范化路径；非法抛 ValueError。

    拒绝：空路径、绝对路径/盘符、反斜杠、`.`/`..`/空路径段、控制字符、
    超长路径、`SKILL.md`（由技能正文 body 独占）。
    """
    p = (path or "").strip()
    if not p:
        raise ValueError("附属文件路径不能为空")
    if len(p) > MAX_SKILL_RELPATH_LEN:
        raise ValueError(f"附属文件路径过长（>{MAX_SKILL_RELPATH_LEN} 字符）")
    if "\\" in p:
        raise ValueError("附属文件路径必须使用 POSIX 斜杠（/）分隔")
    if p.startswith("/") or (len(p) >= 2 and p[1] == ":"):
        raise ValueError("附属文件路径必须是相对路径")
    if any(ord(c) < 32 for c in p):
        raise ValueError("附属文件路径含控制字符")
    for seg in p.split("/"):
        if not seg or seg in (".", ".."):
            raise ValueError("附属文件路径含非法路径段")
    if p == "SKILL.md":
        raise ValueError("SKILL.md 由技能正文（body）独占，不能作为附属文件")
    return p


@dataclass
class SkillMeta:
    name: str
    description: str
    version: str = "1.0"
    source: str = "builtin"        # builtin / 目录路径
    scope: str = "builtin"         # builtin / user / agent / org
    enabled: bool = True
    path: str = ""


BUILTIN_SKILLS: Dict[str, Dict[str, str]] = {
    "chart-report": {
        "description": "生成数据图表与文字结合的分析小结（Markdown 表格 + 结论）。",
        "body": (
            "---\n"
            "name: chart-report\n"
            "description: 生成数据图表与文字结合的分析小结（Markdown 表格 + 结论）\n"
            "version: 1.0\n"
            "---\n\n"
            "# chart-report\n\n"
            "当需要输出数据分析小结时使用本技能：\n"
            "1. 先用表格列出关键指标；\n"
            "2. 给出 2-4 条结论；\n"
            "3. 结论必须与表格数字一致，不得编造。\n"
        ),
    },
    "meeting-minutes": {
        "description": "把原始素材整理为结构化会议纪要（议题/决议/待办）。",
        "body": (
            "---\n"
            "name: meeting-minutes\n"
            "description: 把原始素材整理为结构化会议纪要（议题/决议/待办）\n"
            "version: 1.0\n"
            "---\n\n"
            "# meeting-minutes\n\n"
            "当用户给出会议记录/录音转写素材时使用本技能：\n"
            "1. 提炼议题与讨论要点；\n"
            "2. 归纳决议；\n"
            "3. 输出待办清单（负责人 + 截止时间，如素材中没有则标注待定）。\n"
        ),
    },
}

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def _parse_frontmatter(body: str) -> Dict[str, str]:
    """解析 SKILL.md YAML frontmatter 的 key: value 行（轻量、无依赖）。"""
    m = _FRONTMATTER_RE.match(body or "")
    out: Dict[str, str] = {}
    if not m:
        return out
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


class SkillRegistry:
    """技能注册表：内置 + 外部目录技能，启用范围控制与审计。"""

    def __init__(self, disabled: Optional[Set[str]] = None):
        self.disabled: Set[str] = disabled or set()

    @classmethod
    def from_settings(cls) -> "SkillRegistry":
        raw = getattr(settings, "AGENT_DISABLED_SKILLS", "") or ""
        disabled = {x.strip() for x in raw.split(",") if x.strip()}
        return cls(disabled=disabled)

    def _external_skills(self) -> List[SkillMeta]:
        """扫描 AGENT_SKILLS_DIR 下的 <name>/SKILL.md（scope 按子目录归属）。"""
        root = (getattr(settings, "AGENT_SKILLS_DIR", "") or "").strip()
        out: List[SkillMeta] = []
        if not root or not os.path.isdir(root):
            return out
        for entry in sorted(os.listdir(root)):
            skill_dir = os.path.join(root, entry)
            md_path = os.path.join(skill_dir, "SKILL.md")
            if not os.path.isfile(md_path):
                continue
            try:
                with open(md_path, encoding="utf-8") as f:
                    body = f.read()
            except OSError:
                continue
            fm = _parse_frontmatter(body)
            name = fm.get("name") or entry
            out.append(SkillMeta(
                name=name,
                description=fm.get("description", ""),
                version=fm.get("version", "1.0"),
                source=skill_dir,
                scope="org",       # 平台目录技能视为组织范围（用户/Agent 私有技能经各自目录接入）
                enabled=name not in self.disabled,
                path=md_path,
            ))
        return out

    def list_skills(self, scope: Optional[str] = None) -> List[SkillMeta]:
        out = []
        for name, spec in BUILTIN_SKILLS.items():
            out.append(SkillMeta(
                name=name,
                description=spec["description"],
                enabled=name not in self.disabled,
            ))
        out.extend(self._external_skills())
        if scope:
            out = [s for s in out if s.scope == scope]
        return out

    def enabled_skills(self) -> List[SkillMeta]:
        return [s for s in self.list_skills() if s.enabled]

    def get_skill_body(self, meta: SkillMeta) -> str:
        if meta.source == "builtin":
            return BUILTIN_SKILLS[meta.name]["body"]
        with open(meta.path, encoding="utf-8") as f:
            return f.read()

    def files_payload(self) -> Dict[str, Any]:
        """StateBackend 注入格式：{"/skills/<name>/<相对路径>": {"content": ...}}

        注入技能目录**全部文件**（SKILL.md 指令 + 附属脚本/引用文件，标准
        Agent Skills 形态），但**不携带工具授权**（工具面由 Capability
        Registry / ToolPolicy 决定）。内置技能只含 SKILL.md；目录技能
        （AGENT_SKILLS_DIR）递归携带整个技能目录（隐藏文件/字节码不注入）。
        """
        from deepagents.backends.utils import create_file_data

        files: Dict[str, Any] = {}
        for meta in self.enabled_skills():
            if meta.source == "builtin":
                files[f"/skills/{meta.name}/SKILL.md"] = create_file_data(
                    content=self.get_skill_body(meta)
                )
                continue
            files.update(_skill_dir_files(meta.name, os.path.dirname(meta.path)))
        return files

    def to_audit_fields(self) -> Dict[str, Any]:
        return {
            "skills": [
                {"name": s.name, "version": s.version, "scope": s.scope,
                 "source": s.source, "enabled": s.enabled}
                for s in self.list_skills()
            ]
        }


# ── 目录技能多文件收集 ────────────────────────────────────────────────────────

def _skill_dir_files(name: str, skill_dir: str) -> Dict[str, Any]:
    """递归收集技能目录文件 → {"/skills/<name>/<相对路径>": FileData}。

    标准 Agent Skills 形态：SKILL.md + 附属脚本/引用文件整目录注入。
    - 跳过隐藏文件/目录、__pycache__/.git/node_modules、*.pyc；
    - UTF-8 文本直接注入，非 UTF-8 二进制以 base64 注入
      （create_file_data encoding="base64"）；
    - SKILL.md（核心指令）不受体积上限约束；附属文件应用
      单文件/总量/数量上限，超限跳过并记 warning（不阻断运行）。
    """
    import base64

    from deepagents.backends.utils import create_file_data

    files: Dict[str, Any] = {}
    total = 0
    aux_count = 0
    for root, dirs, filenames in os.walk(skill_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS)
        for fname in sorted(filenames):
            if fname.startswith(".") or fname.endswith(".pyc"):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, skill_dir).replace(os.sep, "/")
            # SKILL.md 是技能核心指令（validate_skill_relpath 对附属文件保留此名）
            is_skill_md = (rel == "SKILL.md")
            if not is_skill_md:
                try:
                    rel = validate_skill_relpath(rel)
                except ValueError:
                    continue
            if not is_skill_md and aux_count >= MAX_SKILL_FILE_COUNT:
                logger.warning("技能 %s 附属文件数量超限（>%d），其余不再注入", name, MAX_SKILL_FILE_COUNT)
                return files
            try:
                with open(fpath, "rb") as f:
                    raw = f.read()
            except OSError:
                continue
            try:
                content: str = raw.decode("utf-8")
                data = create_file_data(content=content)
            except UnicodeDecodeError:
                content = base64.b64encode(raw).decode("ascii")
                data = create_file_data(content=content, encoding="base64")
            size = len(content.encode("utf-8"))
            if not is_skill_md and (size > MAX_SKILL_FILE_BYTES or total + size > MAX_SKILL_TOTAL_BYTES):
                logger.warning("技能 %s 附属文件 %s 超出注入体积上限，跳过", name, rel)
                continue
            if not is_skill_md:
                total += size
                aux_count += 1
            files[f"/skills/{name}/{rel}"] = data
    return files


# ── 管理面（DB）技能接入 ──────────────────────────────────────────────────────

def ensure_skill_frontmatter(body: str, *, name: str, description: str = "",
                             version: str = "1.0") -> str:
    """保证 SKILL.md 含合法 frontmatter（name 以入参为准，缺则补、错则修）。"""
    body = body or ""
    fm = _parse_frontmatter(body)
    if fm:
        # 已有 frontmatter：仅当缺 name/version/description 时在首个 --- 块内补齐
        need_fix = (fm.get("name") != name) or ("version" not in fm) or ("description" not in fm)
        if not need_fix:
            return body
        lines = [f"name: {name}"]
        if description or fm.get("description"):
            lines.append(f"description: {fm.get('description') or description}")
        lines.append(f"version: {fm.get('version') or version}")
        rest = _FRONTMATTER_RE.sub("", body, count=1).lstrip("\n")
        return "---\n" + "\n".join(lines) + "\n---\n\n" + rest
    return (
        f"---\nname: {name}\ndescription: {description}\nversion: {version}\n---\n\n"
        f"{body}"
    )


def _binding_allows(agent_ids: Any, agent_id: Optional[int]) -> bool:
    """Agent 级绑定判定：None = 全局；[] = 不注入；列表 = 仅指定 Agent。"""
    if agent_ids is None:
        return True
    if not isinstance(agent_ids, (list, tuple)):
        return False
    return agent_id is not None and agent_id in agent_ids


def db_enabled_names(rows: List[Any], agent_id: Optional[int]) -> Set[str]:
    """通过启用 + Agent 级绑定过滤的 DB 技能名集合（供同名整目录覆盖复用）。"""
    names: Set[str] = set()
    for row in rows:
        if not getattr(row, "enabled", True):
            continue
        if not _binding_allows(getattr(row, "agent_ids", None), agent_id):
            continue
        name = getattr(row, "name", "") or ""
        if name:
            names.add(name)
    return names


def db_files_payload(rows: List[Any], agent_id: Optional[int]) -> Dict[str, Any]:
    """管理面（DB）技能 → StateBackend 注入格式（同 files_payload）。

    每个技能注入 SKILL.md（body）+ 附属文件（files：相对路径 → 文本内容，
    标准 Agent Skills 形态）；附属文件路径经 validate_skill_relpath 校验，
    非法防御性跳过，超限跳过不注入。
    过滤：enabled 且通过 Agent 级绑定判定；同名技能 DB 优先（后写覆盖前写）。
    rows 为 Skill ORM 行（或含同名属性的兼容对象）。
    """
    from deepagents.backends.utils import create_file_data

    files: Dict[str, Any] = {}
    for row in rows:
        if not getattr(row, "enabled", True):
            continue
        if not _binding_allows(getattr(row, "agent_ids", None), agent_id):
            continue
        name = getattr(row, "name", "") or ""
        body = getattr(row, "body", "") or ""
        if not name or not body:
            continue
        files[f"/skills/{name}/SKILL.md"] = create_file_data(content=body)
        total = 0
        aux_count = 0
        for path, content in (getattr(row, "files", None) or {}).items():
            if not isinstance(content, str):
                continue
            try:
                rel = validate_skill_relpath(path)
            except ValueError:
                logger.warning("技能 %s 附属文件路径非法，跳过注入: %r", name, path)
                continue
            if aux_count >= MAX_SKILL_FILE_COUNT:
                logger.warning("技能 %s 附属文件数量超限（>%d），其余不再注入", name, MAX_SKILL_FILE_COUNT)
                break
            size = len(content.encode("utf-8"))
            if size > MAX_SKILL_FILE_BYTES or total + size > MAX_SKILL_TOTAL_BYTES:
                logger.warning("技能 %s 附属文件 %s 超出注入体积上限，跳过", name, rel)
                continue
            total += size
            aux_count += 1
            files[f"/skills/{name}/{rel}"] = create_file_data(content=content)
    return files
