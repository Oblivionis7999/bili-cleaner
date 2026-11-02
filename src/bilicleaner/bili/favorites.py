"""收藏夹：文件夹列表、条目分页、批量删除与失效清理。

契约：``list_folders`` / ``iter_items`` / ``batch_delete`` / ``clean_invalid``。
按契约，``batch_delete`` 单次最多 20 条，切批由调用方负责（超过直接报错）。
"""

from __future__ import annotations

from typing import Any, Iterator

from .client import MAX_PAGES, csrf_of, resolve_mid, to_int, unwrap

FOLDERS_PATH = "/x/v3/fav/folder/created/list-all"
RESOURCE_LIST_PATH = "/x/v3/fav/resource/list"
BATCH_DEL_PATH = "/x/v3/fav/resource/batch-del"
CLEAN_PATH = "/x/v3/fav/resource/clean"

PAGE_SIZE = 20
MAX_BATCH_SIZE = 20
DEFAULT_FOLDER_NAME = "默认收藏夹"


def list_folders(client: Any) -> list[dict]:
    """取当前用户的收藏夹列表，返回契约里的 FavFolder 列表。"""
    uid = resolve_mid(client)
    payload = client.get(
        FOLDERS_PATH, {"up_mid": uid, "web_location": 333.1387}
    )
    data = unwrap(payload)
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        raw_items = data.get("list") or data.get("medias") or []
    else:
        raw_items = []
    if not isinstance(raw_items, list):
        return []

    folders: list[dict] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "")
        attr = to_int(raw.get("attr"))
        folders.append(
            {
                "media_id": to_int(raw.get("id") or raw.get("media_id")),
                "title": title,
                "count": to_int(raw.get("media_count") or raw.get("count")),
                "is_default": bool(raw.get("is_default"))
                or title.strip() == DEFAULT_FOLDER_NAME,
                "privacy": attr & 1,
            }
        )
    return folders


def iter_items(client: Any, media_id: int, max_pages: int = MAX_PAGES) -> Iterator[dict]:
    """分页拉取收藏夹条目（ps=20，无 wbi），逐条产出契约里的 FavItem。"""
    target = to_int(media_id, -1)
    if target <= 0:
        raise ValueError(f"无效的 media_id: {media_id!r}")
    total = 0
    fetched = 0
    for page in range(1, max(1, to_int(max_pages, MAX_PAGES)) + 1):
        payload = client.get(
            RESOURCE_LIST_PATH,
            {
                "media_id": target,
                "pn": page,
                "ps": PAGE_SIZE,
                "keyword": "",
                "order": "mtime",
                "type": 0,
                "tid": 0,
                "platform": "web",
            },
        )
        data = unwrap(payload)
        if not isinstance(data, dict):
            break
        medias = data.get("medias")
        if not isinstance(medias, list) or not medias:
            break
        for raw in medias:
            fetched += 1
            yield parse_fav_item(raw)
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        if page == 1:
            total = to_int(info.get("media_count"))
        has_more = data.get("has_more")
        if has_more is None:
            has_more = len(medias) >= PAGE_SIZE
        if not has_more:
            break
        if total and fetched >= total:
            break
        if len(medias) < PAGE_SIZE:
            break


def parse_fav_item(raw: Any) -> dict:
    """把收藏条目原始 item 解析成契约 FavItem（字段缺失给空值）。"""
    item = raw if isinstance(raw, dict) else {}
    upper = item.get("upper") if isinstance(item.get("upper"), dict) else {}
    attr = to_int(item.get("attr"))
    title = str(item.get("title") or "")
    bvid = str(item.get("bvid") or "")
    return {
        "id": str(item.get("id") or ""),
        "type": to_int(item.get("type")),
        "title": title,
        "upper": str(upper.get("name") or item.get("upper_name") or ""),
        "bvid": bvid,
        "cover": str(item.get("cover") or ""),
        "fav_time": to_int(item.get("fav_time")),
        "invalid": bool(attr & 1) or (not title and not bvid),
    }


def batch_delete(client: Any, media_id: int, resources: list[str]) -> dict:
    """批量删除收藏条目；``resources`` 为 ``["id:type", ...]``，单批最多 20 条。"""
    target = to_int(media_id, -1)
    if target <= 0:
        raise ValueError(f"无效的 media_id: {media_id!r}")
    keys = [str(resource).strip() for resource in (resources or []) if str(resource).strip()]
    if not keys:
        return {}
    if len(keys) > MAX_BATCH_SIZE:
        raise ValueError(
            f"单批最多 {MAX_BATCH_SIZE} 条（收到 {len(keys)} 条），请由调用方切批"
        )
    payload: dict[str, Any] = {
        "resources": ",".join(keys),
        "media_id": target,
        "platform": "web",
    }
    csrf = csrf_of(client)
    if csrf:
        payload["csrf"] = csrf
    response = client.post(BATCH_DEL_PATH, payload)
    data = unwrap(response)
    return data if isinstance(data, dict) else {}


def clean_invalid(client: Any, media_id: int) -> None:
    """清理收藏夹里的失效内容（一键清理接口）。"""
    target = to_int(media_id, -1)
    if target <= 0:
        raise ValueError(f"无效的 media_id: {media_id!r}")
    payload: dict[str, Any] = {"media_id": target, "platform": "web"}
    csrf = csrf_of(client)
    if csrf:
        payload["csrf"] = csrf
    client.post(CLEAN_PATH, payload)


__all__ = [
    "FOLDERS_PATH",
    "RESOURCE_LIST_PATH",
    "BATCH_DEL_PATH",
    "CLEAN_PATH",
    "PAGE_SIZE",
    "MAX_BATCH_SIZE",
    "list_folders",
    "iter_items",
    "parse_fav_item",
    "batch_delete",
    "clean_invalid",
]
