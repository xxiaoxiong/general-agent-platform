"""依据用户意图及已有文档上下文确定 Run 路线。"""
from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any, Iterable

from app.core.config import settings

from .backends import workspace_root

DOCUMENT_GRAPH_TYPES = frozenset({"profile_extract", "document_generate", "document_edit"})
_DOCUMENT_EXTENSIONS = {".docx", ".doc", ".pdf", ".md", ".txt", ".rtf", ".odt"}
# 引用本会话已有内容（"调研结果/讨论过的内容/上面的结论"）：
# 命中即必须把前文正文作为资料注入，不能让模型从零索要资料
_PRIOR_CONTENT = re.compile(
    r"上[面文一条述]|刚才|刚生成|前[文面]|之前|此前|前述|上述|这[些段份篇]|以上|"
    r"讨论(?:过)?的?|聊过|(?:调研|研究|分析|检索)(?:结果|结论)|已有内容|现有内容", re.I
)


def references_prior_content(message: str) -> bool:
    """用户是否在指代本会话已有内容（决定是否注入对话正文作为资料来源）。"""
    return bool(_PRIOR_CONTENT.search(message or ""))


# 用户显式授权自主决策（"不用再问我/缺少的用通用假设"）：此时任何表单式追问
# 都是打断，文档流水线必须按假设直接完成并在正文注明，而不是 waiting_input。
_AUTONOMY = re.compile(
    r"(?:不需要|不用|无需|不必|不要).{0,10}(?:再)?(?:向我)?(?:确认|询问|问我|补充|提供|索要)"
    r"|(?:自行|自己|自动|自主).{0,8}(?:决定|判断|补全|补充|假设|发挥|处理)"
    r"|(?:缺少|缺失|没有|不足).{0,12}(?:信息|资料|数据|内容).{0,12}(?:用|按|采用|自行).{0,8}(?:假设|通用|合理|补)"
    r"|(?:通用|合理).{0,6}假设"
    r"|(?:no need to|don't|do not).{0,24}(?:ask|confirm)",
    re.I,
)


def user_grants_autonomy(message: str) -> bool:
    """用户是否明确授权"缺的资料自行假设、不要再来问"。"""
    return bool(_AUTONOMY.search(message or ""))


_PROFILE = re.compile(
    r"(?:提炼|抽取|提取|建立|制作|创建|归纳|总结.{0,10}可复用|整理出).{0,25}(?:规范|模板|profile)"
    r"|(?:需要|想要|设计|定义|写|生成).{0,12}(?:一套|一份|一个).{0,20}(?:规范|模板)(?:$|[。！？，,\s])"
    r"|\b(?:extract|derive|create)\b.{0,35}\b(?:profile|reusable template)\b"
)
_EDIT = re.compile(
    r"(?:改|修改|改写|重写|重新写|重新整理|重组|重排|替换|删除|增加|补写|更新|润色|调整|勘误|"
    r"插入|换掉|缩小|放大|去掉|清除|更换|优化|美化|统一|排版|调格式|完善|深化|细化|精简|梳理|"
    r"合并|拆分|规范化|正规化|修正)"
    r"|\b(?:edit|revise|rewrite|replace|update)\b"
)
# 样式/排版类诉求：只调整格式，不改文字内容（"深度优化文档样式、排版"）
_STYLE = re.compile(
    r"样式|排版|版式|格式|字体|字号|行距|段间距|段前|段后|缩进|对齐|页边距|页眉|页脚|目录|配色", re.I
)
_STYLE_ACTION = re.compile(
    r"优化|美化|统一|规范|调整|修改|重排|重新排|改版|升级|重新给|重新出|重新生成"
    r"|套用.{0,10}(?:样式|模板|格式)", re.I
)
# 明确的内容改动（与样式诉求区分）："补充最新数据"属于改内容，不能只改格式
_CONTENT_EDIT = re.compile(
    r"(?:补充|增加|删除|重写|扩写|改写|修订|更新|修正).{0,12}(?:内容|正文|章节|数据|文字|事实)"
    r"|(?:内容|正文|章节|数据|文字).{0,12}(?:补充|增加|删除|重写|扩写|改写|更新|修正)", re.I
)
# 结构性返工/合规改写：点名"不符合要求""需符合写作规范""深化迭代"时属于内容与结构
# 返工，即使同时出现"样式/排版"也不能降级成只重排格式（143 实录：1s 直出 v3/v4 未改内容）
_CONTENT_REVISION = re.compile(
    r"不符合|不满足|未达到|达不到|写作规范|行文规范|内容规范"
    r"|结构调整|调整结构|深化|迭代|返工|重做|重构|完善|补充|新增|删除|替换", re.I
)
_REPLACE = re.compile(r"(?:把|将).{0,110}(?:改成|改为|换成|换为|改掉|去掉)|(?:改成|更换|缩放|移除).{0,12}(?:图片|插图|配图)")
_EDIT_TARGET = re.compile(
    r"第\s*[一二三四五六七八九十百零〇两\d]+\s*[章节段条]|(?:文档|报告|word)(?:的|中|里)"
    r"|(?:上次|上一版|刚才|刚生成|原有|已有).{0,12}(?:文档|报告|总结|word|版)"
    r"|\b(?:section|chapter|previous report|existing document)\b"
)
# 文档对象指代：会话内已有文档时，带编辑动作的"全文/正文/表格"等诉求同样属于文档编辑
_DOC_OBJECT = re.compile(r"全文|全篇|整篇|整份|文档|正文|章节|小节|段落|表格|图表")
# 只有点名 Word/DOCX/文档交付物时才进入 Word 流水线；「写一份报告/总结/方案」
# 这类诉求直接在对话中交付正文（通用能力），不得默认生成 DOCX。
_GENERATE = re.compile(
    r"(?:生成|制作|输出|导出|撰写|编写|写一[份个篇]|写成|整理成|转[成为]|形成|起草|交付|放到|存到|写到|保存到)"
    r".{0,28}(?:word|docx|文档|公文)"
    r"|(?:按照|根据|用|套用).{0,22}(?:规范|模板|profile).{0,15}(?:生成|制作|起草|编写|撰写|写|输出)"
    r"|\b(?:generate|write|draft|create|export)\b.{0,35}\b(?:document|word|docx)\b"
)
_ANALYZE = re.compile(r"阅读|解读|解释|摘要|总结|概括|分析|提取|对比|怎么看|说了什么|summari[sz]e|analy[sz]e|explain|read")
_FILE_REFERENCE = re.compile(r"附件|文件|文档|这份|这篇|word|docx|pdf|attachment|file|document")
_PREVIOUS_DOCUMENT = re.compile(r"(?:上次|上一版|刚才|刚生成|已有).{0,12}(?:文档|报告|总结|word|版)")
_HOW_TO = re.compile(r"^(?:请问[，,\s]*)?(?:如何|怎么|怎样|为何|为什么|how (?:do|can|to))")
_DELIVERY = re.compile(
    r"(?:给(?:到)?我|发(?:给)?我|提供|导出).{0,18}(?:可下载|成品|word|docx|文档)"
    r"|(?:做|整理|写)成.{0,10}(?:word|docx|文档)|\bdownloadable\b.{0,15}\b(?:document|word|docx)\b"
    # 「放到 word 文档中给到我」类语序：交付物在前、"给到我"在后
    r"|(?:word|docx|文档|报告).{0,14}(?:给(?:到)?我|发(?:给)?我|交付)"
)
_MEDIA_TARGET = re.compile(r"第.{0,6}[张幅]图|图\s*[一二三四五六七八九十\d]+|图片|插图|配图")

# ── 交付格式识别 ──────────────────────────────────────────────────────────
# 文档图只负责 Word（DOCX）的排版/校验/版本；用户点名其他格式时必须交给
# 通用能力（工作区文件/代码执行）处理，不能一律当成 Word 生成。
_FORMAT_DOCX = re.compile(r"(?<![a-zA-Z])(?:word|docx?)(?![a-zA-Z])|\.docx?\b", re.I)
_FORMAT_OTHER = (
    ("md", re.compile(r"(?<![a-zA-Z])markdown(?![a-zA-Z])|(?<![a-zA-Z])md(?![a-zA-Z])", re.I)),
    ("txt", re.compile(r"(?<![a-zA-Z])txt(?![a-zA-Z])|纯文本|文本文件", re.I)),
    ("csv", re.compile(r"(?<![a-zA-Z])csv(?![a-zA-Z])", re.I)),
    ("xlsx", re.compile(r"(?<![a-zA-Z])(?:xlsx?|excel)(?![a-zA-Z])", re.I)),
    ("pdf", re.compile(r"(?<![a-zA-Z])pdf(?![a-zA-Z])", re.I)),
    ("pptx", re.compile(r"(?<![a-zA-Z])(?:pptx?|powerpoint)(?![a-zA-Z])", re.I)),
    ("html", re.compile(r"(?<![a-zA-Z])(?:html|htm)(?![a-zA-Z])", re.I)),
    ("json", re.compile(r"(?<![a-zA-Z])json(?![a-zA-Z])", re.I)),
)


def detect_deliverable_format(message: str) -> str | None:
    """识别用户明确要求的交付文件格式（取最后一次提到的格式）。

    返回 ``docx`` / ``md`` / ``txt`` / ``csv`` / ``xlsx`` / ``pdf`` /
    ``pptx`` / ``html`` / ``json``；未点名格式返回 ``None``。
    """
    hits: list[tuple[int, str]] = [
        (match.start(), fmt)
        for fmt, pattern in _FORMAT_OTHER
        if (match := pattern.search(message or ""))
    ]
    if match := _FORMAT_DOCX.search(message or ""):
        hits.append((match.start(), "docx"))
    return max(hits)[1] if hits else None


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
    # 用户点名非 Word 格式，或附件本身是文本类文件：文档图只处理 DOCX，
    # 这类请求交给通用能力（工作区读写/代码执行）完成，不能强行生成 Word
    deliverable_format = detect_deliverable_format(content)
    text_attachment = any(
        Path(str(ref.get("path") or "")).suffix.casefold() in {".md", ".markdown", ".txt", ".csv", ".json"}
        for ref in attachment_refs
    )
    non_word = deliverable_format not in (None, "docx") or (text_attachment and not has_word)
    edit = _EDIT.search(content) or _REPLACE.search(content)
    # 只调样式/排版（不含内容改动与结构返工）且会话/附件里已有文档 → 编辑原文档，
    # 绝不能当成新主题生成；同时点名章节/指出不符合要求时必须走真实内容修改流程
    style_edit = bool(actionable and _STYLE.search(content) and _STYLE_ACTION.search(content)
                      and not _CONTENT_EDIT.search(content) and not _CONTENT_REVISION.search(content)
                      and not non_word)
    if task_mode != "auto":
        task_type = "general_chat" if task_mode == "chat" else task_mode
        confidence, reason = 1.0, "用户显式选择任务模式"
    elif style_edit and (document_id or has_document_context or has_word or _PREVIOUS_DOCUMENT.search(content)):
        task_type, confidence, reason = "document_edit", 0.95, "用户要求优化已有文档的样式或排版"
    elif style_edit:
        # 没有任何可优化文档时不得凭空生成"讲排版"的新文档，让对话澄清目标文档
        task_type, confidence, reason = "general_chat", 0.7, "样式/排版优化需要先指明具体文档"
    elif actionable and edit and not non_word and (
        document_id or _PREVIOUS_DOCUMENT.search(content)
        or (has_word and (_FILE_REFERENCE.search(content) or _EDIT_TARGET.search(content) or _MEDIA_TARGET.search(content)))
        or (has_document_context and (_EDIT_TARGET.search(content) or _MEDIA_TARGET.search(content)
                                      or _DOC_OBJECT.search(content)))
    ):
        task_type, confidence, reason = "document_edit", 0.98, "用户要求修改已有文档"
    elif actionable and _PROFILE.search(content) and not document_profile_id:
        task_type, confidence, reason = "profile_extract", 0.95, "用户要求创建可复用文档规范"
    elif actionable and (_GENERATE.search(content) or _DELIVERY.search(content)):
        if non_word:
            task_type, confidence, reason = (
                "general_chat", 0.9, "用户明确要求非 Word 格式文件，交由通用能力生成与交付",
            )
        else:
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
        # confidence 仅为兼容旧事件字段保留，不再代表模型校准后的可信度；
        # 决策来源与确定性请以 intent_source / decision_status 为准
        "confidence": confidence,
        "intent_source": "rules",
        "reason": reason,
        "document_profile_id": document_profile_id,
        "document_id": document_id,
        "style_edit": bool(style_edit and task_type == "document_edit"),
        "deliverable_format": deliverable_format,
        "router_version": 3,
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


def continuation_attachments(
    message: str, history: list[dict], task_type: str, current: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """后续文档任务自动沿用会话内最近上传的 Word。

    用户上传资料后通常不会在追问时重复上传；指代前文（"这份报告/基于上面"）的
    规范提炼与文档生成任务必须仍能读到源资料，否则只能凭空生成。
    """
    if tuple(current) or task_type not in {"profile_extract", "document_generate", "document_analyze"}:
        return []
    if not references_prior_content(message):
        return []
    return recent_word_attachments(history)


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


def _attachment_name(value: Any) -> str:
    relative = str(value or "").replace("\\", "/")
    parts = relative.split("/")
    if (
        len(parts) != 2 or parts[0] != "uploads" or not parts[1]
        or parts[1] in (".", "..") or ":" in parts[1]
        or any(ord(char) < 32 for char in parts[1])
    ):
        raise ValueError("附件路径无效")
    return parts[1]


async def resolve_attachment_refs_async(
    attachments: list[dict[str, Any]],
    *,
    user_id: int,
    agent_id: int,
    session_id: int | None,
) -> list[dict[str, Any]]:
    """按会话后端读取附件指纹：项目会话读 Sandbox /workspace，存量会话读本地工作区。"""
    if not attachments:
        return []
    if len(attachments) > 30:
        raise ValueError("单次请求最多支持 30 个附件")
    if session_id is None:
        raise ValueError("请先将附件上传至当前会话")
    from app.core.database import AsyncSessionLocal
    from app.models.agent import AgentSession
    from app.services.sandbox import session_files

    async with AsyncSessionLocal() as db:
        session = await db.get(AgentSession, session_id)
        if session is None or session.user_id != user_id:
            raise ValueError("会话不存在")
        if session_files.uses_sandbox(session):
            refs = []
            max_bytes = int(settings.AGENT_MAX_FILE_SIZE_MB * 1024 * 1024)
            for attachment in attachments:
                name = _attachment_name(attachment.get("path"))
                relative = f"uploads/{name}"
                try:
                    raw, _mime = await session_files.read_bytes(db, session, relative)
                except Exception as exc:  # noqa: BLE001 Sandbox 不可达按附件缺失处理
                    raise FileNotFoundError("会话附件不存在") from exc
                if len(raw) > max_bytes:
                    raise ValueError("附件超过大小限制")
                ref: dict[str, Any] = {
                    "path": relative, "name": name, "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
                text_path = attachment.get("text_path")
                if text_path:
                    expected = f"{relative}.extracted.txt"
                    if str(text_path).replace("\\", "/") != expected:
                        raise ValueError("附件文本副本路径无效")
                    try:
                        text_raw, _mime = await session_files.read_bytes(db, session, expected)
                    except Exception:  # noqa: BLE001 文本副本缺失不阻断
                        pass
                    else:
                        if len(text_raw) <= max_bytes:
                            ref["text_path"] = expected
                            ref["text_sha256"] = hashlib.sha256(text_raw).hexdigest()
                refs.append(ref)
            return refs
    return await asyncio.to_thread(
        resolve_attachment_refs, attachments, user_id=user_id,
        agent_id=agent_id, session_id=session_id,
    )
