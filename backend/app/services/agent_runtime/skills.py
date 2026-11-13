"""
Skill 注册与装载（DeepAgents 原生 Skills 机制，P2-05）。

Skill = 目录 + SKILL.md（YAML frontmatter: name/description/version）。
StateBackend 下 skill 文件以 files 注入；FilesystemBackend 下由 SkillsMiddleware
从 backend 读取（source 路径 /skills/<name>/），渐进式加载。

Registry（§4.6）：
- 来源范围：内置（builtin）/ 用户（user）/ Agent（agent）/ 组织（org），
  外部目录经 AGENT_SKILLS_DIR 扫描（按目录归属判定 scope）；
- 元数据：版本、启用状态、来源、范围、审计字段（to_audit_fields）；
- 安全边界：Skill 只是指令文本（prompt 注入面），**不得自行提升工具权限**——
  工具面始终由 Capability Registry / ToolPolicy 决定，Skill 内容无法授权工具。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from app.core.config import settings


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

    def files_payload(self) -> Dict[str, Dict[str, str]]:
        """StateBackend 注入格式：{"/skills/<name>/SKILL.md": {"content": ...}}

        只注入 /skills/** 指令文件——Skill 不携带工具授权（工具面由
        Capability Registry / ToolPolicy 决定）。
        """
        from deepagents.backends.utils import create_file_data

        files: Dict[str, Any] = {}
        for meta in self.enabled_skills():
            files[f"/skills/{meta.name}/SKILL.md"] = create_file_data(
                content=self.get_skill_body(meta)
            )
        return files

    def to_audit_fields(self) -> Dict[str, Any]:
        return {
            "skills": [
                {"name": s.name, "version": s.version, "scope": s.scope,
                 "source": s.source, "enabled": s.enabled}
                for s in self.list_skills()
            ]
        }
