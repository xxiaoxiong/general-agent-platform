"""当前登录用户的录音上传与转写接口。"""

import asyncio
from collections.abc import AsyncGenerator
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from python_multipart.multipart import parse_options_header
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.logging import get_logger
from app.services.asr_service import TranscriptionError, normalize_audio, transcribe_wav


router = APIRouter(prefix="/asr", tags=["语音转写"], dependencies=[Depends(get_current_user)])
logger = get_logger("asr")
_slots = asyncio.Semaphore(2)
_MULTIPART_OVERHEAD_BYTES = 64 * 1024
_UPLOAD_TIMEOUT_SECONDS = 30


class Capabilities(BaseModel):
    enabled: bool
    max_duration_seconds: int
    max_file_bytes: int


class Transcription(BaseModel):
    text: str


class _UploadTooLarge(MultiPartException):
    pass


class _InMemoryMultipart(MultiPartParser):
    # 整个请求还受流式读取上限约束，因此原始录音不会写入临时文件。
    spool_max_size = 17 * 1024 * 1024


def _file_too_large() -> TranscriptionError:
    return TranscriptionError(413, "file_too_large", f"录音不能超过 {settings.ASR_MAX_UPLOAD_MB} MB。")


async def _bounded_stream(request: Request, limit: int) -> AsyncGenerator[bytes, None]:
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise _UploadTooLarge("录音请求超过大小限制。")
        yield chunk


async def _read_recording(request: Request) -> tuple[bytes, str]:
    limit = settings.asr_max_file_bytes
    body_limit = limit + _MULTIPART_OVERHEAD_BYTES
    try:
        if int(request.headers.get("content-length", "0")) > body_limit:
            raise _file_too_large()
    except ValueError:
        raise TranscriptionError(400, "invalid_audio", "录音上传请求无效，请重新录音。") from None

    media_type, options = parse_options_header(request.headers.get("content-type", ""))
    if media_type != b"multipart/form-data":
        raise TranscriptionError(415, "unsupported_format", "请上传录音文件。")
    if not 1 <= len(options.get(b"boundary", b"")) <= 70:
        raise TranscriptionError(400, "invalid_audio", "录音上传请求无效，请重新录音。")

    parser = _InMemoryMultipart(request.headers, _bounded_stream(request, body_limit), max_files=1, max_fields=0)
    try:
        async with asyncio.timeout(_UPLOAD_TIMEOUT_SECONDS):
            form = await parser.parse()
        try:
            recording = form.get("file")
            if not isinstance(recording, UploadFile):
                raise TranscriptionError(400, "invalid_audio", "未收到录音文件，请重新录音。")
            if recording.size is not None and recording.size > limit:
                raise _file_too_large()
            audio = await recording.read(limit + 1)
            if len(audio) > limit:
                raise _file_too_large()
            return audio, recording.content_type or ""
        finally:
            await form.close()
    except _UploadTooLarge:
        raise _file_too_large() from None
    except (MultiPartException, ClientDisconnect):
        raise TranscriptionError(400, "invalid_audio", "录音上传请求无效，请重新录音。") from None
    except TimeoutError:
        raise TranscriptionError(504, "asr_timeout", "录音上传超时，请检查连接后重试。") from None


@router.get("/capabilities")
async def get_capabilities(response: Response) -> Capabilities:
    response.headers["Cache-Control"] = "no-store"
    return Capabilities(
        enabled=settings.asr_enabled,
        max_duration_seconds=settings.ASR_MAX_AUDIO_SECONDS,
        max_file_bytes=settings.asr_max_file_bytes,
    )


@router.post(
    "/transcribe",
    openapi_extra={"requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
        "type": "object", "required": ["file"], "properties": {"file": {"type": "string", "format": "binary"}},
    }}}}},
)
async def transcribe(request: Request, response: Response) -> Transcription:
    try:
        if not settings.asr_enabled:
            raise TranscriptionError(503, "asr_unavailable", "语音输入尚未启用，可继续使用文本输入。")
        if _slots.locked():
            raise TranscriptionError(429, "asr_busy", "语音识别忙，请稍后重试。")
        async with _slots:
            audio, content_type = await _read_recording(request)
            wav = await normalize_audio(audio, content_type, settings.ASR_MAX_AUDIO_SECONDS)
            text = await transcribe_wav(wav, settings)
    except TranscriptionError as error:
        headers = {"Cache-Control": "no-store"}
        if error.status >= 500:
            request_id = getattr(request.state, "request_id", None) or uuid4().hex
            logger.warning("语音转写失败", code=error.code, request_id=request_id)
            headers["X-Request-ID"] = request_id
        raise HTTPException(
            error.status,
            detail={"code": error.code, "message": error.message},
            headers=headers,
        ) from None

    response.headers["Cache-Control"] = "no-store"
    return Transcription(text=text)
