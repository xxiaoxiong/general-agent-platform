"""对主智能体已保存的源码执行隔离验收，并交付同一份源码快照。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import posixpath
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from app.assistant.task_contract import TaskContract
from app.assistant.verification import CheckResult
from app.core.config import settings

from .events import redact_secrets
from .project_verifier_protocol import (
    MAX_PROJECT_BYTES, MAX_PROJECT_FILES, MAX_PROJECT_FILE_BYTES, openapi_source, source_digest, valid_job_path,
)
from .team_project_verifier import verify_runtime


@dataclass
class ProjectReview:
    digest: str
    checks: list[CheckResult]
    artifacts: list[dict]
    unavailable: bool = False


def _snapshot(root: Path, artifacts: list[dict], openapi_path: str | None) -> tuple[str, dict[str, str]]:
    expected = {item["path"]: item for item in artifacts if not item["path"].casefold().endswith(".zip")}
    if not expected or len(expected) > MAX_PROJECT_FILES or sum(item["size"] for item in expected.values()) > MAX_PROJECT_BYTES:
        raise ValueError("工程源码为空或超出隔离验收容量")
    paths = [*expected, openapi_path] if openapi_path else list(expected)
    folder = posixpath.commonpath([posixpath.dirname(path) for path in paths])
    files = {}
    for path, item in sorted(expected.items()):
        name = posixpath.relpath(path, folder or ".")
        target = root / path
        if (not valid_job_path(path) or not valid_job_path(name) or target.resolve() != target
                or not target.is_file() or not 0 < item["size"] <= MAX_PROJECT_FILE_BYTES):
            raise ValueError(f"隔离验收无法读取源码文件 {path}")
        with target.open("rb") as stream:
            data = stream.read(MAX_PROJECT_FILE_BYTES + 1)
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError(f"源码已变化，需要重新验收 {path}")
        try:
            files[name] = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"隔离验收暂只支持 UTF-8 源码，请检查 {path}") from None
    return folder, files


def _workspace_source_digest(folder: str, files: dict[str, str]) -> str:
    return source_digest({posixpath.join(folder, name): text for name, text in files.items()})


async def workspace_project_digest(root: Path, artifacts: list[dict], openapi_path: str | None) -> str:
    """核对当前字节与会话内路径，不调用隔离验收服务。"""
    try:
        folder, files = await asyncio.to_thread(_snapshot, root, artifacts, openapi_path)
    except (OSError, ValueError, RuntimeError):
        return ""
    return _workspace_source_digest(folder, files)


def _save_output(root: Path, path: str, content: bytes) -> dict:
    target = root / path
    if not valid_job_path(path) or target.resolve() != target:
        raise ValueError("交付路径不在当前工作区内")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        staged = Path(stream.name)
        try:
            stream.write(content)
            stream.close()
            try:
                os.link(staged, target)
            except FileExistsError:
                if not target.is_file():
                    raise ValueError(f"交付路径已占用 {path}") from None
                with target.open("rb") as existing:
                    if existing.read(len(content) + 1) != content:
                        raise ValueError(f"交付路径已存在其他内容 {path}") from None
        finally:
            staged.unlink(missing_ok=True)
    return {"path": path, "name": target.name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def _archive(files: dict[str, str], checks: list[CheckResult]) -> bytes:
    output = io.BytesIO()
    report = "verification.json"
    while report in files:
        report = "project-" + report
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for path, text in sorted(files.items()):
            archive.writestr(ZipInfo(path), text.encode("utf-8"), compress_type=ZIP_DEFLATED)
        archive.writestr(ZipInfo(report), json.dumps({
            "passed": True, "source_digest": source_digest(files),
            "checks": [{"id": item.id, "passed": item.passed} for item in checks],
        }, ensure_ascii=False))
    return output.getvalue()


def _runtime_checks(receipt: dict, contract: TaskContract) -> list[CheckResult]:
    rows = {row["id"]: row for row in receipt.get("checks", [])}
    passing = {key for key, row in rows.items() if row.get("status") == "passed"}
    errors = [f"{row['path']} · {row['message']}" for row in receipt.get("issues", [])[:4]]
    errors.extend(row.get("detail", "") for row in rows.values() if row.get("status") == "failed")
    start = {"runtime.web"} if contract.project_scope == "frontend" else {"runtime.start"}
    if contract.project_scope == "fullstack":
        start.add("runtime.web")
    flows = {key for key in rows if key.startswith("runtime.browser.flow.")}
    suites = {key for key in rows if key.startswith("runtime.tests.")}
    requirements = {
        "runtime_start": (start <= passing, start, "需实际启动计划中的服务并请求页面或接口"),
        "runtime_build": ("runtime.web_build" in passing, {"runtime.web_build"}, "缺少通过的生产构建记录"),
        "runtime_tests": (bool(suites & passing), {"runtime.tests", *suites}, "缺少通过的随包业务测试"),
        "runtime_browser": (bool(flows) and {"runtime.browser", *flows} <= passing,
                            {"runtime.browser", "runtime.browser.flows", *flows},
                            "需保存 tests/browser-checks.json 并实际通过浏览器操作场景"),
    }
    checks = [CheckResult("project_errors", not errors, str(redact_secrets("；".join(dict.fromkeys(errors))))[:1000])]
    for key in contract.project_checks:
        if key in requirements:
            passed, related, fallback = requirements[key]
            details = [row["detail"] for name, row in rows.items()
                       if name in related and row.get("status") != "passed" and row.get("detail")]
            detail = str(redact_secrets("；".join(details)))[:520] if details else fallback
            checks.append(CheckResult(key, passed, "" if passed else detail))
    return checks


async def review_workspace_project(
    root: Path, run_id: str, contract: TaskContract, artifacts: list[dict], prior: ProjectReview | None,
) -> ProjectReview:
    """发布时复用已验收的字节。模型自报的命令、路径和旧压缩包不作为运行证据。"""
    try:
        folder, files = await asyncio.to_thread(_snapshot, root, artifacts, contract.project_openapi_path)
    except (OSError, ValueError, RuntimeError) as exc:
        detail = str(exc) if isinstance(exc, ValueError) else "无法读取本轮已保存的工程文件"
        return ProjectReview("", [CheckResult("project_sources", False, detail)], [])
    digest = _workspace_source_digest(folder, files)
    if prior and prior.digest == digest and not prior.unavailable:
        return prior
    if not settings.AGENT_PROJECT_VERIFIER_DIR.strip():
        return ProjectReview(digest, [CheckResult("project_environment", False, "当前部署未启用隔离工程验收")], [], True)

    checks, generated = [], []
    api_path = contract.project_openapi_path or posixpath.join(folder, "openapi.json")
    archive_path = contract.project_archive_path or posixpath.join(folder, f"project-{run_id[:12]}.zip")
    api_entry = posixpath.relpath(api_path, folder or ".")
    captured = None
    if "openapi" in contract.project_checks:
        if api_entry in files:
            try:
                document = openapi_source(json.loads(files[api_entry]))
            except (ValueError, RecursionError):
                document = None
        elif valid_job_path(api_entry):
            receipt = await verify_runtime(files, contract.project_scope, capture_openapi=True)
            captured = document = openapi_source((receipt or {}).get("openapi"))
            if captured:
                files[api_entry] = captured
        else:
            document = None
        checks.append(CheckResult("openapi", document is not None, "" if document else f"缺少可用的 OpenAPI 契约 {api_path}"))

    receipt = await verify_runtime(files, contract.project_scope, capture_browser_errors=True)
    if not receipt or receipt.get("source_digest") != source_digest(files):
        return ProjectReview(digest, [CheckResult("project_environment", False, "未收到与源码快照一致的运行记录")], [], True)
    checks.extend(_runtime_checks(receipt, contract))
    if all(item.passed for item in checks):
        try:
            if captured:
                generated.append(await asyncio.to_thread(_save_output, root, api_path, captured.encode()))
            if "archive" in contract.project_checks:
                bundle = await asyncio.to_thread(_archive, files, checks)
                generated.append(await asyncio.to_thread(_save_output, root, archive_path, bundle))
                checks.append(CheckResult("archive", True, f"已按验收源码生成 {archive_path}"))
        except (OSError, RuntimeError, ValueError) as exc:
            detail = str(exc) if isinstance(exc, ValueError) else "无法保存可下载的工程交付文件"
            checks.append(CheckResult("project_publish", False, detail))
    elif "archive" in contract.project_checks:
        checks.append(CheckResult("archive", False, "工程通过上述检查后才会生成可下载的源码包"))
    unavailable = any(row.get("id") == "runtime.environment" for row in receipt.get("checks", []))
    return ProjectReview(digest, checks, generated, unavailable)
