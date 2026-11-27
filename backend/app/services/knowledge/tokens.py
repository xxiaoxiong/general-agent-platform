"""Token 计量与边界感知切分（任务书 §5）。

不引入 tiktoken 等重依赖：采用确定性近似计量——
- CJK 字符按 1 Token；
- 连续拉丁字母/数字按每 4 字符 1 Token（向上取整）；
- 标点与空白按 1/2 Token 折算，取整。
用途仅为 Chunk 大小控制与上下文预算，不用于计费。
"""
from __future__ import annotations

import re
from typing import Iterable, List, Tuple

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_LATIN_RE = re.compile(r"[A-Za-z0-9_]+")
_WS_RE = re.compile(r"\s+")
_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+(?=[A-Z0-9])|\n+")


def count_tokens(text: str) -> int:
    """确定性 Token 近似计量。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    latin_chars = sum(len(m.group(0)) for m in _LATIN_RE.finditer(text))
    other = len(_WS_RE.sub("", _CJK_RE.sub("", _LATIN_RE.sub("", text))))
    return int(cjk + (latin_chars + 3) // 4 + (other + 1) // 2)


def split_sentences(text: str) -> List[str]:
    """按中英文句末标点与换行切句（保留标点），过滤空段。"""
    parts = [p.strip() for p in _SENTENCE_RE.split(text) if p and p.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def _hard_split(text: str, max_tokens: int) -> List[str]:
    """超长单句按字符滑窗硬切（保证有限步长，overlap < max）。"""
    if max_tokens <= 0:
        return [text]
    pieces: List[str] = []
    start = 0
    length = len(text)
    # 每 Token 约 1~2 字符：用比例估算窗口，保证不会死循环
    window = max(1, max_tokens * 2)
    while start < length:
        end = min(length, start + window)
        piece = text[start:end]
        if piece.strip():
            pieces.append(piece)
        if end >= length:
            break
        start = max(start + 1, end - max(1, window // 8))
    return pieces


def split_text_windows(
    text: str,
    *,
    max_tokens: int,
    overlap_tokens: int = 0,
) -> List[str]:
    """按 Token 上限 + 句边界把文本切成窗口。

    约束：``max_tokens > 0`` 且 ``0 <= overlap_tokens < max_tokens``
    （非法配置在 API / 启动校验阻断，这里再做一次防御）。
    """
    max_tokens = int(max_tokens)
    overlap_tokens = int(overlap_tokens)
    if max_tokens <= 0:
        raise ValueError("max_tokens 必须大于 0")
    if overlap_tokens < 0 or overlap_tokens >= max_tokens:
        raise ValueError("overlap_tokens 必须 >= 0 且小于 max_tokens")
    text = (text or "").strip()
    if not text:
        return []
    if count_tokens(text) <= max_tokens:
        return [text]

    sentences: List[str] = []
    for sentence in split_sentences(text):
        if count_tokens(sentence) > max_tokens:
            sentences.extend(_hard_split(sentence, max_tokens))
        else:
            sentences.append(sentence)

    windows: List[str] = []
    current: List[str] = []
    current_tokens = 0

    def _flush() -> None:
        nonlocal current, current_tokens
        if current:
            windows.append("".join(current).strip())
        current = []
        current_tokens = 0

    for sentence in sentences:
        tokens = count_tokens(sentence)
        if current and current_tokens + tokens > max_tokens:
            _flush()
            # Overlap：从已切好的窗口尾部回填若干句（不超过 overlap 预算）
            if overlap_tokens > 0 and windows:
                tail: List[str] = []
                tail_tokens = 0
                for prev in reversed(split_sentences(windows[-1])):
                    prev_tokens = count_tokens(prev)
                    if tail_tokens + prev_tokens > overlap_tokens:
                        break
                    tail.insert(0, prev)
                    tail_tokens += prev_tokens
                current = tail
                current_tokens = tail_tokens
        current.append(sentence)
        current_tokens += tokens
    _flush()
    return [w for w in windows if w.strip()]


def group_segments(
    segments: Iterable[Tuple[str, int]],
    *,
    max_tokens: int,
) -> List[List[str]]:
    """把 ``(文本, Token 数)`` 段按上限分组（不拆分单段，超限段单独成组）。"""
    groups: List[List[str]] = []
    current: List[str] = []
    current_tokens = 0
    for text, tokens in segments:
        if current and current_tokens + tokens > max_tokens:
            groups.append(current)
            current = []
            current_tokens = 0
        current.append(text)
        current_tokens += tokens
    if current:
        groups.append(current)
    return groups
