"""任务路由：创建、查询、暂停/继续/停止。

API 层只负责校验请求形状和转交，写操作一律由 TaskManager 负责。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api", tags=["tasks"])


def _ctx(request: Request):
    return request.app.state.ctx


class TaskCreateIn(BaseModel):
    """创建任务请求体。``ack_risk`` 按契约放在 params 里。"""

    type: str
    params: dict = Field(default_factory=dict)
    confirm_phrase: str | None = None


@router.post("/tasks")
def create_task(request: Request, body: TaskCreateIn) -> dict:
    """创建任务（入队后由后台单写者线程执行）。"""
    ctx = _ctx(request)
    return ctx.tm.create(body.type, body.params or {}, body.confirm_phrase)


@router.get("/tasks")
def list_tasks(request: Request) -> dict:
    """任务列表（新的在前）。"""
    return {"items": _ctx(request).tm.list()}


@router.get("/tasks/{task_id}")
def get_task(request: Request, task_id: str) -> dict:
    task = _ctx(request).tm.get(task_id)
    if task is None:
        from ..errors import TaskError

        raise TaskError(f"任务不存在：{task_id}", code="NOT_FOUND")
    return task


@router.post("/tasks/{task_id}/pause")
def pause_task(request: Request, task_id: str) -> dict:
    return _ctx(request).tm.pause(task_id)


@router.post("/tasks/{task_id}/resume")
def resume_task(request: Request, task_id: str) -> dict:
    return _ctx(request).tm.resume(task_id)


@router.post("/tasks/{task_id}/stop")
def stop_task(request: Request, task_id: str) -> dict:
    return _ctx(request).tm.stop(task_id)
