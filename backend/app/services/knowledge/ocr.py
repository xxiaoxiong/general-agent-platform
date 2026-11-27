"""OCR（扫描 PDF / 图片）：RapidOCR（PaddleOCR ONNX 推理，中英混合）。

设计要点（任务书 §3.3）：
- OCR 是可选能力：未安装 rapidocr 或显式关闭时，图片/扫描页明确标记失败
  或 partial，不按文本兜底冒充成功；
- OCR 有独立并发保护（引擎非线程安全，串行执行）；
- 保存 OCR 页码与平均置信度，低质量页面标记 needs_review。
"""
from __future__ import annotations

import io
import logging
import threading
from dataclasses import dataclass, field
from typing import List, Optional

from app.core.config import settings

from .model import OCRUnavailableError

logger = logging.getLogger(__name__)

_ENGINE = None
_ENGINE_ERROR: Optional[str] = None
_ENGINE_LOCK = threading.Lock()
_OCR_LOCK = threading.Lock()


@dataclass
class OCRResult:
    text: str
    lines: List[str] = field(default_factory=list)
    mean_score: float = 0.0
    engine: str = "rapidocr"

    @property
    def low_quality(self) -> bool:
        return self.mean_score < float(settings.KNOWLEDGE_OCR_MIN_SCORE or 0.5)


def ocr_available() -> bool:
    """OCR 能力是否可用（不触发模型加载）。"""
    if not settings.KNOWLEDGE_OCR_ENABLED:
        return False
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _get_engine():
    global _ENGINE, _ENGINE_ERROR
    if _ENGINE is not None:
        return _ENGINE
    if _ENGINE_ERROR is not None:
        raise OCRUnavailableError(f"OCR 引擎不可用：{_ENGINE_ERROR}")
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            return _ENGINE
        if not settings.KNOWLEDGE_OCR_ENABLED:
            _ENGINE_ERROR = "KNOWLEDGE_OCR_ENABLED=false"
            raise OCRUnavailableError("OCR 已关闭（KNOWLEDGE_OCR_ENABLED=false）")
        try:
            from rapidocr_onnxruntime import RapidOCR

            _ENGINE = RapidOCR()
            logger.info("RapidOCR 引擎初始化完成（语言：%s）", settings.KNOWLEDGE_OCR_LANGUAGES)
        except Exception as exc:  # noqa: BLE001
            _ENGINE_ERROR = str(exc)
            raise OCRUnavailableError(f"OCR 引擎初始化失败：{exc}") from exc
    return _ENGINE


def ocr_image_bytes(data: bytes) -> OCRResult:
    """对图片二进制执行 OCR（PNG / JPG / TIFF 等，由 PIL 解码）。"""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            pixels = int(width) * int(height)
            limit = int(settings.KNOWLEDGE_MAX_IMAGE_PIXELS or 0)
            if limit > 0 and pixels > limit:
                raise OCRUnavailableError(
                    f"图片像素数 {pixels} 超过上限 {limit}，拒绝 OCR（防资源耗尽）"
                )
            rgb = image.convert("RGB")
    except OCRUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OCRUnavailableError(f"图片解码失败：{exc}") from exc

    engine = _get_engine()
    import numpy as np

    with _OCR_LOCK:
        try:
            result, _elapse = engine(np.asarray(rgb))
        except Exception as exc:  # noqa: BLE001
            raise OCRUnavailableError(f"OCR 推理失败：{exc}") from exc

    lines: List[str] = []
    scores: List[float] = []
    for item in result or []:
        try:
            _box, text, score = item[0], item[1], item[2]
        except Exception:  # noqa: BLE001
            continue
        if text and str(text).strip():
            lines.append(str(text).strip())
            try:
                scores.append(float(score))
            except (TypeError, ValueError):
                pass
    mean_score = (sum(scores) / len(scores)) if scores else 0.0
    return OCRResult(
        text="\n".join(lines),
        lines=lines,
        mean_score=round(mean_score, 4),
    )


def ocr_image_file(path: str) -> OCRResult:
    with open(path, "rb") as fh:
        return ocr_image_bytes(fh.read())


def render_pdf_page_png(path: str, page_index: int, *, scale: float = 2.0) -> bytes:
    """把 PDF 指定页渲染为 PNG（扫描页 OCR 用）。"""
    try:
        import pypdfium2 as pdfium
    except Exception as exc:  # noqa: BLE001
        raise OCRUnavailableError(
            "扫描 PDF OCR 需要 pypdfium2（pip install pypdfium2），当前不可用"
        ) from exc
    with pdfium.PdfDocument(path) as pdf:
        if page_index < 0 or page_index >= len(pdf):
            raise OCRUnavailableError(f"PDF 页码越界：{page_index + 1}/{len(pdf)}")
        page = pdf[page_index]
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
