"""统一时间上下文：给模型注入带时区的"当前时间"。

模型的训练知识有截止时间，时效判断必须以运行时刻为基准；容器默认 UTC，
直接 ``date.today()`` 会在北京时间 0:00-8:00 差一天，因此时区显式可配。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import settings

_WEEK = "一二三四五六日"


def current_datetime() -> datetime:
    """当前时间（按 AGENT_TIMEZONE 解析，非法时区回退系统本地时间）。"""
    name = (getattr(settings, "AGENT_TIMEZONE", "") or "").strip()
    if name:
        try:
            return datetime.now(ZoneInfo(name))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now()


def current_time_text() -> str:
    """供提示词使用的当前时间描述（含星期与时区）。"""
    now = current_datetime()
    zone = now.tzname() or ""
    suffix = f"，{zone}" if zone else ""
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')}（星期{_WEEK[now.weekday()]}{suffix}）"
