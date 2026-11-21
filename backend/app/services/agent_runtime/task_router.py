"""依据用户意图及已有文档上下文确定 Run 路线。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Iterable

from app.core.config import settings

from .backends import workspace_root

DOCUMENT_GRAPH_TYPES = frozenset({"profile_extract", "document_generate", "document_edit"})
_DOCUMENT_EXTENSIONS = {".docx", ".doc", ".pdf", ".md", ".txt", ".rtf", ".odt"}
_PROFILE = re.compile(
    r"(?:提炼|抽取|提取|建立|制作|创建|归纳|总结.{0,10}可复用|整理出).{0,25}(?:规范|模板|profile)"
    r"|(?:需要|想要|设计|定义|写|生成).{0,12}(?:一套|一份|一个).{0,20}(?:规范|模板)(?:$|[。！？，,\s])"
    r"|\b(?:extract|derive|create)\b.{0,35}\b(?:profile|reusable template)\b"
)
_EDIT = re.compile(
    r"(?:修改|改写|重写|重新写|重新整理|重组|重排|替换|删除|增加|补写|更新|润色|调整|勘误|"
    r"插入|换掉|缩小|放大|去掉|清除|更换)"
    r"|\b(?:edit|revise|rewrite|replace|update)\b"
)
_REPLACE = re.compile(r"(?:把|将).{0,110}(?:改成|改为|换成|换为|改掉|去掉)|(?:改成|更换|缩放|移除).{0,12}(?:图片|插图|配图)")
_EDIT_TARGET = re.compile(
    r"第[一二三四五六七八九十百零〇两\d]+[章节段条]|(?:文档|报告|word)(?:的|中|里)"
    r"|(?:上次|上一版|刚才|刚生成|原有|已有).{0,12}(?:文档|报告|总结|word|版)"
    r"|\b(?:section|chapter|previous report|existing document)\b"
)
_GENERATE = re.compile(
    r"(?:生成|制作|输出|导出|撰写|编写|写一[份个篇]|写成|整理成|转[成为]|形成|起草|交付)"
    r".{0,28}(?:word|docx|文档|报告|公文|方案|简报|说明书|纪要|总结|手册|指南)"
    r"|(?:按照|根据|用|套用).{0,22}(?:规范|模板|profile).{0,15}(?:生成|制作|起草|编写|撰写|写|输出)"
    r"|\b(?:generate|write|draft|create|export)\b.{0,35}\b(?:report|document|word|docx)\b"
)
_ANALYZE = re.compile(r"阅读|解读|解释|摘要|总结|概括|分析|提取|对比|怎么看|说了什么|summari[sz]e|analy[sz]e|explain|read")
_FILE_REFERENCE = re.compile(r"附件|文件|文档|这份|这篇|word|docx|pdf|attachment|file|document")
_PREVIOUS_DOCUMENT = re.compile(r"(?:上次|上一版|刚才|刚生成|已有).{0,12}(?:文档|报告|总结|word|版)")
_HOW_TO = re.compile(r"^(?:请问[，,\s]*)?(?:如何|怎么|怎样|为何|为什么|how (?:do|can|to))")
_DELIVERY = re.compile(
    r"(?:给(?:到)?我|发(?:给)?我|提供|导出).{0,18}(?:可下载|成品|word|docx|文档)"
    r"|(?:做|整理|写)成.{0,10}(?:word|docx|文档)|\bdownloadable\b.{0,15}\b(?:document|word|docx)\b"
)
_MEDIA_TARGET = re.compile(r"第.{0,6}[张幅]图|图\s*[一二三四五六七八九十\d]+|图片|插图|配图")


def route_task(
    message: str,
    *,
    task_mode: str = "auto",
    document_profile_id: int | None = None,
    document_id: int | None = None,
    attachment_refs: Iterable[dict[str, Any]] = (),
    has_document_context: bool = False,
    has_word_attachment: bool = False,
) -> dict[str, Any]:
    """显式选择、上下文和明确意图依次参与决策。"""
    content = message.strip().casefold()
    actionable = not _HOW_TO.match(content)
    attachment_refs = tuple(attachment_refs)
    has_word = has_word_attachment or any(Path(ref["path"]).suffix.casefold() == ".docx" for ref in attachment_refs)
    edit = _EDIT.search(content) or _REPLACE.search(content)
    if task_mode != "auto":
        task_type = "general_chat" if task_mode == "chat" else task_mode
        confidence, reason = 1.0, "用户显式选择任务模式"
    elif actionable and edit and (
        document_id or _PREVIOUS_DOCUMENT.search(content)
        or (has_word and (_FILE_REFERENCE.search(content) or _EDIT_TARGET.search(content) or _MEDIA_TARGET.search(content)))
        or (has_document_context and (_EDIT_TARGET.search(content) or _MEDIA_TARGET.search(content)))
    ):
        task_type, confidence, reason = "document_edit", 0.98, "用户要求修改已有文档"
    elif actionable and _PROFILE.search(content) and not document_profile_id:
        task_type, confidence, reason = "profile_extract", 0.95, "用户要求创建可复用文档规范"
    elif actionable and (_GENERATE.search(content) or _DELIVERY.search(content)):
        task_type, confidence, reason = "document_generate", 0.96, "用户明确要求生成文档交付物"
    elif _ANALYZE.search(content) and (
        document_id or _FILE_REFERENCE.search(content) or any(
            Path(ref["path"]).suffix.casefold() in _DOCUMENT_EXTENSIONS
            for ref in attachment_refs
        )
    ):
        task_type, confidence, reason = "document_analyze", 0.91, "用户要求阅读或分析文档内容"
    else:
        task_type, confidence, reason = "general_chat", 0.8, "未识别到明确的文档交付意图"
    return {
        "task_type": task_type,
        "task_mode": task_mode,
        "confidence": confidence,
        "reason": reason,
        "document_profile_id": document_profile_id,
        "document_id": document_id,
        "router_version": 1,
    }


def word_candidates(attachments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in attachments if Path(str(item.get("path") or "")).suffix.casefold() == ".docx"]


def recent_word_attachments(history: list[dict]) -> list[dict[str, Any]]:
    """回读最近一次用户明确上传的 Word，兼容旧版会话中的附件注记。"""
    for entry in reversed(history):
        if not isinstance(entry, dict) or entry.get("role") != "user":
            continue
        attachments = entry.get("attachments")
        if isinstance(attachments, list):
            candidates = word_candidates(item for item in attachments if isinstance(item, dict))
        else:
            content = str(entry.get("content") or "")
            _, marker, note = content.partition("\n\n[附件]")
            if not marker:
                continue
            refs = []
            for line in note.splitlines():
                if not line.startswith("- /workspace/uploads/"):
                    continue
                path = line.removeprefix("- /workspace/").split("（文本副本：", 1)[0]
                refs.append({"path": path, "name": Path(path).name})
            candidates = word_candidates(refs)
        if candidates:
            return candidates
    return []


def choose_word_attachment(message: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(candidates) == 1:
        return candidates[0]
    names = [(str(item.get("name") or Path(item["path"]).name), item) for item in candidates]
    named = [item for name, item in names if name.casefold() in message.casefold()]
    return named[0] if len(named) == 1 else None


def validated_upload_root(user_id: int, agent_id: int, session_id: int | None) -> Path:
    """核对当前用户工作区的 uploads 父路径，防止符号链接跨过会话边界。"""
    base = Path(workspace_root(user_id, agent_id, session_id))
    expected = (
        Path(settings.AGENT_FILES_ROOT).resolve() / str(user_id) / str(agent_id)
        / (str(session_id) if session_id is not None else "ephemeral")
    )
    root = base / "uploads"
    try:
        if base.resolve() != expected or root.resolve() != expected / "uploads":
            raise ValueError("会话附件路径越界")
    except RuntimeError:
        raise ValueError("会话附件路径无效") from None
    return root


def resolve_attachment_refs(
    attachments: list[dict[str, Any]],
    *,
    user_id: int,
    agent_id: int,
    session_id: int | None,
) -> list[dict[str, Any]]:
    """读取当前会话的文件指纹，供幂等校验与后续执行复核。"""
    if len(attachments) > 30:
        raise ValueError("单次请求最多支持 30 个附件")
    if attachments and session_id is None:
        raise ValueError("请先将附件上传至当前会话")
    root = validated_upload_root(user_id, agent_id, session_id)
    max_bytes = int(settings.AGENT_MAX_FILE_SIZE_MB * 1024 * 1024)

    def resolve(value: Any) -> tuple[str, Path]:
        relative = str(value or "").replace("\\", "/")
        parts = relative.split("/")
        if (
            len(parts) != 2 or parts[0] != "uploads" or not parts[1]
            or parts[1] in (".", "..") or ":" in parts[1]
            or any(ord(char) < 32 for char in parts[1])
        ):
            raise ValueError("附件路径无效")
        path = root / parts[1]
        if path.is_symlink() or not path.is_file() or path.resolve() != root.resolve() / parts[1]:
            raise FileNotFoundError("会话附件不存在")
        return relative, path

    refs = []
    for attachment in attachments:
        relative, path = resolve(attachment.get("path"))
        with path.open("rb") as stream:
            size = path.stat().st_size
            if size > max_bytes:
                raise ValueError("附件超过大小限制")
            ref: dict[str, Any] = {
                "path": relative,
                "name": path.name,
                "size": size,
                "sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
            }
        text_path = attachment.get("text_path")
        if text_path:
            if str(text_path).replace("\\", "/") != f"{relative}.extracted.txt":
                raise ValueError("附件文本副本路径无效")
            try:
                ref["text_path"], extracted = resolve(text_path)
            except FileNotFoundError:
                pass
            else:
                if extracted.stat().st_size <= max_bytes:
                    with extracted.open("rb") as stream:
                        ref["text_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
                else:
                    del ref["text_path"]
        refs.append(ref)
    return refs
