"""ZIP 打包下载的共享响应构造（工作区文件包 / Run 交付包共用）。

产物以 ``SpooledTemporaryFile`` 缓冲生成（小包在内存、大包落盘），
再分块流式输出，避免大工作区一次性占用内存；文件名按 RFC5987 编码。
"""
from __future__ import annotations

import os
import tempfile
from typing import Iterable
from urllib.parse import quote
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi.responses import StreamingResponse

_CHUNK = 64 * 1024


def _iter_chunks(stream: tempfile.SpooledTemporaryFile):
    try:
        while chunk := stream.read(_CHUNK):
            yield chunk
    finally:
        stream.close()


def zip_stream_response(entries: Iterable[tuple[str, str]], filename: str) -> StreamingResponse:
    """把 (zip 内条目名, 源文件绝对路径) 集合打包为流式下载响应。

    个别源文件读取失败时跳过该条目，不中断整体打包。
    """
    buffer = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for entry, path in entries:
            try:
                archive.write(path, entry)
            except OSError:
                continue
    buffer.seek(0, os.SEEK_END)
    size = buffer.tell()
    buffer.seek(0)
    return StreamingResponse(
        _iter_chunks(buffer),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "Content-Length": str(size),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
