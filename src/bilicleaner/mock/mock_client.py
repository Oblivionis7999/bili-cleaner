"""MockBiliClient：不连 B 站、可随机注入风控的内存版客户端。

两种用法：

1. **传输层替身（生产 mock 模式）**：``create_app(mock=True)`` 用
   ``MockBiliClient()`` 当 ``client_factory``，``bili.auth/follows/dynamics/
   favorites/toview`` 里的模块函数原样套在它身上跑。为此 ``get/post/nav``
   模拟了真实 B 站接口的 JSON envelope 与原始字段名。
2. **高层便捷方法（测试 / 调试 / 演示）**：``list_followings`` 等直接返回
   ``docs/API-CONTRACT.md`` 里的 dict 结构，省得再包一层 bili 模块。

行为开关：

- ``logged_in=True``：初始是否已登录（默认已登录，方便直接演示）。
- ``write_delay=0.0``：每次写操作前 sleep 的秒数，用来演示任务进度。
- ``risk_every=0``：每 N 次请求抛一次 ``RiskControlError(-352)``，0 表示关闭。
- ``qr_status_plan``：二维码轮询状态码序列，默认 86101(未扫) → 86090(已扫) → 0(确认)。

所有写操作（取关、删动态、清收藏夹、清稍后再看）都真的修改内存数据。
"""

from __future__ import annotations

import base64
import copy
import io
import threading
import time
import uuid
from typing import Any, Iterator

from ..const import DYNAMIC_TYPE_LABELS
from ..errors import AuthRequiredError, BiliApiError, RiskControlError
from .fixtures import DEFAULT_SEED, make_groups, make_mock_data

# 真实站点里 wbi 签名用的 key 图片（假值，仅用于让 bili.client 解析不报错）
_WBI_IMG = "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png"
_WBI_SUB = "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"

_QR_POLL_CODES = {
    0: "confirmed",
    86101: "waiting",
    86090: "scanned",
    86038: "expired",
}

# 1x1 透明 PNG，qrcode 库不可用时的兜底
_TINY_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


class _FakeSession:
    """只为了兼容 bili.client 暴露的 ``session`` 属性（cookies/headers/close）。"""

    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.headers: dict[str, str] = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            )
        }
        self.verify = True

    def close(self) -> None:  # pragma: no cover - 与 requests.Session 对齐
        pass


def _qr_png_data_uri(url: str) -> str:
    """把登录 url 画成二维码；没有 qrcode/pillow 时退回 1x1 PNG。"""
    try:
        import qrcode  # 延迟导入，mock 包本身不强制依赖

        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:  # pragma: no cover - 依赖缺失环境
        return _TINY_PNG


class MockBiliClient:
    """与 :class:`bilicleaner.bili.client.BiliClient` 同接口的内存 Mock 实现。"""

    #: 便于 API 层判断当前是不是 mock 模式
    mock = True
    is_mock = True

    def __init__(
        self,
        store: Any = None,
        settings: Any = None,
        limiter: Any = None,
        *,
        seed: int = DEFAULT_SEED,
        logged_in: bool = True,
        write_delay: float = 0.0,
        risk_every: int = 0,
        data: dict | None = None,
        qr_status_plan: list[int] | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.limiter = limiter
        self.seed = seed
        self.data: dict = copy.deepcopy(data) if data is not None else make_mock_data(seed)
        self._initial_data = copy.deepcopy(self.data)
        self._initial_logged_in = bool(logged_in)
        self._write_delay = max(0.0, float(write_delay or 0.0))
        self._risk_every = max(0, int(risk_every or 0))
        self._qr_plan = list(qr_status_plan) if qr_status_plan else [86101, 86090, 0]
        self._lock = threading.RLock()
        self._request_count = 0
        self._write_count = 0
        self._closed = False
        self._qr_states: dict[str, dict] = {}
        self._session = _FakeSession()
        self._cookies: dict[str, str] = {}
        self._logged_in = False
        self.set_logged_in(bool(logged_in))

    # ------------------------------------------------------------------ 基础属性

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<MockBiliClient logged_in={self._logged_in} "
            f"follows={len(self.data['follows'])} dynamics={len(self.data['dynamics'])}>"
        )

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    @property
    def user(self) -> dict | None:
        return self.data["user"] if self._logged_in else None

    @property
    def user_mid(self) -> int:
        return int(self.data["user"]["mid"]) if self._logged_in else 0

    @property
    def cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    @property
    def csrf(self) -> str:
        return self._cookies.get("bili_jct") or "mock_csrf"

    @property
    def session(self) -> _FakeSession:
        return self._session

    @property
    def mock_data(self) -> dict:
        return self.data

    # -------------------------------------------------------------- 登录态 / 生命周期

    def set_logged_in(self, value: bool) -> None:
        """模拟扫码成功 / 退出登录。"""
        with self._lock:
            self._logged_in = bool(value)
            if self._logged_in:
                user = self.data["user"]
                self._cookies = {
                    "SESSDATA": f"mock_sessdata_{uuid.uuid4().hex[:16]}",
                    "bili_jct": f"mock_csrf_{uuid.uuid4().hex[:8]}",
                    "DedeUserID": str(user["mid"]),
                    "DedeUserID__ckMd5": uuid.uuid4().hex[:16],
                    "sid": uuid.uuid4().hex[:8],
                }
            else:
                self._cookies = {}
            self._session.cookies = self._cookies

    def logout(self) -> None:
        self.set_logged_in(False)

    def reset(self) -> None:
        """恢复初始化时的数据与登录态（测试 / 前端演示刷数据用）。"""
        with self._lock:
            self.data = copy.deepcopy(self._initial_data)
            self._request_count = 0
            self._write_count = 0
            self._qr_states.clear()
            self.set_logged_in(self._initial_logged_in)

    def close(self) -> None:
        """可重复调用，不抛异常（FastAPI shutdown 会调用）。"""
        self._closed = True

    # -------------------------------------------------------------- 请求节流 / 风控

    def _normalize_path(self, path: Any) -> str:
        text = str(path or "")
        if "?" in text:
            text = text.split("?", 1)[0]
        return text.strip()

    def _tick(self, *, write: bool = False, path: str = "") -> None:
        """计数 + 假风控注入 + 写操作延迟。"""
        with self._lock:
            self._request_count += 1
            count = self._request_count
        if self._risk_every and count % self._risk_every == 0:
            raise RiskControlError(-352, "请求过于频繁，请稍后再试", path)
        if write:
            with self._lock:
                self._write_count += 1
            if self._write_delay > 0:
                time.sleep(self._write_delay)

    def _require_login(self, path: str = "") -> None:
        if not self._logged_in:
            raise AuthRequiredError(f"未登录（mock）：{path or '接口'}")

    @staticmethod
    def _envelope(data: Any = None) -> dict:
        return {"code": 0, "message": "0", "ttl": 1, "data": {} if data is None else data}

    # -------------------------------------------------------------- 底层接口（兼容 BiliClient）

    def nav(self) -> dict:
        """返回 /x/web-interface/nav 的 data 部分（与 BiliClient.nav 对齐）。

        与真实 ``BiliClient.nav`` 一致：未登录时**不抛异常**，而是返回
        ``isLogin=False`` 的 data（``bili.auth.fetch_me`` 据此判定未登录）。
        风控注入仍会抛出。
        """
        self._tick(path="/x/web-interface/nav")
        if not self._logged_in:
            return {
                "isLogin": False,
                "mid": 0,
                "uname": "",
                "face": "",
                "vipStatus": 0,
                "vip": {"type": 0, "status": 0},
                "level_info": {"current_level": 0},
                "wbi_img": {"img_url": _WBI_IMG, "sub_url": _WBI_SUB},
                "money": 0,
                "cookie_refresh": 0,
            }
        return self._nav_data()

    def get(self, path: str, params: dict | None = None, wbi: bool = False) -> dict:
        """模拟 GET，返回完整 envelope ``{"code":0,"data":...}``。"""
        normalized = self._normalize_path(path)
        self._tick(path=normalized)
        params = dict(params or {})
        low = normalized.lower()

        # 登录相关接口不需要登录态
        if "qrcode/generate" in low:
            return self._envelope(self._qr_generate())
        if "qrcode/poll" in low:
            return self._envelope(self._qr_poll(params))

        self._require_login(normalized)

        if "web-interface/nav" in low:
            return self._envelope(self._nav_data())
        if "relation/followings" in low:
            return self._envelope(self._followings_raw(params))
        if "relation/tags" in low:
            return self._envelope(make_groups(self.data["follows"]))
        if "relation" in low:
            return self._envelope(self._relation_raw(params))
        if "web-dynamic/v1/feed/space" in low or "feed/space" in low:
            return self._envelope(self._dynamics_raw(params))
        if "fav/folder/created/list-all" in low or "fav/folder/created/list" in low:
            return self._envelope(self._fav_folders_raw())
        if "fav/resource/list" in low:
            return self._envelope(self._fav_list_raw(params))
        if "history/toview" in low:
            if "web" in low.split("history/toview")[-1]:
                # /x/v2/history/toview/web 之类不区分，统一返回列表
                return self._envelope(self._toview_raw())
            return self._envelope(self._toview_raw())

        raise BiliApiError(-404, f"Mock 未实现的 GET 接口：{normalized}", normalized)

    def post(self, path: str, data: dict | None = None, wbi: bool = False) -> dict:
        """模拟 POST，返回完整 envelope；写操作会按需延迟与注入风控。"""
        normalized = self._normalize_path(path)
        payload = dict(data or {})
        self._tick(write=True, path=normalized)
        low = normalized.lower()

        if "relation/modify" in low:
            self._require_login(normalized)
            return self._envelope(self._unfollow_raw(payload))

        self._require_login(normalized)

        if "rm_dynamic" in low or "remove/dynamic" in low or "feed/remove" in low:
            return self._envelope(self._delete_dynamic_raw(payload))
        if "batch-del" in low:
            return self._envelope(self._batch_delete_raw(payload))
        if "fav/resource/clean" in low:
            return self._envelope(self._clean_invalid_raw(payload))
        if "history/toview/clear" in low:
            return self._envelope(self._clear_toview_raw())
        if "logout" in low:
            self.logout()
            return self._envelope({})

        raise BiliApiError(-404, f"Mock 未实现的 POST 接口：{normalized}", normalized)

    # -------------------------------------------------------------- 登录 / 二维码

    def _nav_data(self) -> dict:
        user = self.data["user"]
        return {
            "isLogin": True,
            "mid": user["mid"],
            "uname": user["name"],
            "face": user["face"],
            "vipStatus": 1 if user.get("vip") else 0,
            "vip": {"type": 0, "status": 0},
            "level_info": {"current_level": 5},
            "wbi_img": {"img_url": _WBI_IMG, "sub_url": _WBI_SUB},
            "money": 0,
            "cookie_refresh": 0,
            "email_verified": 0,
            "mobile_verified": 0,
        }

    def _qr_generate(self) -> dict:
        key = uuid.uuid4().hex  # 32 位，与真实 qrcode_key 等长
        url = f"https://passport.bilibili.com/h5-app/passport/login/scan?navhide=1&key={key}"
        self._qr_states[key] = {"polls": 0, "url": url}
        return {"url": url, "qrcode_key": key, "expires_in": 180}

    def _qr_poll(self, params: dict) -> dict:
        key = str(params.get("qrcode_key") or params.get("key") or "")
        state = self._qr_states.get(key)
        if state is None:
            return {"code": 86038, "message": "二维码已失效", "url": ""}
        index = min(state["polls"], len(self._qr_plan) - 1)
        code = int(self._qr_plan[index])
        state["polls"] += 1
        if code == 0:
            self.set_logged_in(True)
            return {"code": 0, "message": "0", "url": "https://www.bilibili.com/", "refresh_token": "mock_refresh_token"}
        if code == 86090:
            return {"code": 86090, "message": "扫码成功，请在手机上确认", "url": ""}
        if code == 86038:
            return {"code": 86038, "message": "二维码已失效", "url": ""}
        return {"code": 86101, "message": "未扫码", "url": ""}

    def qrcode_generate(self) -> dict:
        """高层：返回契约里的 QRCode。"""
        env = self.get("/x/passport-login/web/qrcode/generate", {"source": "main-fe-header"})
        info = env["data"]
        return {
            "url": info["url"],
            "key": info["qrcode_key"],
            "expires_in": 180,
            "qr_png": _qr_png_data_uri(info["url"]),
        }

    def qrcode_poll(self, key: str) -> dict:
        """高层：返回契约里的 QRPoll，状态流转 waiting → scanned → confirmed。"""
        env = self.get(
            "/x/passport-login/web/qrcode/poll",
            {"qrcode_key": key, "source": "main-fe-header"},
        )
        data = env["data"]
        status = _QR_POLL_CODES.get(int(data.get("code", 86101)), "waiting")
        user = self.data["user"] if status == "confirmed" else None
        return {"status": status, "user": user}

    def fetch_me(self) -> dict:
        """高层：返回契约里的 User；未登录抛 AuthRequiredError。"""
        self._require_login("/x/web-interface/nav")
        return copy.deepcopy(self.data["user"])

    # -------------------------------------------------------------- 关注

    def _find_follow(self, mid: int) -> dict | None:
        for item in self.data["follows"]:
            if int(item["mid"]) == int(mid):
                return item
        return None

    def _follow_raw(self, item: dict) -> dict:
        official = item.get("official") or ""
        return {
            "mid": int(item["mid"]),
            "uname": item["name"],
            "face": item["face"],
            "sign": item["sign"],
            "attribute": int(item["attribute"]),
            "official_verify": {"type": 1 if official else -1, "desc": official},
            "tag": list(item.get("group_ids") or []),
            "mtime": int(item["mtime"]),
            "special": 0,
            "vip": {"type": 0, "status": 0},
            "contract_info": {"is_contractor": False, "ts": 0},
        }

    def _followings_raw(self, params: dict) -> dict:
        pn = max(1, self._as_int(params.get("pn"), 1))
        ps = max(1, self._as_int(params.get("ps"), 50))
        follows = self.data["follows"]
        total = len(follows)
        start = (pn - 1) * ps
        page = follows[start:start + ps]
        return {"total": total, "list": [self._follow_raw(f) for f in page]}

    def _relation_raw(self, params: dict) -> dict:
        fid = self._as_int(params.get("fid"), 0) or self._as_int(params.get("mid"), 0)
        item = self._find_follow(fid) if fid else None
        if item is None:
            return {"mid": str(fid), "attribute": 0, "mtime": 0, "tag": [], "special": 0}
        return {
            "mid": str(fid),
            "attribute": int(item["attribute"]),
            "mtime": int(item["mtime"]),
            "tag": list(item.get("group_ids") or []),
            "special": 0,
        }

    def _unfollow_raw(self, payload: dict) -> dict:
        fid = self._as_int(payload.get("fid"), 0) or self._as_int(payload.get("mid"), 0)
        act = str(payload.get("act", "2"))
        if act == "2" and fid:
            with self._lock:
                follows = self.data["follows"]
                self.data["follows"] = [f for f in follows if int(f["mid"]) != fid]
        return {}

    # ---- 高层关注方法

    def list_groups(self) -> list[dict]:
        self._tick(path="/x/relation/tags")
        self._require_login("/x/relation/tags")
        return make_groups(self.data["follows"])

    def list_followings(
        self,
        offset: int = 0,
        limit: int = 50,
        q: str = "",
        category: str = "",
        group: str = "",
        mutual: Any = None,
    ) -> dict:
        """契约形状的 FollowList，支持搜索 / 分类 / 分组 / 互关筛选。"""
        self._tick(path="/x/relation/followings")
        self._require_login("/x/relation/followings")
        offset = max(0, self._as_int(offset, 0))
        limit = max(1, self._as_int(limit, 50))
        keyword = str(q or "").strip().lower()
        category = str(category or "").strip()
        group = str(group or "").strip()
        mutual_flag = self._as_bool(mutual)

        items = self.data["follows"]
        if keyword:
            items = [
                f for f in items
                if keyword in str(f["name"]).lower() or keyword in str(f["sign"]).lower()
            ]
        if category:
            items = [f for f in items if f.get("category") == category]
        if group:
            items = [
                f for f in items
                if group in (f.get("groups") or []) or group in [str(g) for g in (f.get("group_ids") or [])]
            ]
        if mutual_flag is not None:
            items = [f for f in items if bool(f.get("mutual")) is mutual_flag]

        total = len(items)
        page = copy.deepcopy(items[offset:offset + limit])
        return {"total": total, "offset": offset, "items": page}

    def iter_followings(self) -> Iterator[dict]:
        """逐个产出关注（内部按 50 条一页模拟翻页）。"""
        offset = 0
        while True:
            page = self.list_followings(offset=offset, limit=50)
            items = page["items"]
            if not items:
                return
            yield from items
            offset += len(items)
            if offset >= page["total"]:
                return

    def unfollow(self, mid: int) -> None:
        self.post("/x/relation/modify", {"fid": mid, "act": 2, "re_src": 11})

    def get_relation(self, mid: int) -> dict:
        self._tick(path="/x/relation")
        self._require_login("/x/relation")
        item = self._find_follow(mid)
        if item is None:
            return {"mid": int(mid), "attribute": 0, "followed": False, "mutual": False, "mtime": 0, "tag": []}
        return {
            "mid": int(mid),
            "attribute": int(item["attribute"]),
            "followed": True,
            "mutual": bool(item.get("mutual")),
            "mtime": int(item["mtime"]),
            "tag": list(item.get("group_ids") or []),
        }

    def set_follow_category(self, mid: int, category: str, source: str = "user") -> bool:
        """回写分类（/api/follows/classify 与 classify_llm 任务用）。"""
        with self._lock:
            item = self._find_follow(mid)
            if item is None:
                return False
            item["category"] = category
            item["category_source"] = source
            return True

    def set_categories(self, mapping: dict[int, str], source: str = "user") -> int:
        """批量回写分类，返回更新成功条数。"""
        updated = 0
        for mid, category in mapping.items():
            if self.set_follow_category(int(mid), str(category), source):
                updated += 1
        return updated

    # -------------------------------------------------------------- 动态

    def _find_dynamic(self, dyn_id: str) -> tuple[int, dict] | None:
        target = str(dyn_id)
        for index, item in enumerate(self.data["dynamics"]):
            if item["id"] == target:
                return index, item
        return None

    def _dynamic_images(self, dyn: dict, bucket: str) -> list[dict]:
        # 用 id 的数字后缀稳定地决定图片数量（1~3 张）
        try:
            count = 1 + int(str(dyn["id"])[-2:]) % 3
        except (ValueError, KeyError):
            count = 1
        return [
            {"src": f"https://i0.hdslb.com/bfs/new_dyn/{bucket}_{dyn['id']}_{i}.jpg"}
            for i in range(count)
        ]

    def _dynamic_raw(self, item: dict) -> dict:
        text = item.get("text") or ""
        dtype = item.get("type") or "DYNAMIC_TYPE_WORD"
        module_dynamic: dict[str, Any] = {}
        if text:
            module_dynamic["desc"] = {"text": text, "rich_text_nodes": []}

        major: dict[str, Any] = {}
        if dtype == "DYNAMIC_TYPE_DRAW":
            major["draw"] = {"items": self._dynamic_images(item, "draw")}
            major["opus"] = {"pics": [{"url": img["src"]} for img in major["draw"]["items"]]}
        elif dtype == "DYNAMIC_TYPE_AV" and item.get("video"):
            major["archive"] = {
                "bvid": item["video"]["bvid"],
                "title": item["video"]["title"],
                "cover": f"https://i0.hdslb.com/bfs/archive/{item['id']}.jpg",
                "desc": item["video"]["title"],
            }
        elif dtype == "DYNAMIC_TYPE_ARTICLE":
            major["article"] = {"title": text, "covers": []}
            major["opus"] = {"title": text, "pics": []}
        if major:
            module_dynamic["major"] = major

        user = self.data["user"]
        raw: dict[str, Any] = {
            "id_str": str(item["id"]),
            "type": dtype,
            "type_label": DYNAMIC_TYPE_LABELS.get(dtype, "动态"),
            "modules": {
                "module_author": {
                    "mid": user["mid"],
                    "name": user["name"],
                    "face": user["face"],
                    "pub_ts": int(item["pub_ts"]),
                    "pub_time": "",
                },
                "module_dynamic": module_dynamic,
            },
        }

        if dtype == "DYNAMIC_TYPE_FORWARD":
            orig_dynamic: dict[str, Any] = {}
            if item.get("has_image"):
                orig_dynamic["major"] = {"draw": {"items": self._dynamic_images(item, "orig")}}
            if item.get("video"):
                orig_dynamic["major"] = {
                    "archive": {
                        "bvid": item["video"]["bvid"],
                        "title": item["video"]["title"],
                    }
                }
            raw["orig"] = {
                "id_str": str(int(item["id"]) - 1),
                "type": "DYNAMIC_TYPE_DRAW",
                "modules": {
                    "module_author": {"name": item.get("orig_author") or "未知UP主"},
                    "module_dynamic": orig_dynamic,
                },
            }
        return raw

    def _dynamics_raw(self, params: dict) -> dict:
        offset_text = str(params.get("offset") or "").strip()
        start = int(offset_text) if offset_text.isdigit() else 0
        page_size = 20
        dynamics = self.data["dynamics"]
        page = dynamics[start:start + page_size]
        has_more = start + page_size < len(dynamics)
        return {
            "items": [self._dynamic_raw(item) for item in page],
            "offset": str(start + page_size) if has_more else "",
            "has_more": has_more,
        }

    def _delete_dynamic_raw(self, payload: dict) -> dict:
        dyn_id = str(payload.get("dynamic_id") or payload.get("id") or "")
        found = self._find_dynamic(dyn_id)
        if found is None:
            raise BiliApiError(500404, "动态不存在或已删除", "rm_dynamic")
        index, _ = found
        with self._lock:
            del self.data["dynamics"][index]
        return {}

    # ---- 高层动态方法

    def list_dynamics(
        self,
        offset: Any = "",
        limit: int = 20,
        q: str = "",
        type_: str = "",
        **kwargs: Any,
    ) -> dict:
        """契约形状的 DynamicList；``type_`` 也接受 type/type_label。"""
        self._tick(path="/x/polymer/web-dynamic/v1/feed/space")
        self._require_login("/x/polymer/web-dynamic/v1/feed/space")
        offset_text = str(offset or "").strip()
        start = int(offset_text) if offset_text.isdigit() else 0
        limit = max(1, self._as_int(limit, 20))
        keyword = str(q or "").strip().lower()
        wanted = str(type_ or kwargs.get("type") or kwargs.get("type_label") or "").strip()

        items = self.data["dynamics"]
        if keyword:
            items = [
                d for d in items
                if keyword in str(d.get("text", "")).lower()
                or keyword in str((d.get("video") or {}).get("title", "")).lower()
            ]
        if wanted:
            items = [
                d for d in items
                if wanted in (d.get("type"), d.get("type_label"))
            ]

        total = len(items)
        page = copy.deepcopy(items[start:start + limit])
        next_offset = str(start + limit) if start + limit < total else ""
        return {"offset": next_offset, "has_more": bool(next_offset), "items": page}

    def iter_dynamics(self, mid: int | None = None) -> Iterator[dict]:
        offset: Any = ""
        while True:
            page = self.list_dynamics(offset=offset)
            items = page["items"]
            if not items:
                return
            yield from items
            offset = page["offset"]
            if not page["has_more"]:
                return

    def delete_dynamic(self, dyn_id: str) -> None:
        self.post("/x/dynamic/feed/remove/dynamic", {"dynamic_id": str(dyn_id)})

    # -------------------------------------------------------------- 收藏夹

    def _find_folder(self, media_id: Any) -> tuple[int, dict] | None:
        target = self._as_int(media_id, -1)
        for index, folder in enumerate(self.data["folders"]):
            if int(folder["media_id"]) == target:
                return index, folder
        return None

    def _folder_items(self, media_id: Any) -> list[dict]:
        target = self._as_int(media_id, -1)
        found = self._find_folder(target)
        if found is None:
            raise BiliApiError(-400, "收藏夹不存在", "fav/resource/list")
        return self.data["favorites"][int(found[1]["media_id"])]

    def _fav_folders_raw(self) -> dict:
        result = []
        for folder in self.data["folders"]:
            items = self.data["favorites"].get(int(folder["media_id"]), [])
            result.append(
                {
                    "id": int(folder["media_id"]),
                    "fid": int(folder["media_id"]),
                    "mid": self.user_mid,
                    "title": folder["title"],
                    "media_count": len(items),
                    "attr": 0,
                    "fav_state": 1,
                    "is_default": bool(folder.get("is_default")),
                }
            )
        return {"count": len(result), "list": result}

    def _fav_list_raw(self, params: dict) -> dict:
        media_id = self._as_int(params.get("media_id") or params.get("mediaId"), -1)
        folder_found = self._find_folder(media_id)
        if folder_found is None:
            raise BiliApiError(-400, "收藏夹不存在", "fav/resource/list")
        _, folder = folder_found
        items = self.data["favorites"].get(media_id, [])
        keyword = str(params.get("keyword") or "").strip()
        filtered = [it for it in items if keyword in str(it.get("title", ""))] if keyword else items
        pn = max(1, self._as_int(params.get("pn"), 1))
        ps = max(1, self._as_int(params.get("ps"), 20))
        start = (pn - 1) * ps
        page = filtered[start:start + ps]
        medias = []
        for item in page:
            medias.append(
                {
                    "id": int(item["id"]),
                    "type": int(item.get("type", 2)),
                    "title": item["title"],
                    "cover": item["cover"],
                    "upper": {"name": item["upper"], "mid": 0},
                    "bvid": item["bvid"],
                    "fav_time": int(item["fav_time"]),
                    "attr": 9 if item.get("invalid") else 0,
                    "cnt_info": {"collect": 0, "play": 0, "danmaku": 0},
                }
            )
        info = {
            "id": media_id,
            "title": folder["title"],
            "media_count": len(items),
            "attr": 0,
            "fav_state": 1,
        }
        return {"info": info, "medias": medias, "has_more": start + ps < len(filtered), "ttl": 1}

    def _batch_delete_raw(self, payload: dict) -> dict:
        media_id = self._as_int(payload.get("media_id") or payload.get("mediaId"), -1)
        raw_resources = payload.get("resources") or payload.get("ids") or ""
        if isinstance(raw_resources, str):
            entries = [entry for entry in raw_resources.split(",") if entry.strip()]
        else:
            entries = list(raw_resources)
        wanted: set[str] = set()
        for entry in entries:
            wanted.add(str(entry).strip().split(":", 1)[0])
        items = self._folder_items(media_id)
        with self._lock:
            keep = [it for it in items if str(it["id"]) not in wanted]
            self.data["favorites"][media_id] = keep
        return {"count": len(items) - len(keep)}

    def _clean_invalid_raw(self, payload: dict) -> dict:
        media_id = self._as_int(payload.get("media_id") or payload.get("mediaId"), -1)
        items = self._folder_items(media_id)
        with self._lock:
            keep = [it for it in items if not it.get("invalid")]
            self.data["favorites"][media_id] = keep
        return {"count": len(items) - len(keep)}

    # ---- 高层收藏夹方法

    def list_folders(self) -> list[dict]:
        self._tick(path="/x/v3/fav/folder/created/list-all")
        self._require_login("/x/v3/fav/folder/created/list-all")
        folders = []
        for folder in self.data["folders"]:
            items = self.data["favorites"].get(int(folder["media_id"]), [])
            folders.append(
                {
                    "media_id": int(folder["media_id"]),
                    "title": folder["title"],
                    "count": len(items),
                    "is_default": bool(folder.get("is_default")),
                    "privacy": int(folder.get("privacy", 0)),
                }
            )
        return folders

    def list_fav_items(
        self,
        media_id: int,
        offset: int = 0,
        limit: int = 20,
        q: str = "",
    ) -> dict:
        self._tick(path="/x/v3/fav/resource/list")
        self._require_login("/x/v3/fav/resource/list")
        offset = max(0, self._as_int(offset, 0))
        limit = max(1, self._as_int(limit, 20))
        keyword = str(q or "").strip().lower()
        items = self._folder_items(media_id)
        if keyword:
            items = [it for it in items if keyword in str(it.get("title", "")).lower()]
        total = len(items)
        page = copy.deepcopy(items[offset:offset + limit])
        return {"total": total, "offset": offset, "items": page}

    def iter_items(self, media_id: int) -> Iterator[dict]:
        offset = 0
        while True:
            page = self.list_fav_items(media_id, offset=offset, limit=20)
            items = page["items"]
            if not items:
                return
            yield from items
            offset += len(items)
            if offset >= page["total"]:
                return

    def batch_delete(self, media_id: int, resources: list[str]) -> dict:
        return self.post(
            "/x/v3/fav/resource/batch-del",
            {
                "media_id": int(media_id),
                "resources": ",".join(str(r) for r in resources),
                "platform": "web",
            },
        )["data"]

    def clean_invalid(self, media_id: int) -> dict:
        return self.post("/x/v3/fav/resource/clean", {"media_id": int(media_id)})["data"]

    # -------------------------------------------------------------- 稍后再看

    def _toview_raw(self) -> dict:
        items = self.data["toview"]
        serialized = []
        for item in items:
            serialized.append(
                {
                    "aid": int(item["aid"]),
                    "title": item["title"],
                    "owner": {"name": item["upper"], "mid": 0},
                    "duration": int(item["duration"]),
                    "progress": int(item["progress"]),
                    "add_at": int(item["add_ts"]),
                    "add_ts": int(item["add_ts"]),
                    "bvid": item["bvid"],
                    "cover": f"https://i0.hdslb.com/bfs/archive/{item['aid']}.jpg",
                }
            )
        return {"count": len(items), "list": serialized}

    def _clear_toview_raw(self) -> dict:
        with self._lock:
            count = len(self.data["toview"])
            self.data["toview"] = []
        return {"count": count}

    # ---- 高层稍后再看方法

    def list_toview(self) -> list[dict]:
        self._tick(path="/x/v2/history/toview")
        self._require_login("/x/v2/history/toview")
        return copy.deepcopy(self.data["toview"])

    def list_toview_page(self, offset: int = 0, limit: int = 50) -> dict:
        offset = max(0, self._as_int(offset, 0))
        limit = max(1, self._as_int(limit, 50))
        items = self.list_toview()
        return {"total": len(items), "offset": offset, "items": items[offset:offset + limit]}

    def clear_toview(self) -> dict:
        return self.post("/x/v2/history/toview/clear", {})["data"]

    # -------------------------------------------------------------- 小工具

    @staticmethod
    def _as_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_bool(value: Any) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("", "all", "any"):
            return None
        return text in ("1", "true", "yes", "on")


__all__ = ["MockBiliClient", "_FakeSession"]
