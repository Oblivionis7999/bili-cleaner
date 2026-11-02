"""空间动态：游标翻页与单条删除。

契约：``iter_dynamics`` / ``delete_dynamic``。
动态列表接口不需要 wbi，但需要带空间页 Referer（由 client 按 path 自动设置）。
"""

from __future__ import annotations

from typing import Any, Iterator

from ..const import DYNAMIC_TYPE_LABELS
from ..errors import BiliApiError, RiskControlError
from .client import MAX_PAGES, VC_BASE, csrf_of, to_int, unwrap

SPACE_FEED_PATH = "/x/polymer/web-dynamic/v1/feed/space"
RM_DYNAMIC_URL = VC_BASE + "/dynamic_svr/v1/dynamic_svr/rm_dynamic"

# 接口每页大约 20 条，这里仅用于判断"是否可能还有下一页"
PAGE_HINT = 20
# 这些 code 表示动态已不存在（重复删除/已被删），按成功处理
DELETED_ALREADY_CODES = {500404, -404}
TYPE_LABEL_FALLBACK = "其他"


def iter_dynamics(client: Any, mid: int, max_pages: int = MAX_PAGES) -> Iterator[dict]:
    """游标翻页拉取空间动态，逐条产出契约里的 DynamicItem。"""
    host_mid = to_int(mid, -1)
    if host_mid <= 0:
        raise ValueError(f"无效的 mid: {mid!r}")
    offset = ""
    seen: set[str] = set()
    for _ in range(max(1, to_int(max_pages, MAX_PAGES))):
        payload = client.get(
            SPACE_FEED_PATH,
            {
                "host_mid": host_mid,
                "offset": offset,
                "timezone_offset": -480,
                "features": "itemOpusStyle",
                "platform": "web",
            },
        )
        data = unwrap(payload)
        if not isinstance(data, dict):
            break
        items = data.get("items")
        if not isinstance(items, list) or not items:
            break
        for raw in items:
            item = parse_dynamic(raw)
            if not item["id"] or item["id"] in seen:
                continue
            seen.add(item["id"])
            yield item
        next_offset = str(data.get("offset") or "")
        if not data.get("has_more"):
            break
        if not next_offset or next_offset == offset:
            break
        offset = next_offset


def parse_dynamic(raw: Any) -> dict:
    """把空间动态原始 item 解析成契约 DynamicItem（字段缺失给空值）。"""
    item = raw if isinstance(raw, dict) else {}
    modules = item.get("modules") if isinstance(item.get("modules"), dict) else {}
    author = (
        modules.get("module_author")
        if isinstance(modules.get("module_author"), dict)
        else {}
    )
    dynamic = (
        modules.get("module_dynamic")
        if isinstance(modules.get("module_dynamic"), dict)
        else {}
    )
    major = dynamic.get("major") if isinstance(dynamic.get("major"), dict) else {}

    dyn_type = str(item.get("type") or "")
    return {
        "id": str(item.get("id_str") or item.get("id") or ""),
        "type": dyn_type,
        "type_label": DYNAMIC_TYPE_LABELS.get(dyn_type, TYPE_LABEL_FALLBACK),
        "pub_ts": to_int(author.get("pub_ts")),
        "text": _dynamic_text(dynamic),
        "has_image": bool(_dynamic_pics(major)),
        "video": _dynamic_video(major),
        "orig_author": _orig_author(item.get("orig")),
    }


def delete_dynamic(client: Any, dyn_id: str) -> None:
    """删除一条动态；已删除的（500404）视为成功，风控照常抛出。"""
    target = str(dyn_id or "").strip()
    if not target:
        raise BiliApiError(-999, "动态 id 为空", RM_DYNAMIC_URL)
    csrf = csrf_of(client)
    payload = {"dynamic_id": target, "csrf_token": csrf, "csrf": csrf}
    try:
        client.post(RM_DYNAMIC_URL, payload)
    except RiskControlError:
        raise
    except BiliApiError as exc:
        if getattr(exc, "bili_code", None) in DELETED_ALREADY_CODES:
            return
        raise


# ---------------------------------------------------------------------- 解析辅助


def _dynamic_text(dynamic: dict) -> str:
    """正文/转发语：desc.text 为主，再兜底 opus/视频/专栏的标题与摘要。"""
    parts: list[str] = []
    desc = dynamic.get("desc") if isinstance(dynamic.get("desc"), dict) else {}
    if desc.get("text"):
        parts.append(str(desc["text"]))
    major = dynamic.get("major") if isinstance(dynamic.get("major"), dict) else {}
    opus = major.get("opus") if isinstance(major.get("opus"), dict) else {}
    summary = opus.get("summary") if isinstance(opus.get("summary"), dict) else {}
    if summary.get("text"):
        parts.append(str(summary["text"]))
    if opus.get("title"):
        parts.append(str(opus["title"]))
    for key in ("archive", "article"):
        block = major.get(key)
        if not isinstance(block, dict):
            continue
        for field in ("title", "desc"):
            if block.get(field):
                parts.append(str(block[field]))
    # 去重且保序
    unique: list[str] = []
    for part in parts:
        if part and part not in unique:
            unique.append(part)
    return "\n".join(unique)


def _dynamic_pics(major: dict) -> list[str]:
    pics: list[str] = []
    draw = major.get("draw") if isinstance(major.get("draw"), dict) else {}
    items = draw.get("items")
    if isinstance(items, list):
        for entry in items:
            if isinstance(entry, dict) and entry.get("src"):
                pics.append(str(entry["src"]))
    opus = major.get("opus") if isinstance(major.get("opus"), dict) else {}
    opus_pics = opus.get("pics")
    if isinstance(opus_pics, list):
        for entry in opus_pics:
            if isinstance(entry, dict) and entry.get("url"):
                pics.append(str(entry["url"]))
    return pics


def _dynamic_video(major: dict) -> dict | None:
    archive = major.get("archive")
    if not isinstance(archive, dict):
        return None
    bvid = str(archive.get("bvid") or "")
    title = str(archive.get("title") or "")
    if not (bvid or archive.get("jump_url")):
        return None
    return {"bvid": bvid, "title": title}


def _orig_author(orig: Any) -> str | None:
    if not isinstance(orig, dict):
        return None
    modules = orig.get("modules") if isinstance(orig.get("modules"), dict) else {}
    author = (
        modules.get("module_author")
        if isinstance(modules.get("module_author"), dict)
        else {}
    )
    name = str(author.get("name") or "")
    return name or None


__all__ = [
    "SPACE_FEED_PATH",
    "RM_DYNAMIC_URL",
    "DELETED_ALREADY_CODES",
    "iter_dynamics",
    "parse_dynamic",
    "delete_dynamic",
]
