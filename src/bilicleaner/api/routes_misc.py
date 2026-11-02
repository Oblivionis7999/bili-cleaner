"""杂项路由：健康检查、备份列表、操作日志、设置。"""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query, Request

from ..const import VERSION

router = APIRouter(prefix="/api", tags=["misc"])


def _ctx(request: Request):
    return request.app.state.ctx


# ---------------------------------------------------------------- 设置校验

_NUMERIC_RULES = {
    "delay_seconds": (0.0, 120.0),
    "jitter_seconds": (0.0, 60.0),
    "batch_pause_seconds": (0.0, 3600.0),
    "cooldown_seconds": (0.0, 86400.0),
}
_INT_RULES = {
    "batch_size": (1, 200),
    "max_cooldowns": (1, 10),
    "backup_keep": (0, 10000),
}
_BOOL_KEYS = {"protect_mutual"}
_LLM_INT_RULES = {"batch_size": (1, 20)}


def _check_number(key: str, value: object, low: float, high: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{key} 必须是数字") from None
    if number < low or number > high:
        raise HTTPException(status_code=400, detail=f"{key} 超出允许范围 {low}~{high}")
    return number


def _validate_settings(patch: dict) -> None:
    for key, (low, high) in _NUMERIC_RULES.items():
        if key in patch:
            if isinstance(patch[key], bool):
                raise HTTPException(status_code=400, detail=f"{key} 必须是数字")
            _check_number(key, patch[key], low, high)
    for key, (low, high) in _INT_RULES.items():
        if key in patch:
            value = _check_number(key, patch[key], low, high)
            if int(value) != value:
                raise HTTPException(status_code=400, detail=f"{key} 必须是整数")
    for key in _BOOL_KEYS:
        if key in patch and not isinstance(patch[key], bool):
            raise HTTPException(status_code=400, detail=f"{key} 必须是布尔值")
    if "confirm_phrase" in patch:
        phrase = str(patch.get("confirm_phrase") or "").strip()
        if not phrase:
            raise HTTPException(status_code=400, detail="确认词不能为空")
    if "llm" in patch:
        llm = patch.get("llm")
        if not isinstance(llm, dict):
            raise HTTPException(status_code=400, detail="llm 必须是对象")
        for key, (low, high) in _LLM_INT_RULES.items():
            if key in llm:
                value = _check_number(f"llm.{key}", llm[key], low, high)
                if int(value) != value:
                    raise HTTPException(status_code=400, detail=f"llm.{key} 必须是整数")
        if "enabled" in llm and not isinstance(llm["enabled"], bool):
            raise HTTPException(status_code=400, detail="llm.enabled 必须是布尔值")
        for key in ("base_url", "api_key", "model"):
            if key in llm and not isinstance(llm[key], str):
                raise HTTPException(status_code=400, detail=f"llm.{key} 必须是字符串")


# ---------------------------------------------------------------- 路由


@router.get("/health")
def health(request: Request) -> dict:
    """健康检查（启动时前端也用它探测 mock 模式）。"""
    ctx = _ctx(request)
    return {"ok": True, "version": VERSION, "mock": bool(getattr(ctx, "mock", False))}


@router.get("/backups")
def list_backups(request: Request) -> dict:
    """备份文件列表（每个任务一个目录，内含 CSV+JSONL）。"""
    return {"items": _ctx(request).backup.list_backups()}


@router.get("/logs")
def list_logs(
    request: Request,
    limit: int = Query(200, ge=1, le=2000),
    task_id: str | None = Query(None),
) -> dict:
    """操作日志（默认最近 200 条，可按任务过滤）。"""
    ctx = _ctx(request)
    entries = ctx.store.tail_logs(limit=limit, task_id=task_id or None)
    return {"items": entries}


@router.get("/settings")
def get_settings(request: Request) -> dict:
    return _ctx(request).get_settings()


@router.put("/settings")
def update_settings(request: Request, payload: dict = Body(default_factory=dict)) -> dict:
    patch = payload if isinstance(payload, dict) else {}
    _validate_settings(patch)
    return _ctx(request).update_settings(patch)
