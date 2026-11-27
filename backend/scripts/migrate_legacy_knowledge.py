"""Phase 6：旧知识库索引迁移脚本（可重复运行）。

用法（backend/ 目录下）：
    python -m scripts.migrate_legacy_knowledge                       # 只体检，不写库
    python -m scripts.migrate_legacy_knowledge --apply               # 备份 + 创建版本/Job
    python -m scripts.migrate_legacy_knowledge --apply --run         # 备份 + 入队 + 立即执行
    python -m scripts.migrate_legacy_knowledge --apply --kb-id 1     # 只迁移指定知识库

行为：
1. 备份旧 SQLite `knowledge.db` 与 `UPLOAD_DIR/` 到 ``--backup-dir``；
2. 统计旧 KB / Document / Chunk；
3. 对每个旧文档从**原始文件**重新解析（不复用旧字符 Chunk）；
4. 源文件缺失 / 格式已不支持 → 标记 ``migration_required``，不伪造成功；
5. 新索引在 Worker 中构建完成后才切换 Active Generation；
6. 旧 SQLite 索引保留，`KNOWLEDGE_LEGACY_FALLBACK=true` 可短期回滚。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import AsyncSessionLocal, init_db
from app.models.knowledge import (
    KnowledgeBase,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeIngestionJob,
)
from app.services.knowledge import storage
from app.services.knowledge.ingestion import IngestionRunner, queue_ingest, utcnow
from app.services.knowledge.security import allowed_formats, validate_magic


def _legacy_chunk_stats() -> dict:
    db_path = Path(settings.CHROMA_PERSIST_DIR) / "knowledge.db"
    if not db_path.is_file():
        return {"exists": False, "chunks": 0, "collections": {}}
    try:
        conn = sqlite3.connect(str(db_path))
        collections = dict(conn.execute(
            "SELECT collection, COUNT(*) FROM chunks GROUP BY collection"
        ).fetchall())
        total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        conn.close()
        return {"exists": True, "chunks": int(total), "collections": collections}
    except sqlite3.Error as exc:
        return {"exists": True, "error": str(exc)}


def _backup(backup_dir: Path) -> dict:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"knowledge-backup-{stamp}"
    target.mkdir(parents=True, exist_ok=True)
    result = {"target": str(target)}
    legacy_db = Path(settings.CHROMA_PERSIST_DIR) / "knowledge.db"
    if legacy_db.is_file():
        shutil.copy2(legacy_db, target / "knowledge.db")
        result["legacy_db"] = str(target / "knowledge.db")
    upload_dir = Path(settings.UPLOAD_DIR)
    if upload_dir.exists():
        shutil.copytree(upload_dir, target / "uploads", dirs_exist_ok=True)
        result["uploads"] = str(target / "uploads")
    storage_dir = Path(settings.KNOWLEDGE_STORAGE_DIR)
    if storage_dir.exists():
        shutil.copytree(
            storage_dir, target / "knowledge", dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("backups"),
        )
        result["knowledge"] = str(target / "knowledge")
    return result


async def _migrate(kb_id: int | None, apply: bool, run: bool, backup_dir: Path) -> dict:
    await init_db()
    report: dict = {
        "apply": apply,
        "run": run,
        "started_at": utcnow().isoformat(),
        "legacy_index": _legacy_chunk_stats(),
        "knowledge_bases": 0,
        "documents": 0,
        "queued": 0,
        "skipped": 0,
        "migration_required": 0,
        "jobs": [],
    }
    if apply:
        report["backup"] = _backup(backup_dir)

    supported = allowed_formats()
    async with AsyncSessionLocal() as db:
        query = select(KnowledgeBase).where(KnowledgeBase.status != "deleted")
        if kb_id:
            query = query.where(KnowledgeBase.id == kb_id)
        bases = (await db.execute(query)).scalars().all()
        report["knowledge_bases"] = len(bases)

        for kb in bases:
            documents = (await db.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.knowledge_base_id == kb.id,
                    KnowledgeDocument.status != "deleted",
                )
            )).scalars().all()
            report["documents"] += len(documents)
            for document in documents:
                ext = (document.file_type or "").lower().lstrip(".")
                source = Path(document.file_path) if document.file_path else None
                if ext not in supported:
                    report["migration_required"] += 1
                    if apply:
                        document.status = "migration_required"
                        document.error_code = "unsupported_format"
                        document.error_msg = f"新索引不支持格式 .{ext}，请转换为受支持格式后重新上传"
                        db.add(document)
                    continue
                if source is None or not source.is_file():
                    report["migration_required"] += 1
                    if apply:
                        document.status = "migration_required"
                        document.error_code = "source_missing"
                        document.error_msg = "源文件缺失，无法重新解析；请重新上传原始文件"
                        db.add(document)
                    continue
                try:
                    validate_magic(str(source), ext)
                except Exception as exc:  # noqa: BLE001
                    report["migration_required"] += 1
                    if apply:
                        document.status = "migration_required"
                        document.error_code = "mime_mismatch"
                        document.error_msg = f"源文件类型校验失败：{exc}"
                        db.add(document)
                    continue

                # 已存在当前 Pipeline 的版本/Job：跳过（幂等）
                existing_version = (await db.execute(
                    select(KnowledgeDocumentVersion).where(
                        KnowledgeDocumentVersion.document_id == document.id,
                        KnowledgeDocumentVersion.source_sha256 == (document.sha256 or ""),
                        KnowledgeDocumentVersion.parsed_artifact_key.is_not(None),
                    ).order_by(KnowledgeDocumentVersion.version_no.desc()).limit(1)
                )).scalar_one_or_none()
                if existing_version is not None:
                    report["skipped"] += 1
                    continue

                if not apply:
                    report["queued"] += 1
                    continue

                sha = document.sha256 or storage.sha256_file(source)
                document.sha256 = sha
                if not document.storage_key:
                    # 旧文件保留在原路径；新解析产物写入新存储目录
                    document.storage_key = None
                last_no = (await db.execute(
                    select(func.max(KnowledgeDocumentVersion.version_no)).where(
                        KnowledgeDocumentVersion.document_id == document.id
                    )
                )).scalar_one_or_none() or 0
                version = KnowledgeDocumentVersion(
                    document_id=document.id,
                    version_no=int(last_no) + 1,
                    source_sha256=sha,
                    parser_name="",
                    parser_version="",
                    pipeline_version="",
                    status="building",
                    options_json={"migration": True},
                )
                db.add(version)
                document.status = "queued"
                document.error_code = None
                document.error_msg = None
                db.add(document)
                await db.flush()
                job = await queue_ingest(
                    db, knowledge_base=kb, document=document, version=version,
                )
                report["queued"] += 1
                report["jobs"].append(job.id)
                await db.commit()

        if apply:
            await db.commit()

    if run and report["jobs"]:
        worker_id = f"migration-{os.getpid()}"
        runner = IngestionRunner(worker_id)
        for job_id in report["jobs"]:
            async with AsyncSessionLocal() as db:
                job = await db.get(KnowledgeIngestionJob, job_id)
                if job is None or job.status not in {"queued"}:
                    continue
                # Worker 正常由 claim_job 领取；迁移脚本直接推进一次（单进程离线）
                job.status = "running"
                job.lease_owner = worker_id
                job.lease_token = f"migration-{job_id}"
                job.attempt = int(job.attempt or 0) + 1
                job.started_at = utcnow()
                db.add(job)
                await db.commit()
            await runner.run(job_id)
    report["finished_at"] = utcnow().isoformat()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="旧知识库索引迁移（Phase 6）")
    parser.add_argument("--apply", action="store_true", help="实际写入版本 / Job（默认只体检）")
    parser.add_argument("--run", action="store_true", help="入队后立即在本进程执行构建")
    parser.add_argument("--kb-id", type=int, default=None, help="只迁移指定知识库")
    parser.add_argument(
        "--backup-dir",
        default=str(Path(settings.KNOWLEDGE_STORAGE_DIR).parent / "backups" / "knowledge"),
        help="备份目录（默认在知识库存储目录之外，避免递归备份）",
    )
    args = parser.parse_args()
    report = asyncio.run(_migrate(
        args.kb_id, args.apply, args.run, Path(args.backup_dir),
    ))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
