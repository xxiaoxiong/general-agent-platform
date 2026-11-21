"""从同一会话识别文档任务的后续请求，并选取精确的历史资料。"""

from __future__ import annotations

import re

from .task_router import recent_word_attachments, route_task

_MANUAL = re.compile(r"手册|指南|报告|方案|白皮书|说明书|简报|纪要|总结|文档|word|docx", re.I)
_FOLLOW_UP = re.compile(
    r"^\s*(?:请)?继续[呀吧。！\s]*$|(?:说|用|换成|改成)(?:中文|英文|汉语|英语)"
    r"|(?:自行|自己|你来|帮我).{0,6}(?:发挥|补全|补充|编写|编一些)"
    r"|(?:前面|上面|之前|刚才|上一轮|上述|这份|当前).{0,30}(?:内容|草稿|构思|模板|文档|手册|报告|指南|方案|word)"
    r"|(?:接着|继续).{0,18}(?:写|生成|输出|完成|编写|处理)", re.I,
)
_EXPORT = re.compile(r"下载|导出|交付成|写到.{0,10}(?:word|docx)|整理成.{0,10}(?:word|docx)", re.I)
_REVISE = re.compile(r"重写|改写|修改|重做|润色|改版|新增|加上|增加|扩写|细化|补写|补上|调整|更新|"
                     r"删掉|删除|改成|翻译|改为|完善|精简|写得更|(?:写|讲|展开)详细", re.I)
_TEMPLATE = re.compile(r"模板|[以按照].{0,10}范本|参照.{0,12}(?:结构|格式|版式)", re.I)
_EXAMPLE = re.compile(
    r"(?:自己发挥|自行发挥|自行补全|虚构|构造).{0,38}(?:测试|示例|样例|演示)|"
    r"(?:测试|示例|样例|演示).{0,38}(?:自己发挥|自行发挥|自行补全|虚构|构造)", re.S,
)
_HEADING = re.compile(r"^\s*#{1,4}\s+(.+)$", re.M)
_QUESTION = re.compile(r"^(?:好[的，！\s]*，?)?(?:还需|请提供|请补充|我需要.{0,10}信息|I need more information)", re.I)
_TOPIC_CHANGE = re.compile(r"(?:换个|聊个|另一个|切换|新)话题|先(?:聊|问|讨论)别的")
_UNRELATED_QUESTION = re.compile(r"[?？]$|^(?:请问|你知道|你是谁|你用的什么|如何|怎样|为什么)")
_DRAFT_LIMIT = 160_000


def _main_text(message: dict) -> str:
    return str(message.get("content") or "").split("\n\n[附件]", 1)[0].strip()


def _substantive_draft(messages: list[dict]) -> str:
    for item in reversed(messages):
        if item.get("role") != "assistant":
            continue
        content = _main_text(item)
        headings = list(_HEADING.finditer(content))
        if len(content) >= 280 and len(headings) >= 2 and not _QUESTION.search(content[:180]):
            return content[headings[0].start():].strip()
    return ""


def is_template_reference(message: str) -> bool:
    return bool(_TEMPLATE.search(message))


def document_turn(message: str, history: list[dict], route: dict) -> tuple[dict, list[dict], str | None]:
    """返回附带上下文的路由、需核验的历史附件和可能有用户补充资料的原 Run。"""
    if route.get("task_mode") not in ("auto", "document_generate"):
        return route, [], None
    latest = message.strip()
    followup = bool(_FOLLOW_UP.search(latest))
    export = bool(_EXPORT.search(latest) and not _REVISE.search(latest))
    if not (followup or export or route.get("task_type") == "document_generate"):
        return route, [], None
    anchor = None
    recent = [item for item in history[-24:] if isinstance(item, dict)]
    if followup or export:
        for index, item in reversed(list(enumerate(recent))):
            if item.get("role") != "user":
                continue
            content = _main_text(item)
            if _TOPIC_CHANGE.search(content) or (not export and not _MANUAL.search(content)
                    and not _FOLLOW_UP.search(content) and _UNRELATED_QUESTION.search(content)):
                break
            if route_task(content)["task_type"] == "document_generate" and (
                is_template_reference(content) or (_MANUAL.search(content) and not _EXPORT.search(content))
            ):
                anchor = index
                break
    related = recent[anchor:] if anchor is not None else []
    draft = _substantive_draft(related or (recent if export else []))
    if anchor is None and not draft and route.get("task_type") != "document_generate":
        return route, [], None
    if route.get("task_type") not in {"document_generate", "general_chat", "document_analyze"}:
        return route, [], None
    if route.get("task_type") != "document_generate":
        route = {**route, "task_type": "document_generate", "confidence": 0.91,
                 "reason": "延续本会话的文档生成任务"}
    users = [_main_text(item) for item in related if item.get("role") == "user"]
    original = users[0][:3000] if users else ""
    refinements = [value[:1200] for value in users[1:] if value and not _EXPORT.search(value)][-8:]
    request = latest
    if original:
        parts = [f"原始文档任务：{original}"]
        if refinements:
            parts.append("后续补充要求：\n" + "\n".join(f"- {value}" for value in refinements))
        parts.append("本轮要求（优先）： " + latest)
        request = "\n\n".join(parts)
    inherited = []
    if is_template_reference(original or latest):
        inherited = recent_word_attachments(related[:1]) or recent_word_attachments(related)
    context = {
        "request": request, "draft": draft[:_DRAFT_LIMIT], "template_paths": [item["path"] for item in inherited],
        "example_mode": bool(_EXAMPLE.search("\n".join([*users, latest]))),
        "export_existing": bool(export and draft and len(draft) <= _DRAFT_LIMIT and (
            followup or re.search(r"^(?:下载|导出)|^(?:在界面中)?\s*(?:[给发](?:到)?我|我要|可以|帮我|请|直接).{0,20}(?:下载|导出)", latest)
        ) and not is_template_reference(latest)),
    }
    anchor_run = related[0].get("document_run_id") if related else None
    return {**route, "document_context": context}, inherited, anchor_run
