"""模型驱动的任务意图识别（通用能力，规则路由仅作快速通道与兜底）。

平台同时具备通用对话（工具/文件/代码）与 Word 文档流水线。用关键词枚举来判断
"这一轮到底要不要生成 Word"必然越写越僵硬：会话里出现过文档后，任何带「整理 /
上面 / 下载」的请求都可能被误判成文档任务。这里改为让模型基于**本轮消息 + 会话
上下文**做一次轻量分诊，规则路由（task_router.route_task）只负责：

- 用户显式选择任务模式（task_mode != auto）时的确定性直达；
- 模型不可用/超时/输出非法时的兜底；
- 资源安全边界（没有可编辑文档时不得进入文档编辑）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Iterable

from app.core.config import settings

logger = logging.getLogger(__name__)

INTENT_VALUES = {
    "chat", "create_document", "edit_document", "analyze_document",
    "extract_profile", "software_project",
}
FORMAT_VALUES = {"docx", "md", "txt", "csv", "xlsx", "pdf", "pptx", "html", "json", "none"}
TARGET_VALUES = {"none", "preview", "latest_draft", "attachment"}

_SYSTEM_PROMPT = """你是任务分诊器，负责判断用户最新一条消息应该由哪种执行形态处理。
只输出一个 JSON 对象，不要输出解释、Markdown 代码块或多余文字。

可选 intent：
- chat：普通问答、解释、分析、对比、建议、代码、写作、查询等，直接在对话中回答；
  用户要求下载非 Word 格式的文件（md/txt/csv/xlsx/pdf/pptx 等）也属于 chat，
  由通用能力写入工作区交付。
- create_document：用户明确要求生成一份新的 Word（.docx）版式文档并交付下载。
- edit_document：用户明确要求在会话已有的 Word 文档上修改内容、结构或样式排版。
- analyze_document：用户明确要求阅读、总结、分析已有文档或附件的内容。
- extract_profile：用户明确要求从文档中提炼可复用的写作规范/模板。
- software_project：用户明确要求构建一个完整可运行的软件项目。

判断原则：
1. 以**本轮消息本身**为准。会话以前生成过 Word，不代表这一轮仍是文档任务。
2. 用户要的是"答案/内容"而不是"文件"→ chat；只有明确要 Word 交付物才 create_document。
3. 用户点名 md/txt/csv/xlsx/pdf/pptx 等其他格式 → chat + 对应 deliverable_format。
4. 指代"上面的/刚才的/之前的"内容且要求整理成 Word → create_document；
   仅要求整理、总结、补充、修改结论 → chat。
5. "第 N 章/文档/报告/表格/图片"等修改诉求，只有在明确指向会话已有 Word 文档时
   才是 edit_document；否则按 chat 处理。
6. 不要因为上下文里有文档就强行归为文档任务；不确定时选 chat。

输出格式：
{"intent":"chat|create_document|edit_document|analyze_document|extract_profile|software_project",
 "deliverable_format":"docx|md|txt|csv|xlsx|pdf|pptx|html|json|none",
 "target":"none|preview|latest_draft|attachment",
 "reason":"不超过 40 字的中文理由"}
"""


def _clip(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def build_intent_prompt(
    message: str,
    *,
    history: Iterable[dict[str, Any]] = (),
    artifacts: Iterable[dict[str, Any]] = (),
    preview_document: dict[str, Any] | None = None,
    attachments: Iterable[dict[str, Any]] = (),
    recent_turn_limit: int = 8,
) -> str:
    """组装分诊上下文：最近对话、已有文档产物、预览文档、本轮附件。"""
    turns = [item for item in history if isinstance(item, dict)][-recent_turn_limit:]
    lines = ["## 最近对话"]
    if turns:
        for item in turns:
            role = "用户" if item.get("role") == "user" else "助手"
            marker = "（该轮产生了文档产物）" if item.get("document_run_id") else ""
            lines.append(f"- {role}{marker}：{_clip(item.get('content'), 600)}")
    else:
        lines.append("- （无历史，这是会话第一条消息）")

    produced = [item for item in artifacts if isinstance(item, dict)][:5]
    if produced:
        lines.append("\n## 会话已有文档产物")
        for item in produced:
            lines.append(
                f"- 《{_clip(item.get('title'), 80)}》 格式={item.get('format') or 'docx'} "
                f"document_id={item.get('document_id')} 最新版本={item.get('version_no')}"
            )

    if preview_document:
        lines.append(
            "\n## 用户当前正在预览的文档（仅当本轮消息确实针对它时才使用）\n"
            f"- 《{_clip(preview_document.get('title'), 80)}》 "
            f"document_id={preview_document.get('document_id')} "
            f"版本={preview_document.get('version_no')}"
        )

    files = [item for item in attachments if isinstance(item, dict)]
    if files:
        lines.append("\n## 本轮附件")
        for item in files:
            lines.append(f"- {_clip(item.get('name') or item.get('path'), 120)}")

    lines.append("\n## 本轮用户消息\n" + _clip(message, 4000))
    return "\n".join(lines)


def _extract_json(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        text = text[start:end + 1]
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def parse_intent(raw: str) -> dict[str, Any] | None:
    """解析并校验模型输出；非法值一律视为不可用（回退规则路由）。"""
    payload = _extract_json(raw)
    if payload is None:
        return None
    intent = str(payload.get("intent") or "").strip()
    if intent not in INTENT_VALUES:
        return None
    deliverable = str(payload.get("deliverable_format") or "none").strip().lower()
    if deliverable not in FORMAT_VALUES:
        deliverable = "none"
    target = str(payload.get("target") or "none").strip().lower()
    if target not in TARGET_VALUES:
        target = "none"
    return {
        "intent": intent,
        "deliverable_format": deliverable,
        "target": target,
        "reason": _clip(payload.get("reason"), 80) or "模型判定",
    }


async def invoke_intent_model(provider: dict[str, Any], model_config: dict[str, Any], prompt: str) -> str:
    """调用当前对话模型做一次非流式分诊（低温度、小 token）。"""
    from app.services.agent_runtime.model_adapter import build_chat_model

    chat = build_chat_model(
        provider,
        model_config,
        streaming=False,
        temperature=0,
        max_tokens=int(settings.AGENT_INTENT_ROUTER_MAX_TOKENS),
        require_tools=False,
    )
    response = await chat.ainvoke([
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ])
    content = getattr(response, "content", "")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return str(content or "")


async def classify_intent(
    message: str,
    *,
    provider: dict[str, Any],
    model_config: dict[str, Any],
    history: Iterable[dict[str, Any]] = (),
    artifacts: Iterable[dict[str, Any]] = (),
    preview_document: dict[str, Any] | None = None,
    attachments: Iterable[dict[str, Any]] = (),
) -> dict[str, Any] | None:
    """模型分诊入口：任何失败（超时/网络/非法输出）都返回 None，由规则兜底。"""
    if not settings.AGENT_INTENT_ROUTER_ENABLED:
        return None
    prompt = build_intent_prompt(
        message, history=history, artifacts=artifacts,
        preview_document=preview_document, attachments=attachments,
    )
    try:
        raw = await asyncio.wait_for(
            invoke_intent_model(provider, model_config, prompt),
            timeout=float(settings.AGENT_INTENT_ROUTER_TIMEOUT),
        )
    except asyncio.TimeoutError:
        logger.warning("意图识别超时（>%.1fs），回退规则路由", settings.AGENT_INTENT_ROUTER_TIMEOUT)
        return None
    except Exception as e:  # noqa: BLE001 模型不可用时必须继续可用
        logger.warning("意图识别调用失败，回退规则路由: %s", e)
        return None
    return parse_intent(raw)


def apply_intent(
    decision: dict[str, Any],
    rules: dict[str, Any],
    *,
    has_existing_document: bool,
    has_attachments: bool,
) -> dict[str, Any]:
    """把模型分诊结果映射为执行上下文；只保留资源安全边界，不做关键词改写。"""
    intent = decision["intent"]
    fmt = decision.get("deliverable_format") or "none"
    # 用户在本轮消息里点名的格式是确定性事实，模型不得改成别的格式
    explicit_format = rules.get("deliverable_format")
    if explicit_format and explicit_format != "docx" and intent in {"chat", "create_document"}:
        fmt = explicit_format
    route = dict(rules)
    route["intent"] = intent
    route["intent_source"] = "llm"
    route["intent_reason"] = decision.get("reason") or ""
    route.pop("document_context", None)

    if intent == "chat":
        route.update(task_type="general_chat", confidence=0.9, reason="模型判定本轮为通用对话")
        # 会话文档只是上下文，不代表本轮要绑定编辑目标
        route["document_id"] = None
        route["style_edit"] = False
        if fmt not in (None, "none"):
            route["deliverable"] = {"format": fmt}
        else:
            route.pop("deliverable", None)
        return route

    if intent == "create_document":
        if fmt in (None, "none"):
            fmt = "docx"
        route["deliverable_format"] = fmt
        if fmt == "docx":
            route.update(task_type="document_generate", confidence=0.92,
                         reason="模型判定需要生成 Word 文档交付物")
            route.pop("deliverable", None)
        else:
            # 非 Word 交付：通用能力写工作区文件，不进入 Word 流水线
            route.update(task_type="general_chat", confidence=0.9,
                         reason="模型判定需要交付非 Word 格式文件")
            route["document_id"] = None
            route["style_edit"] = False
            route["deliverable"] = {"format": fmt}
        return route

    if intent == "edit_document":
        if not (has_existing_document or has_attachments):
            route.update(task_type="general_chat", confidence=0.7,
                         reason="本轮没有可编辑的文档，按对话处理")
            route["document_id"] = None
            return route
        route.update(task_type="document_edit", confidence=0.92,
                     reason="模型判定需要修改已有文档")
        return route

    if intent == "analyze_document":
        if not (has_existing_document or has_attachments):
            route.update(task_type="general_chat", confidence=0.7,
                         reason="本轮没有可分析的文档，按对话处理")
            return route
        route.update(task_type="document_analyze", confidence=0.9,
                     reason="模型判定需要分析文档内容")
        return route

    if intent == "extract_profile":
        route.update(task_type="profile_extract", confidence=0.9,
                     reason="模型判定需要提炼可复用文档规范")
        return route

    # software_project 仅在团队模式下有执行形态；单助手模式按通用对话处理
    route.update(task_type="general_chat", confidence=0.75,
                 reason="模型判定为软件项目任务，但当前未开启团队协作")
    route["wants_software_project"] = True
    return route
