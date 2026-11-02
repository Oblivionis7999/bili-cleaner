"""只读数据路由：关注 / 动态 / 收藏夹 / 稍后再看，以及规则分类与统计。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api", tags=["data"])


def _ctx(request: Request):
    return request.app.state.ctx


# ---------------------------------------------------------------- 小工具


def _parse_bool_flag(value: str) -> bool | None:
    """``mutual=1/0`` 这类参数解析；空值返回 None 表示不过滤。"""
    text = str(value or "").strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return None


def _date_ts(value: str, end_of_day: bool = False) -> float | None:
    """``YYYY-MM-DD`` → 当日 0 点/23:59:59 的 Unix 秒。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail=f"日期格式应为 YYYY-MM-DD：{text}") from None
    if end_of_day:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return parsed.timestamp()


def _is_mutual(item: dict) -> bool:
    if item.get("mutual"):
        return True
    try:
        return int(item.get("attribute") or 0) == 6
    except (TypeError, ValueError):
        return False


def _filter_follows(
    items: list[dict],
    q: str = "",
    category: str = "",
    group: str = "",
    mutual: str = "",
) -> list[dict]:
    """关注列表筛选：关键词 / 分类 / 分组 / 互关。"""
    keyword = str(q or "").strip().lower()
    category = str(category or "").strip()
    group = str(group or "").strip()
    mutual_flag = _parse_bool_flag(mutual)
    group_name = ""
    group_id: int | None = None
    if group not in ("", "all"):
        if group in ("0", "none", "__none__"):
            group_id = 0
        else:
            try:
                group_id = int(group)
            except ValueError:
                group_name = group

    result: list[dict] = []
    for item in items:
        if keyword:
            haystack = f"{item.get('name') or ''} {item.get('sign') or ''}".lower()
            if keyword not in haystack:
                continue
        if category:
            if category == "__none__":
                if item.get("category"):
                    continue
            elif item.get("category") != category:
                continue
        if group not in ("", "all"):
            group_ids = item.get("group_ids") or []
            groups = item.get("groups") or []
            if group_id == 0:
                # 未分组：既没有 tagid 也没有分组名
                if group_ids or groups:
                    continue
            elif group_id is not None:
                if group_id not in group_ids and str(group_id) not in [str(g) for g in groups]:
                    continue
            elif group_name and group_name not in [str(g) for g in groups]:
                continue
        if mutual_flag is not None and _is_mutual(item) != mutual_flag:
            continue
        result.append(item)
    return result


# ---------------------------------------------------------------- 关注


@router.get("/follows")
def list_follows(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    q: str = "",
    category: str = "",
    group: str = "",
    mutual: str = "",
) -> dict:
    """关注列表（支持关键词/分类/分组/互关筛选与分页）。"""
    ctx = _ctx(request)
    filtered = _filter_follows(ctx.follows(), q=q, category=category, group=group, mutual=mutual)
    return {"total": len(filtered), "offset": offset, "items": filtered[offset : offset + limit]}


@router.get("/follows/groups")
def list_follow_groups(request: Request) -> dict:
    """关注分组列表。"""
    return {"items": _ctx(request).groups()}


@router.get("/follows/stats")
def follows_stats(request: Request) -> dict:
    """关注分类统计（全量，不是"已加载"部分）。"""
    return _ctx(request).follows_stats()


@router.post("/follows/classify")
def classify_follows(request: Request) -> dict:
    """规则分类（同步、秒级）；LLM 增强分类走异步任务。"""
    return {"updated": _ctx(request).rule_classify()}


# ---------------------------------------------------------------- 动态


@router.get("/dynamics")
def list_dynamics(
    request: Request,
    offset: str = "",
    limit: int = Query(30, ge=1, le=200),
    q: str = "",
    type: str = "",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """动态列表；``offset`` 是下一页游标（空串表示从头）。"""
    ctx = _ctx(request)
    items = ctx.dynamics()
    keyword = str(q or "").strip().lower()
    type_filter = str(type or "").strip()
    ts_from = _date_ts(date_from)
    ts_to = _date_ts(date_to, end_of_day=True)

    filtered: list[dict] = []
    for item in items:
        if type_filter and type_filter not in (item.get("type"), item.get("type_label")):
            continue
        if keyword:
            video = item.get("video") or {}
            haystack = (
                f"{item.get('text') or ''} {video.get('title') or ''} {item.get('orig_author') or ''}"
            ).lower()
            if keyword not in haystack:
                continue
        if ts_from is not None or ts_to is not None:
            try:
                pub_ts = int(item.get("pub_ts") or 0)
            except (TypeError, ValueError):
                pub_ts = 0
            if ts_from is not None and pub_ts < ts_from:
                continue
            if ts_to is not None and pub_ts > ts_to:
                continue
        filtered.append(item)

    try:
        start = max(0, int(offset or 0))
    except (TypeError, ValueError):
        start = 0
    page = filtered[start : start + limit]
    next_offset = start + len(page)
    has_more = next_offset < len(filtered)
    return {
        "offset": str(next_offset) if has_more else "",
        "has_more": has_more,
        "items": page,
    }


# ---------------------------------------------------------------- 收藏夹


@router.get("/favorites/folders")
def list_favorite_folders(request: Request) -> dict:
    """收藏夹列表。"""
    return {"items": _ctx(request).favorite_folders()}


@router.get("/favorites/{media_id}/items")
def list_favorite_items(
    request: Request,
    media_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    q: str = "",
) -> dict:
    """某个收藏夹里的条目（支持标题/UP 主关键词）。"""
    ctx = _ctx(request)
    items = ctx.favorite_items(media_id)
    keyword = str(q or "").strip().lower()
    if keyword:
        items = [
            item
            for item in items
            if keyword in f"{item.get('title') or ''} {item.get('upper') or ''}".lower()
        ]
    return {"total": len(items), "offset": offset, "items": items[offset : offset + limit]}


# ---------------------------------------------------------------- 稍后再看


@router.get("/toview")
def list_toview(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """稍后再看列表。"""
    items = _ctx(request).toview()
    return {"total": len(items), "items": items[offset : offset + limit]}
