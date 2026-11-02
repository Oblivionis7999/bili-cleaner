"""稍后再看：列表与一键清空。

契约：``list_toview`` / ``clear_toview``。
"""

from __future__ import annotations

from typing import Any

from .client import csrf_of, to_int, unwrap

TOVIEW_PATH = "/x/v2/history/toview"
CLEAR_PATH = "/x/v2/history/toview/clear"


def list_toview(client: Any) -> list[dict]:
    """取稍后再看全量列表（接口一次返回全部），返回契约里的 ToviewItem 列表。"""
    payload = client.get(TOVIEW_PATH)
    data = unwrap(payload)
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        raw_items = data.get("list") or []
    else:
        raw_items = []
    if not isinstance(raw_items, list):
        return []

    items: list[dict] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        owner = raw.get("owner") if isinstance(raw.get("owner"), dict) else {}
        items.append(
            {
                "aid": to_int(raw.get("aid")),
                "title": str(raw.get("title") or ""),
                "upper": str(owner.get("name") or raw.get("upper") or ""),
                "duration": to_int(raw.get("duration")),
                "progress": to_int(raw.get("progress")),
                # 官方字段是 add_at，兼容 add_ts
                "add_ts": to_int(raw.get("add_at") or raw.get("add_ts")),
                "bvid": str(raw.get("bvid") or ""),
            }
        )
    return items


def clear_toview(client: Any) -> None:
    """一键清空稍后再看。"""
    payload: dict[str, Any] = {}
    csrf = csrf_of(client)
    if csrf:
        payload["csrf"] = csrf
    client.post(CLEAR_PATH, payload)


__all__ = ["TOVIEW_PATH", "CLEAR_PATH", "list_toview", "clear_toview"]
