"""测评产物采集：读取 Trial 真实工作区的最终文件（只读、限额、防路径逃逸）。

工具调用记录里出现 write_file 不等于最终交付成功：后续 edit/delete 可能改变
结果。工程/文档类任务必须以工作区最终文件为准（任务书 §4.7）。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.config import settings

MAX_FILES = 100
MAX_FILE_BYTES = 2 * 1024 * 1024      # 单文件读取上限
MAX_TOTAL_TEXT_CHARS = 200_000        # 合并文本上限（供评分/验证使用）

# 展示前脱敏：常见密钥/令牌/密码形态（不改变落盘文件，仅用于在线预览）
_REDACT_RULES = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "***"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE), "Bearer ***"),
    (re.compile(
        r"(?i)\b(api[_-]?key|secret|password|passwd|token|access[_-]?key)"
        r"(\s*[:=]\s*)[\"']?[^\s\"',;]{4,}"), r"\1\2***"),
    (re.compile(r"((?:https?|redis|postgresql|mysql)://)[^/\s:@]+:[^/\s@]+@"),
     r"\1***@"),
)


def redact_text(text: str) -> str:
    """对在线展示的文本做敏感信息脱敏（保守匹配，避免破坏普通代码）。"""
    result = text or ""
    for pattern, replacement in _REDACT_RULES:
        result = pattern.sub(replacement, result)
    return result

_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".vue", ".html", ".css", ".scss",
    ".sql", ".sh", ".toml", ".ini", ".cfg", ".xml", ".log", ".rst",
}


def workspace_root(user_id: int, agent_id: int, session_id: Optional[int]) -> Optional[Path]:
    """返回 Trial 工作区根目录；无用户/会话或路径非法时返回 None。"""
    if user_id is None or session_id is None:
        return None
    base = Path(settings.AGENT_FILES_ROOT).expanduser().resolve()
    root = (base / str(user_id) / str(agent_id) / str(session_id)).resolve()
    if root != base and base not in root.parents:
        return None
    return root if root.is_dir() else None


def _collect_sync(user_id: int, agent_id: int, session_id: Optional[int]) -> Dict[str, Any]:
    root = workspace_root(user_id, agent_id, session_id)
    if root is None:
        return {"files": [], "text": "", "root": None}

    files = []
    parts = []
    total_chars = 0
    for path in sorted(root.rglob("*")):
        if len(files) >= MAX_FILES:
            break
        try:
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
            if root not in resolved.parents:
                continue
            relative = resolved.relative_to(root).as_posix()
            size = resolved.stat().st_size
            entry: Dict[str, Any] = {"path": relative, "size": size}
            if size <= 0:
                entry["empty"] = True
                files.append(entry)
                continue
            if size > MAX_FILE_BYTES:
                entry["skipped"] = "too_large"
                files.append(entry)
                continue
            raw = resolved.read_bytes()
            entry["sha256"] = hashlib.sha256(raw).hexdigest()
            if resolved.suffix.casefold() in _TEXT_SUFFIXES:
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = ""
                if text:
                    remaining = MAX_TOTAL_TEXT_CHARS - total_chars
                    if remaining > 0:
                        piece = text[:remaining]
                        total_chars += len(piece)
                        parts.append(f"\n\n[工作区文件: {relative}]\n{piece}")
                    entry["text_chars"] = len(text)
            files.append(entry)
        except (OSError, ValueError):
            continue
    return {"files": files, "text": "".join(parts), "root": str(root)}


async def collect_workspace_artifacts(
    user_id: Optional[int],
    agent_id: Optional[int],
    session_id: Optional[int],
) -> Dict[str, Any]:
    """采集 Trial 工作区最终产物（异步包装，避免阻塞事件循环）。"""
    if user_id is None or agent_id is None or session_id is None:
        return {"files": [], "text": "", "root": None}
    import asyncio

    return await asyncio.to_thread(_collect_sync, int(user_id), int(agent_id), int(session_id))
