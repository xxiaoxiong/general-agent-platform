"""区分软件实施、软件讨论和软件相关的文档交付。"""

import re
from typing import Literal


ProjectScope = Literal["fullstack", "frontend", "backend"]

_ADVICE = re.compile(
    r"^\s*(?:(?:请问|请)\s*)?(?:如何|怎么|怎样|为什么|解释|介绍|比较|对比|分析|评价|review|explain|compare|how\b)"
    r"|只(?:要|给|提供|写).{0,8}(?:方案|设计|思路|建议|说明书|文档)|不(?:需要|用|必|要).{0,8}(?:实现|写代码|开发)"
    r"|\b(?:plan only|design only|no code|do not (?:implement|write code))\b",
    re.I,
)
_DOCUMENT_REQUEST = re.compile(
    r"(?:生成|交付|撰写|编写|输出|给我|写一份).{0,34}(?:Word|DOCX|项目方案|开发方案|项目报告|评估报告|项目总结|需求文档|设计文档|需求规格书|实施方案|技术文档|开发文档)"
    r"|\b(?:write|draft|create) (?:a |an )?(?:proposal|specification|design document)\b",
    re.I,
)
_BUILD = re.compile(
    r"构建|搭建|做出|做一[个套款]|做个|开发|实现|编写|创建|生成|制作|写一[个套款]|写个|交付|给我一个"
    r"|\b(?:build|create|implement|develop|make|generate|deliver|scaffold)\b",
    re.I,
)
_FULLSTACK = re.compile(r"前[后後]端|全栈|前端.{0,16}后端|后端.{0,16}前端|\bfull[\s-]?stack\b|\bfrontend.{0,24}backend\b", re.I)
_FRONTEND = re.compile(r"前端|网页|网站|页面|交互原型|web\s*ui|front[\s-]?end|website|landing page", re.I)
_BACKEND = re.compile(r"后端|接口服务|API 服务|服务端|back[\s-]?end|rest api|api server", re.I)
_SOFTWARE = re.compile(r"项目|系统|应用|程序|仪表盘|管理平台|project|application|app\b|dashboard|service\b", re.I)
_SOURCE = re.compile(r"完整源[码碼]|打包.{0,6}下载|代码和测试|\bsource code\b", re.I)


def project_scope(message: str, task_mode: str, *, has_selected_document: bool = False) -> ProjectScope | None:
    if task_mode not in {"auto", "chat"} or has_selected_document or _ADVICE.search(message) or not _BUILD.search(message):
        return None
    if _DOCUMENT_REQUEST.search(message) and not _SOURCE.search(message):
        return None
    if _FULLSTACK.search(message):
        return "fullstack"
    if _FRONTEND.search(message):
        return "frontend"
    if _BACKEND.search(message):
        return "backend"
    if _SOFTWARE.search(message):
        return "fullstack"
    return None
