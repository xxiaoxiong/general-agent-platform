"""录音解码与本地 ASR 转发。上传文件和转写内容仅在本次请求内使用。"""

import asyncio
import io
import json
import subprocess
import wave

import httpx

from app.core.config import Settings


_DEMUXERS = {
    "audio/webm": "matroska",
    "audio/ogg": "ogg",
    "application/ogg": "ogg",
    "audio/mp4": "mov",
    "audio/x-m4a": "mov",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
}
_SAMPLE_RATE = 16000
_SAMPLE_BYTES = 2
_DECODE_TIMEOUT_SECONDS = 15
_RESPONSE_MAX_BYTES = 128 * 1024


class TranscriptionError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


async def _decode_pcm(command: list[str], audio: bytes) -> tuple[int, bytes]:
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
    except NotImplementedError:
        # Windows Selector 事件循环不能启动异步子进程，由受超时约束的线程等待。
        completed = await asyncio.to_thread(
            subprocess.run, command, input=audio, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=_DECODE_TIMEOUT_SECONDS, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return completed.returncode, completed.stdout

    try:
        pcm, _ = await asyncio.wait_for(process.communicate(audio), timeout=_DECODE_TIMEOUT_SECONDS)
        return process.returncode or 0, pcm
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.communicate()


async def normalize_audio(audio: bytes, content_type: str, max_duration_seconds: int) -> bytes:
    """用实际解码后的采样数计算时长，兼容没有时长元数据的浏览器录音。"""
    demuxer = _DEMUXERS.get(content_type.partition(";")[0].strip().lower())
    if demuxer is None:
        raise TranscriptionError(415, "unsupported_format", "请使用 WebM、Ogg、MP4 或 WAV 录音。")
    if not audio:
        raise TranscriptionError(400, "invalid_audio", "录音为空，请重新录音。")

    # 只读取标准输入，采样数只需多读一秒即可判定超限，限制损坏文件的解码成本。
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
        "-max_alloc", str(64 * 1024 * 1024), "-threads", "1", "-filter_threads", "1",
        "-protocol_whitelist", "pipe", "-f", demuxer, "-i", "pipe:0",
        "-map", "0:a:0", "-ac", "1", "-ar", str(_SAMPLE_RATE),
        "-t", str(max_duration_seconds + 1), "-f", "s16le", "pipe:1",
    ]
    try:
        returncode, pcm = await _decode_pcm(command, audio)
    except (TimeoutError, subprocess.TimeoutExpired):
        raise TranscriptionError(504, "asr_timeout", "录音解码超时，请重新录制一段较短的语音。") from None
    except OSError:
        raise TranscriptionError(503, "asr_unavailable", "录音解码暂不可用，请稍后重试。") from None

    if returncode or not pcm or len(pcm) % _SAMPLE_BYTES:
        raise TranscriptionError(400, "invalid_audio", "无法读取录音，请重新录音。")
    if len(pcm) > max_duration_seconds * _SAMPLE_RATE * _SAMPLE_BYTES:
        raise TranscriptionError(422, "duration_exceeded", f"录音不能超过 {max_duration_seconds} 秒。")

    output = io.BytesIO()
    with wave.open(output, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(_SAMPLE_BYTES)
        recording.setframerate(_SAMPLE_RATE)
        recording.writeframes(pcm)
    return output.getvalue()


def _validate_upstream_status(status: int) -> None:
    if 200 <= status < 300:
        return
    if status in {400, 415, 422}:
        raise TranscriptionError(400, "invalid_audio", "识别服务无法读取录音，请重录。")
    if status == 429:
        raise TranscriptionError(429, "asr_busy", "语音识别忙，请稍后重试。")
    if status == 504:
        raise TranscriptionError(504, "asr_timeout", "语音识别超时，请稍后重试。")
    raise TranscriptionError(503, "asr_unavailable", "语音识别服务暂不可用，请稍后重试。")


async def transcribe_wav(wav: bytes, settings: Settings) -> str:
    url = f"{settings.ASR_BASE_URL.strip().rstrip('/')}/v1/audio/transcriptions"
    invalid_response = TranscriptionError(502, "asr_invalid_response", "语音识别返回了无法读取的结果，请稍后重试。")
    body = bytearray()
    try:
        # HTTPX 的超时限制单次网络操作，外层再限定整个转写请求的墙钟时间。
        async with asyncio.timeout(settings.ASR_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(timeout=settings.ASR_TIMEOUT_SECONDS, trust_env=False) as client:
                async with client.stream(
                    "POST",
                    url,
                    data={"model": settings.ASR_MODEL.strip(), "response_format": "json"},
                    files={"file": ("dictation.wav", wav, "audio/wav")},
                    headers={"Accept-Encoding": "identity"},
                ) as response:
                    _validate_upstream_status(response.status_code)
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise invalid_response
                    async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
                        body.extend(chunk)
                        if len(body) > _RESPONSE_MAX_BYTES:
                            raise invalid_response
    except (TimeoutError, httpx.TimeoutException):
        raise TranscriptionError(504, "asr_timeout", "语音识别超时，请稍后重试。") from None
    except (httpx.RequestError, httpx.InvalidURL):
        raise TranscriptionError(503, "asr_unavailable", "语音识别服务暂不可用，请稍后重试。") from None

    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise invalid_response from None
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        raise invalid_response
    text = data["text"].strip()
    if not text:
        raise TranscriptionError(422, "no_speech", "没有识别到语音，请靠近麦克风后重录。")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise invalid_response from None
    return text
