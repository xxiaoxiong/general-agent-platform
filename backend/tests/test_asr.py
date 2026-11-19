"""语音转写的 API、HTTP 协议和录音解码回归。"""

import asyncio
import io
import shutil
import subprocess
import wave
from unittest.mock import AsyncMock
from uuid import UUID

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, ConnectError, MockTransport, ReadTimeout, Response
from pydantic import ValidationError
import pytest

from app.api.deps import get_current_user
from app.api.v1 import asr
from app.core.config import Settings
from app.services import asr_service


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(asr, "settings", Settings(
        ASR_BASE_URL="http://asr:8000", ASR_MAX_UPLOAD_MB=1, ASR_MAX_AUDIO_SECONDS=20,
    ))
    application = FastAPI()
    application.include_router(asr.router, prefix="/api/v1")
    return application


def _authenticated(app):
    app.dependency_overrides[get_current_user] = lambda: object()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_asr_routes_require_authentication(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/asr/capabilities")).status_code == 401
        result = await client.post("/api/v1/asr/transcribe")
    assert result.status_code == 401


@pytest.mark.asyncio
async def test_capabilities_and_transcription(app, monkeypatch):
    decode = AsyncMock(return_value=b"normalized wave recording")
    transcribe = AsyncMock(return_value="今天继续完成报告。")
    monkeypatch.setattr(asr, "normalize_audio", decode)
    monkeypatch.setattr(asr, "transcribe_wav", transcribe)
    async with _authenticated(app) as client:
        capabilities = await client.get("/api/v1/asr/capabilities")
        result = await client.post("/api/v1/asr/transcribe", files={
            "file": ("utterance.webm", b"recording", "audio/webm;codecs=opus"),
        })

    assert capabilities.json() == {"enabled": True, "max_duration_seconds": 20, "max_file_bytes": 1024 ** 2}
    assert result.status_code == 200
    assert result.json() == {"text": "今天继续完成报告。"}
    assert result.headers["cache-control"] == "no-store"
    decode.assert_awaited_once_with(b"recording", "audio/webm;codecs=opus", 20)
    transcribe.assert_awaited_once_with(b"normalized wave recording", asr.settings)


@pytest.mark.asyncio
async def test_disabled_transcription_leaves_settings_accessible(app, monkeypatch):
    monkeypatch.setattr(asr, "settings", Settings(ASR_BASE_URL=""))
    async with _authenticated(app) as client:
        assert (await client.get("/api/v1/asr/capabilities")).json()["enabled"] is False
        result = await client.post("/api/v1/asr/transcribe")
    assert result.status_code == 503
    assert result.json()["detail"]["code"] == "asr_unavailable"
    assert result.headers["cache-control"] == "no-store"
    assert UUID(result.headers["x-request-id"]).version == 4


@pytest.mark.asyncio
async def test_upload_rejects_file_size_and_unknown_chunks_before_decoding(app, monkeypatch):
    decoder = AsyncMock()
    monkeypatch.setattr(asr, "normalize_audio", decoder)

    async def oversized_stream():
        yield b"--rec\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.webm\"\r\n"
        yield b"Content-Type: audio/webm\r\n\r\n"
        for _ in range(20):
            yield b"x" * (64 * 1024)

    async with _authenticated(app) as client:
        for size in (1024 ** 2 + 1, 2 * 1024 ** 2):
            result = await client.post("/api/v1/asr/transcribe", files={
                "file": ("a.webm", b"x" * size, "audio/webm"),
            })
            assert result.status_code == 413
            assert result.json()["detail"]["code"] == "file_too_large"
        result = await client.post(
            "/api/v1/asr/transcribe",
            headers={"Content-Type": "multipart/form-data; boundary=rec"},
            content=oversized_stream(),
        )
    assert result.status_code == 413
    assert result.json()["detail"]["code"] == "file_too_large"
    decoder.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "files, status, code",
    [
        ({"file": ("a.txt", b"text", "text/plain")}, 415, "unsupported_format"),
        ({"file": ("a.wav", b"", "audio/wav")}, 400, "invalid_audio"),
        ({"another": ("b.wav", b"recording", "audio/wav")}, 400, "invalid_audio"),
        ([("file", ("a.wav", b"abc", "audio/wav")), ("file", ("b.wav", b"def", "audio/wav"))], 400, "invalid_audio"),
    ],
)
async def test_rejects_bad_upload(app, files, status, code):
    async with _authenticated(app) as client:
        result = await client.post("/api/v1/asr/transcribe", files=files)
    assert result.status_code == status
    assert result.json()["detail"]["code"] == code


@pytest.fixture
def mock_asr_http(monkeypatch):
    def install(handler):
        def client_factory(**options):
            assert options["trust_env"] is False
            return AsyncClient(transport=MockTransport(handler), **options)
        monkeypatch.setattr(asr_service.httpx, "AsyncClient", client_factory)
    return install


@pytest.mark.asyncio
async def test_real_http_adapter_uses_expected_upstream_contract(mock_asr_http):
    def handler(request):
        assert str(request.url) == "http://asr:8000/v1/audio/transcriptions"
        assert request.method == "POST"
        assert request.headers.get("Accept-Encoding") == "identity"
        assert b'name="model"\r\n\r\nsensevoice' in request.content
        assert b'name="response_format"\r\n\r\njson' in request.content
        assert b'name="file"; filename="dictation.wav"\r\nContent-Type: audio/wav\r\n\r\nRIFFexample' in request.content
        return Response(200, json={"text": "  这是语音验收。\n "})

    mock_asr_http(handler)
    result = await asr_service.transcribe_wav(b"RIFFexample", Settings(ASR_BASE_URL="http://asr:8000/"))
    assert result == "这是语音验收。"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "upstream, status, code",
    [
        (Response(200, json={"text": " "}), 422, "no_speech"),
        (Response(200, json={"text": 1}), 502, "asr_invalid_response"),
        (Response(200, content=b"a" * (128 * 1024 + 1)), 502, "asr_invalid_response"),
        (Response(422, text="upstream private content"), 400, "invalid_audio"),
        (Response(429, text="upstream private content"), 429, "asr_busy"),
        (Response(500, text="upstream private content"), 503, "asr_unavailable"),
        (ConnectError("internal wiring"), 503, "asr_unavailable"),
        (ReadTimeout("internal wiring"), 504, "asr_timeout"),
    ],
)
async def test_maps_upstream_failures_without_sensitive_details(mock_asr_http, upstream, status, code):
    def handler(_):
        if isinstance(upstream, Exception):
            raise upstream
        return upstream

    mock_asr_http(handler)
    with pytest.raises(asr_service.TranscriptionError) as captured:
        await asr_service.transcribe_wav(b"wave data", Settings(ASR_BASE_URL="http://asr:8000"))
    error = captured.value
    assert (error.status, error.code) == (status, code)
    assert "internal wiring" not in str(error)
    assert "upstream private content" not in str(error)


def test_settings_validate_asr_url_and_positive_limits():
    Settings(ASR_BASE_URL="http://asr:8000").validate_startup()
    with pytest.raises(RuntimeError, match="ASR_BASE_URL"):
        Settings(ASR_BASE_URL="http://user:secret@asr:8000").validate_startup()
    with pytest.raises(RuntimeError, match="ASR_MODEL"):
        Settings(ASR_BASE_URL="http://asr:8000", ASR_MODEL=" ").validate_startup()
    with pytest.raises(ValidationError, match="ASR_MAX_UPLOAD_MB"):
        Settings(ASR_MAX_UPLOAD_MB=17)


def _wav(seconds: int) -> bytes:
    sample_rate = 8000
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, sample_rate, 0, "NONE", ""))
        wav.writeframes(b"\x70\x01" * sample_rate * seconds)
    return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="需要安装 FFmpeg 进行真实解码回归")
@pytest.mark.parametrize(
    "encode, mime",
    [
        (None, "audio/wav"),
        (["-c:a", "libopus", "-f", "webm"], "audio/webm;codecs=opus"),
        (["-c:a", "libopus", "-f", "ogg"], "audio/ogg"),
        (["-c:a", "aac", "-movflags", "frag_keyframe+empty_moov", "-f", "mp4"], "audio/mp4"),
    ],
)
async def test_decodes_real_audio_and_measures_samples(encode, mime):
    original = _wav(2)
    if encode:
        # 输出到不可跳转的管道，WebM 不带预先计算的总时长，和浏览器录音一致。
        encoder = await asyncio.to_thread(
            subprocess.run, ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "wav", "-i", "pipe:0",
                             *encode, "pipe:1"],
            input=original, stdout=subprocess.PIPE, timeout=10, check=True,
        )
        original = encoder.stdout

    normalized = await asr_service.normalize_audio(original, mime, 3)
    with wave.open(io.BytesIO(normalized), "rb") as wav:
        assert wav.getframerate() == 16000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert 31_000 <= wav.getnframes() <= 36_000
    with pytest.raises(asr_service.TranscriptionError, match="不能超过 1 秒"):
        await asr_service.normalize_audio(original, mime, 1)
    with pytest.raises(asr_service.TranscriptionError, match="无法读取录音"):
        await asr_service.normalize_audio(b"corrupt unknown recording", mime, 3)
