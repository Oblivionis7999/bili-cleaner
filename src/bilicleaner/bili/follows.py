"""关注列表、关注分组、取关与关系查询。

契约：``iter_followings`` / ``list_groups`` / ``unfollow`` / ``get_relation``。
"""

from __future__ import annotations

from typing import Any, Iterator

from ..errors import BiliCleanerError
from .client import MAX_PAGES, csrf_of, resolve_mid, to_int, unwrap

FOLLOWINGS_PATH = "/x/relation/followings"
TAGS_PATH = "/x/relation/tags"
MODIFY_PATH = "/x/relation/modify"
RELATION_PATH = "/x/relation"

PAGE_SIZE = 50
UNFOLLOW_ACT = 2
# 网页端取关请求带的来源标识
UNFOLLOW_RE_SRC = 11


def iter_followings(client: Any, max_pages: int = MAX_PAGES) -> Iterator[dict]:
    """按页拉取全部关注，逐条产出契约里的 FollowItem。

    自动翻页，total 以第一页响应的 ``total`` 为准；``max_pages`` 是安全上限。
    """
    uid = resolve_mid(client)
    tag_names = _tag_names(client)
    seen: set[int] = set()
    total = 0
    order = 0
    for page in range(1, max(1, to_int(max_pages, MAX_PAGES)) + 1):
        payload = client.get(
            FOLLOWINGS_PATH,
            {
                "vmid": uid,
                "pn": page,
                "ps": PAGE_SIZE,
                "order": "desc",
                "order_type": "attention",
                "web_location": 333.1387,
            },
            wbi=True,
        )
        data = unwrap(payload)
        if not isinstance(data, dict):
            break
        if page == 1:
            total = to_int(data.get("total"))
        items = data.get("list")
        if not isinstance(items, list) or not items:
            break
        for raw in items:
            item = parse_following(raw, order + 1, tag_names)
            order += 1
            mid = item["mid"]
            if mid <= 0 or mid in seen:
                continue
            seen.add(mid)
            yield item
        if total and len(seen) >= total:
            break
        if len(items) < PAGE_SIZE:
            break


def parse_following(raw: Any, order: int = 1, tag_names: dict | None = None) -> dict:
    """把 B 站关注列表项解析成契约 FollowItem（字段缺失给空值，不抛异常）。"""
    item = raw if isinstance(raw, dict) else {}
    names = tag_names if isinstance(tag_names, dict) else {}

    raw_tags = item.get("tag")
    if not isinstance(raw_tags, list):
        raw_tags = []
    group_ids: list[int] = []
    for tag in raw_tags:
        try:
            group_ids.append(int(tag))
        except (TypeError, ValueError):
            continue

    official_verify = item.get("official_verify")
    official = ""
    if isinstance(official_verify, dict):
        official = str(official_verify.get("desc") or "")

    attribute = to_int(item.get("attribute"))
    return {
        "mid": to_int(item.get("mid")),
        "name": str(item.get("uname") or ""),
        "sign": str(item.get("sign") or ""),
        "face": str(item.get("face") or ""),
        "official": official,
        "attribute": attribute,
        "mutual": attribute == 6,
        "groups": [str(names.get(tag_id, tag_id)) for tag_id in group_ids],
        "group_ids": group_ids,
        "order": to_int(order),
        "mtime": to_int(item.get("mtime")),
        "category": None,
        "category_source": None,
    }


def list_groups(client: Any) -> list[dict]:
    """取关注分组，返回契约里的 FollowGroup 列表（含默认分组/特别关注）。"""
    payload = client.get(TAGS_PATH)
    data = unwrap(payload)
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        raw_items = data.get("list") or data.get("tags") or []
    else:
        raw_items = []
    if not isinstance(raw_items, list):
        return []
    groups: list[dict] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        groups.append(
            {
                "tagid": to_int(raw.get("tagid")),
                "name": str(raw.get("name") or ""),
                "count": to_int(raw.get("count")),
            }
        )
    return groups


def unfollow(client: Any, mid: int) -> None:
    """取关单个 UP 主（``act=2``）。"""
    target = to_int(mid, -1)
    if target <= 0:
        raise ValueError(f"无效的 mid: {mid!r}")
    payload: dict[str, Any] = {
        "fid": target,
        "act": UNFOLLOW_ACT,
        "re_src": UNFOLLOW_RE_SRC,
    }
    csrf = csrf_of(client)
    if csrf:
        payload["csrf"] = csrf
    client.post(MODIFY_PATH, payload)


def get_relation(client: Any, mid: int) -> dict:
    """查询与某个 UP 主的关系，返回 data dict（attribute=6 即互关）。"""
    target = to_int(mid, -1)
    if target <= 0:
        raise ValueError(f"无效的 mid: {mid!r}")
    payload = client.get(RELATION_PATH, {"fid": target}, wbi=True)
    data = unwrap(payload)
    return data if isinstance(data, dict) else {}


def _tag_names(client: Any) -> dict[int, str]:
    """分组名映射；分组接口失败时不阻塞关注列表拉取。"""
    try:
        groups = list_groups(client)
    except BiliCleanerError:
        return {}
    return {group["tagid"]: group["name"] for group in groups if group["name"]}


__all__ = [
    "FOLLOWINGS_PATH",
    "TAGS_PATH",
    "MODIFY_PATH",
    "RELATION_PATH",
    "PAGE_SIZE",
    "iter_followings",
    "parse_following",
    "list_groups",
    "unfollow",
    "get_relation",
]
