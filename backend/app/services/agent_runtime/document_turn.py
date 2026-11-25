"""从同一会话识别文档任务的后续请求，并选取精确的历史资料。"""

from __future__ import annotations

import re

from .task_router import recent_word_attachments, references_prior_content, route_task

_MANUAL = re.compile(r"手册|指南|报告|方案|白皮书|说明书|简报|纪要|总结|文档|word|docx", re.I)
_FOLLOW_UP = re.compile(
    r"^\s*(?:请)?继续[呀吧。！\s]*$|(?:说|用|换成|改成)(?:中文|英文|汉语|英语)"
    r"|(?:自行|自己|你来|帮我).{0,6}(?:发挥|补全|补充|编一些)"
    r"|(?:前面|上面|之前|刚才|上一轮|上述|这份|当前).{0,30}(?:内容|草稿|构思|模板|文档|手册|报告|指南|方案|word)"
    r"|(?:接着|继续).{0,18}(?:写|生成|输出|完成|编写|处理)"
    r"|(?:调研|研究|分析|检索)(?:结果|结论)|讨论(?:过)?的?(?:内容|结果|东西)", re.I,
)
_EXPORT = re.compile(
    r"下载|导出|交付成|写到.{0,10}(?:word|docx)"
    # "放到 word 文档中给我"：交付物在前、给/发在后
    r"|(?:整理|放|存|转|做|写)(?:到|成|为).{0,10}(?:word|docx|文档)"
    r"|(?:word|docx|文档|报告).{0,8}(?:给(?:到)?我|发(?:给)?我|下载|导出)"
    # 指代前文 + 轻度交付动词（"把上面的内容生成一个文档""把这段话变成 word"）
    r"|(?:上[面文述]|之前|此前|刚才|这[些段份篇]|以上).{0,20}"
    r"(?:生成|整理|做成|变成|保存|输出|转|写).{0,12}(?:word|docx|文档|报告|文章)", re.I,
)
_REVISE = re.compile(r"重写|改写|修改|重做|润色|改版|新增|加上|增加|扩写|细化|补写|补上|补充|补全|"
                     r"插入|追加|扩充|加入|调整|更新|删掉|删除|改成|翻译|改为|完善|精简|写得更|"
                     r"(?:写|讲|展开)详细", re.I)
_TEMPLATE = re.compile(r"模板|[以按照].{0,10}范本|参照.{0,12}(?:结构|格式|版式)", re.I)
_EXAMPLE = re.compile(
    r"(?:自己发挥|自行发挥|自行补全|虚构|构造).{0,38}(?:测试|示例|样例|演示)|"
    r"(?:测试|示例|样例|演示).{0,38}(?:自己发挥|自行发挥|自行补全|虚构|构造)", re.S,
)
_HEADING = re.compile(r"^\s*#{1,4}\s+(.+)$", re.M)
_CONTINUE_ONLY = re.compile(
    r"^\s*(?:请)?(?:继续|接着|往下|go on|continue)[呀吧。！\s]*$"
    r"|^\s*(?:请)?(?:继续|接着).{0,12}(?:写|生成|输出|完成|编写|处理)", re.I,
)
_QUESTION = re.compile(r"^(?:好[的，！\s]*，?)?(?:还需|请提供|请补充|我需要.{0,10}信息|I need more information)", re.I)
_TOPIC_CHANGE = re.compile(r"(?:换个|聊个|另一个|切换|新)话题|先(?:聊|问|讨论)别的")
_UNRELATED_QUESTION = re.compile(r"[?？]$|^(?:请问|你知道|你是谁|你用的什么|如何|怎样|为什么)")
_DRAFT_LIMIT = 160_000


def _main_text(message: dict) -> str:
    return str(message.get("content") or "").split("\n\n[附件]", 1)[0].strip()


def _is_substantive(content: str) -> bool:
    headings = list(_HEADING.finditer(content))
    return len(content) >= 280 and len(headings) >= 2 and not _QUESTION.search(content[:180])


def _substantive_draft(messages: list[dict], *, stitch: bool = False) -> str:
    """拼接最近一段连续正文：同一报告常分多段生成，中间只隔「继续」类指令。

    stitch=True 时，也允许跨越“针对前文补充/修订”的追问，把完整报告与后续
    更新段拼在一起（例如先出全文，再补充第 4 部分数据）。避免文档任务只拿到
    最后一段更新而丢掉前半篇。
    """
    end = next((index for index in range(len(messages) - 1, -1, -1)
                if messages[index].get("role") == "assistant"
                and _is_substantive(_main_text(messages[index]))), None)
    if end is None:
        return ""
    parts: list[str] = []
    index: int | None = end
    while index is not None:
        content = _main_text(messages[index])
        heading = _HEADING.search(content)
        parts.insert(0, content[heading.start():].strip() if heading else content.strip())
        previous = None
        for cursor in range(index - 1, -1, -1):
            item = messages[cursor]
            if item.get("role") == "assistant":
                previous = cursor
                break
            # 只跨越「继续/接着写」这类无新信息指令；stitch 时也跨越指代前文的补充/修订
            if item.get("role") == "user" and not (
                _CONTINUE_ONLY.search(_main_text(item))
                or (stitch and references_prior_content(_main_text(item)))
            ):
                break
        index = previous if previous is not None and _is_substantive(
            _main_text(messages[previous])) else None
    return "\n\n".join(parts)


def is_template_reference(message: str) -> bool:
    return bool(_TEMPLATE.search(message))


def document_turn(message: str, history: list[dict], route: dict) -> tuple[dict, list[dict], str | None]:
    """返回附带上下文的路由、需核验的历史附件和可能有用户补充资料的原 Run。"""
    if route.get("task_mode") not in ("auto", "document_generate"):
        return route, [], None
    latest = message.strip()
    followup = bool(_FOLLOW_UP.search(latest))
    export = bool(_EXPORT.search(latest) and not _REVISE.search(latest))
    # 指代本会话已有内容（如「将调研结果…」）：即使不是"下载/导出"措辞，也必须注入前文正文
    reference = references_prior_content(latest)
    if not (followup or export or reference or route.get("task_type") == "document_generate"):
        return route, [], None
    anchor = None
    recent = [item for item in history[-24:] if isinstance(item, dict)]
    if followup or export or reference:
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
    # 明确导出或指代前文时才回溯最近连续正文；裸「继续」必须依赖文档锚点，避免误吞无关内容
    draft = _substantive_draft(related or (recent if (export or reference) else []),
                               stitch=bool(export or reference))
    # 只有明确的交付意图、模板引用或延续既有文档生成任务时才转文档生成。
    # 单纯追问/修订调研结论（如「第 4 部分补充数据后重新发给我」）应留在对话中回答，
    # 否则会把一次研究会话误判成文档生成，连带改变后续轮次意图。
    if (route.get("task_type") == "general_chat" and not export
            and anchor is None and not is_template_reference(latest)):
        return route, [], None
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
        # 明确交付意图 + 已有连续正文 = 直接把前文成稿为 Word，不再向用户索要正文资料
        "export_existing": bool(export and draft and len(draft) <= _DRAFT_LIMIT
                                and not is_template_reference(latest)),
    }
    anchor_run = related[0].get("document_run_id") if related else None
    return {**route, "document_context": context}, inherited, anchor_run
