"""任务与时间相关的纯函数工具。"""

from __future__ import annotations

import time
import uuid

from .const import TASK_STATUS


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def new_task(task_type: str, params: dict | None = None, total: int = 0) -> dict:
    ts = now_iso()
    return {
        "id": uuid.uuid4().hex[:12],
        "type": task_type,
        "status": "queued",
        "total": total,
        "done": 0,
        "failed": 0,
        "created_at": ts,
        "updated_at": ts,
        "cooldown_until": None,
        "message": "排队中",
        "backup_path": None,
        "params": params or {},
    }


def touch_task(task: dict, **fields) -> dict:
    task.update(fields)
    task["updated_at"] = now_iso()
    if task.get("status") not in TASK_STATUS:
        task["status"] = "running"
    return task


def task_is_finished(task: dict) -> bool:
    return task.get("status") in ("done", "failed", "stopped")
