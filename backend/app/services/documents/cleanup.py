"""核对并清理进程中断留下的文档文件，默认仅输出可清理项。"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import math
import time

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.agent import AgentRun
from app.services.agent_runtime.lifecycle import TERMINAL_STATUSES

from . import repository, storage


async def reconcile_files(*, cutoff: float, apply: bool = False, sessions=AsyncSessionLocal) -> list[dict]:
    candidates = []
    for candidate in storage.cleanup_candidates(cutoff):
        async with sessions() as db, db.begin():
            if isinstance(candidate, storage.VersionFile):
                if await repository.locked_version_record(db, candidate) is not None:
                    continue
                if storage.discard_version_candidate(candidate, cutoff=cutoff, apply=apply):
                    candidates.append({"type": "version", **asdict(candidate)})
            else:
                status = await db.scalar(select(AgentRun.status).where(
                    AgentRun.id == candidate.run_id, AgentRun.user_id == candidate.owner_id,
                ).with_for_update())
                if status is not None and status not in TERMINAL_STATUSES:
                    continue
                if storage.discard_staging_candidate(candidate, cutoff=cutoff, apply=apply):
                    candidates.append({"type": "staging", **asdict(candidate)})
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="执行清理，默认只显示候选项")
    parser.add_argument("--older-than-hours", type=float, default=24, help="最小文件闲置时长，默认 24 小时")
    args = parser.parse_args()
    if not math.isfinite(args.older_than_hours) or args.older_than_hours <= 0:
        parser.error("--older-than-hours 必须为正数")
    candidates = asyncio.run(reconcile_files(cutoff=time.time() - args.older_than_hours * 3600, apply=args.apply))
    print(json.dumps({"applied": args.apply, "count": len(candidates), "files": candidates}, ensure_ascii=False))


if __name__ == "__main__":
    main()
