"""SandboxFilesBackend：把 DeepAgents 原生文件工具路由到项目 Sandbox 的 /workspace。

保证「文件工具和命令工具操作同一份文件」（任务书 §9.3 / Definition of Done #3）：
- 项目会话使用本后端；未归属项目的存量会话继续使用宿主机 FilesystemBackend；
- 读操作直接经 Runner Gateway -> workspace-agent；
- 写操作先获取 Workspace 写租约（同一 Workspace 串行写，任务书 §3.2）；
- 所有路径限定 /workspace 之下，宿主机路径与目录逃逸被拒绝。
"""
from __future__ import annotations

import base64
import logging
import re
from typing import Any, List, Optional

from deepagents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)

from app.services.sandbox import agent_client
from app.services.sandbox.common import SandboxError
from app.services.sandbox.write_lease import acquire_write_lease

logger = logging.getLogger("ai_plat.sandbox.backend")


def _virtual(path: str) -> str:
    raw = (path or "/").strip()
    if _HOST_PATH.match(raw):
        raise ValueError(
            f"路径 `{raw}` 是用户电脑上的路径，Sandbox 无法访问。"
            "请改写到项目工作区（如 `/workspace/报告.md`）。"
        )
    if raw.startswith("/workspace"):
        return raw
    return "/workspace" + ("" if raw.startswith("/") else "/") + raw


_HOST_PATH = re.compile(r"^(?:/Users/|/home/|/root/|/Documents/|[A-Za-z]:[\\/]|\\\\)")


class SandboxFilesBackend(BackendProtocol):
    def __init__(self, project_id: int, run_id: str, user_id: int) -> None:
        self.project_id = int(project_id)
        self.run_id = run_id
        self.user_id = int(user_id)

    async def _sandbox(self, min_role: str = "viewer"):
        from app.services.tools.sandbox_tools import resolve_sandbox

        return await resolve_sandbox(
            self.project_id, user_id=self.user_id, min_role=min_role,
            wait_seconds=25.0,
        )

    async def _lease_error(self, ref) -> Optional[str]:
        from app.core.database import AsyncSessionLocal

        try:
            async with AsyncSessionLocal() as db:
                token = await acquire_write_lease(
                    db, ref.workspace_id, f"run:{self.run_id}", user_id=self.user_id,
                )
                await db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sandbox 文件写入获取写租约失败: %s", exc)
            return f"获取项目写租约失败：{exc}"
        if token is None:
            return (
                "当前项目工作空间有另一个任务正在写入，为避免文件互相覆盖，"
                "本次写入被拒绝。请等待该任务完成或稍后重试。"
            )
        return None

    async def als(self, path: str) -> LsResult:
        try:
            ref = await self._sandbox()
            data = await agent_client.list_dir(ref.sandbox_id, _virtual(path))
        except SandboxError as exc:
            return LsResult(error=exc.message)
        except Exception as exc:  # noqa: BLE001
            return LsResult(error=f"列出目录失败: {exc}")
        entries = []
        for entry in data.get("entries") or []:
            entries.append({
                "path": entry.get("path"),
                "is_dir": bool(entry.get("is_dir")),
                "size": int(entry.get("size") or 0),
                "modified_at": entry.get("modified_at"),
            })
        return LsResult(entries=entries)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        if limit <= 0:
            return ReadResult(no_lines_requested=True)
        try:
            ref = await self._sandbox()
            data = await agent_client.read_file(
                ref.sandbox_id, _virtual(file_path), offset=max(0, offset), limit=limit,
            )
        except SandboxError as exc:
            return ReadResult(error=exc.message)
        except Exception as exc:  # noqa: BLE001
            return ReadResult(error=f"读取文件失败: {exc}")
        return ReadResult(
            file_data={
                "content": data.get("content") or "",
                "encoding": data.get("encoding") or "utf-8",
            },
            total_lines=data.get("total_lines"),
            start_line=data.get("start_line"),
            end_line=data.get("end_line"),
            next_offset=data.get("next_offset"),
        )

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        try:
            ref = await self._sandbox("operator")
            error = await self._lease_error(ref)
            if error:
                return WriteResult(error=error)
            data = await agent_client.write_file(ref.sandbox_id, _virtual(file_path), content)
        except SandboxError as exc:
            return WriteResult(error=exc.message)
        except Exception as exc:  # noqa: BLE001
            return WriteResult(error=f"写入文件失败: {exc}")
        return WriteResult(path=data.get("path") or _virtual(file_path))

    async def aedit(self, file_path: str, old_string: str, new_string: str,
                    replace_all: bool = False) -> EditResult:
        try:
            ref = await self._sandbox("operator")
            error = await self._lease_error(ref)
            if error:
                return EditResult(error=error)
            data = await agent_client.edit_file(
                ref.sandbox_id, _virtual(file_path), old_string, new_string, replace_all,
            )
        except SandboxError as exc:
            return EditResult(error=exc.message)
        except Exception as exc:  # noqa: BLE001
            return EditResult(error=f"编辑文件失败: {exc}")
        return EditResult(path=data.get("path") or _virtual(file_path),
                          occurrences=int(data.get("occurrences") or 0))

    async def adelete(self, file_path: str) -> DeleteResult:
        try:
            ref = await self._sandbox("operator")
            error = await self._lease_error(ref)
            if error:
                return DeleteResult(error=error)
            data = await agent_client.delete_path(ref.sandbox_id, _virtual(file_path))
        except SandboxError as exc:
            return DeleteResult(error=exc.message)
        except Exception as exc:  # noqa: BLE001
            return DeleteResult(error=f"删除失败: {exc}")
        return DeleteResult(path=data.get("path") or _virtual(file_path))

    async def agrep(self, pattern: str, path: Optional[str] = None,
                    glob: Optional[str] = None, *, max_count: Optional[int] = None) -> GrepResult:
        try:
            ref = await self._sandbox()
            data = await agent_client.grep_files(
                ref.sandbox_id, pattern, path=_virtual(path or "/workspace"),
                glob=glob, max_count=max_count or 200,
            )
        except SandboxError as exc:
            return GrepResult(error=exc.message)
        except Exception as exc:  # noqa: BLE001
            return GrepResult(error=f"搜索失败: {exc}")
        matches = [
            {
                "path": item.get("path"),
                "line": int(item.get("line") or 0),
                "text": item.get("text") or "",
            }
            for item in (data.get("matches") or [])
        ]
        return GrepResult(matches=matches, truncated=bool(data.get("truncated")))

    async def aglob(self, pattern: str, path: Optional[str] = None) -> GlobResult:
        try:
            ref = await self._sandbox()
            data = await agent_client.glob_files(
                ref.sandbox_id, pattern, path=_virtual(path or "/workspace"))
        except SandboxError as exc:
            return GlobResult(error=exc.message)
        except Exception as exc:  # noqa: BLE001
            return GlobResult(error=f"匹配文件失败: {exc}")
        matches = [
            {
                "path": item.get("path"),
                "is_dir": bool(item.get("is_dir")),
                "size": int(item.get("size") or 0),
                "modified_at": item.get("modified_at"),
            }
            for item in (data.get("matches") or [])
        ]
        return GlobResult(matches=matches, truncated=bool(data.get("truncated")),
                          truncation_reason=data.get("truncation_reason"))

    async def aupload_files(self, files: List[Any]) -> List[FileUploadResponse]:
        results: List[FileUploadResponse] = []
        ref = None
        lease_error = None
        try:
            ref = await self._sandbox("operator")
            lease_error = await self._lease_error(ref)
        except SandboxError as exc:
            return [FileUploadResponse(path=path, error=exc.message) for path, _ in files]
        if lease_error:
            return [FileUploadResponse(path=path, error=lease_error) for path, _ in files]
        for path, content in files:
            try:
                raw = content if isinstance(content, (bytes, bytearray)) else str(content).encode()
                try:
                    text = raw.decode("utf-8")
                    await agent_client.write_file(ref.sandbox_id, _virtual(path), text)
                except UnicodeDecodeError:
                    await agent_client.write_file(
                        ref.sandbox_id, _virtual(path),
                        base64.b64encode(raw).decode("ascii"), encoding="base64",
                    )
                results.append(FileUploadResponse(path=path))
            except Exception as exc:  # noqa: BLE001
                results.append(FileUploadResponse(path=path, error=str(exc)[:300]))
        return results

    async def adownload_files(self, paths: List[str]) -> List[FileDownloadResponse]:
        results: List[FileDownloadResponse] = []
        try:
            ref = await self._sandbox()
        except SandboxError as exc:
            return [FileDownloadResponse(path=path, error=exc.message) for path in paths]
        for path in paths:
            try:
                data = await agent_client.read_raw(ref.sandbox_id, _virtual(path))
                results.append(FileDownloadResponse(
                    path=path,
                    content=base64.b64decode(data.get("content_b64") or ""),
                ))
            except Exception as exc:  # noqa: BLE001
                results.append(FileDownloadResponse(path=path, error=str(exc)[:300]))
        return results


def build_sandbox_backend(project_id: int, run_id: str, user_id: int) -> SandboxFilesBackend:
    return SandboxFilesBackend(project_id=project_id, run_id=run_id, user_id=user_id)
