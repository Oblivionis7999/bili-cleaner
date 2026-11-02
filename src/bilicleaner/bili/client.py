"""B 站 HTTP 客户端：统一 Session、wbi 签名、限速与错误码映射。

职责边界：
- 只负责"发请求 + 校验返回码"，不实现任务/备份/重试策略；
- 每次请求前调用注入的 ``limiter.wait()``（若提供 limiter）；
- 非 0 code 抛 ``errors`` 里的对应异常，由 services 层决定冷却或停止；
- 不在客户端内部重试：风控时反复重试只会放大风险（见 ADR-0002）。
"""

from __future__ import annotations

from typing import Any

import requests

from ..const import AUTH_CODES, RISK_CODES
from ..errors import AuthRequiredError, BiliApiError, RiskControlError
from .wbi import extract_wbi_keys, get_mixin_key, sign_params

API_BASE = "https://api.bilibili.com"
PASSPORT_BASE = "https://passport.bilibili.com"
VC_BASE = "https://api.vc.bilibili.com"
SPACE_BASE = "https://space.bilibili.com"
T_BASE = "https://t.bilibili.com"

# 桌面浏览器 UA。ADR-0002 明确不做 UA 伪装来绕风控，这里只是模拟正常网页访问。
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
)

DEFAULT_TIMEOUT = 20.0
# 所有分页生成器的安全上限：接口异常时不至于无限翻页
MAX_PAGES = 500

# HTTP 状态码层面的风控/登录失效
_HTTP_RISK = {412: -412, 429: -799}
_HTTP_AUTH = {401, 403}

NAV_PATH = "/x/web-interface/nav"


def _risk_error(code: int, message: str = "", path: str = "") -> RiskControlError:
    """构造 RiskControlError，并保留 errors.py 里约定的 ``code = "RISK_CONTROL"``。"""
    err = RiskControlError(code, message or f"触发风控（code={code}）", path)
    err.code = "RISK_CONTROL"
    return err


class BiliClient:
    """基于 ``requests.Session`` 的 B 站网页接口客户端。"""

    def __init__(
        self,
        store: Any = None,
        settings: dict | None = None,
        limiter: Any = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        """``store`` / ``limiter`` 可以为 None（单测或只读脚本用）。"""
        self.store = store
        self.settings = settings or {}
        self.limiter = limiter
        self.timeout = float(timeout)

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": DESKTOP_UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )

        self._img_key = ""
        self._sub_key = ""
        self.user: dict | None = None

        cookies = self._load_session_cookies()
        if cookies:
            self.session.cookies.update(cookies)

    # ------------------------------------------------------------------ 登录态

    def _load_session_cookies(self) -> dict:
        """从 Store 读取本地加密保存的登录态 Cookie。"""
        if self.store is None:
            return {}
        try:
            payload = self.store.load_session()
        except Exception:  # noqa: BLE001 - 登录态损坏时按未登录处理
            return {}
        cookies = (payload or {}).get("cookies") or {}
        if not isinstance(cookies, dict):
            return {}
        return {str(k): str(v) for k, v in cookies.items() if v is not None}

    @property
    def cookies(self) -> dict:
        """当前 Session 里的 Cookie（dict），供 API 层登录后写回 Store。"""
        return {cookie.name: cookie.value for cookie in self.session.cookies}

    @property
    def csrf(self) -> str:
        """写操作需要的 csrf（即 bili_jct Cookie）。"""
        return self.cookies.get("bili_jct", "")

    @property
    def user_mid(self) -> int:
        """当前登录用户 mid：优先内存里的 user，其次 DedeUserID Cookie。"""
        if isinstance(self.user, dict):
            mid = to_int(self.user.get("mid"))
            if mid > 0:
                return mid
        return to_int(self.cookies.get("DedeUserID"))

    @property
    def is_logged_in(self) -> bool:
        """粗略判断本地是否有登录态（不发起网络请求）。"""
        return bool(self.cookies.get("SESSDATA"))

    # ------------------------------------------------------------------ 请求

    def _full_url(self, path: str) -> str:
        """相对 path 拼到 api.bilibili.com；传入完整 URL 时原样使用。"""
        if path.startswith(("http://", "https://")):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return API_BASE + path

    def _request_headers(
        self, url: str, params: dict | None = None, extra: dict | None = None
    ) -> dict:
        """按接口设置 Referer / Origin（B 站对部分接口校验来源）。"""
        headers: dict[str, str] = {}
        if "polymer/web-dynamic/v1/feed/space" in url and isinstance(params, dict):
            host_mid = params.get("host_mid")
            if host_mid:
                headers["Referer"] = f"{SPACE_BASE}/{host_mid}/dynamic"
                headers["Origin"] = SPACE_BASE
        if "dynamic_svr" in url or url.startswith(VC_BASE):
            headers["Referer"] = f"{T_BASE}/"
            headers["Origin"] = T_BASE
        elif url.startswith(PASSPORT_BASE):
            headers["Referer"] = "https://www.bilibili.com/"
            headers["Origin"] = "https://www.bilibili.com"
        headers.setdefault("Referer", "https://www.bilibili.com/")
        headers.setdefault("Origin", "https://www.bilibili.com")
        if extra:
            headers.update({str(k): str(v) for k, v in extra.items()})
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        headers: dict | None = None,
    ) -> Any:
        """发一次请求；网络异常统一转成 BiliApiError。"""
        if self.limiter is not None:
            self.limiter.wait()
        kwargs: dict[str, Any] = {
            "headers": self._request_headers(url, params, headers),
            "timeout": self.timeout,
        }
        if params is not None:
            kwargs["params"] = params
        if data is not None:
            kwargs["data"] = data
        try:
            return self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise BiliApiError(-999, f"网络请求失败: {exc}", url) from exc

    def _decode(self, response: Any, path: str) -> dict:
        """把 HTTP 响应解成 JSON dict，并处理 HTTP 层面的风控/登录失败。"""
        status = to_int(getattr(response, "status_code", 200), 200)
        if status in _HTTP_RISK:
            raise _risk_error(_HTTP_RISK[status], f"请求被拦截（HTTP {status}）", path)
        if status in _HTTP_AUTH:
            raise AuthRequiredError(f"登录态失效（HTTP {status}）", http_status=status)
        if status >= 400:
            raise BiliApiError(-999, f"HTTP {status}", path)
        try:
            payload = response.json()
        except ValueError as exc:
            raise BiliApiError(-999, "响应不是合法 JSON，可能触发了风控", path) from exc
        if not isinstance(payload, dict):
            raise BiliApiError(-999, "响应 JSON 不是对象", path)
        return payload

    def _raise_for_code(self, code: int, message: str, path: str) -> None:
        """业务 code -> 异常：风控 / 登录失效 / 其他接口错误。"""
        if code == 0:
            return
        if code in RISK_CODES:
            raise _risk_error(code, message, path)
        if code in AUTH_CODES:
            raise AuthRequiredError(
                message or f"登录态失效（code={code}）", bili_code=code
            )
        raise BiliApiError(code, message, path)

    def _envelope(self, response: Any, path: str) -> dict:
        """检查 code 并返回完整 envelope ``{"code":0,"data":...}``。"""
        payload = self._decode(response, path)
        raw_code = payload.get("code")
        if raw_code is None:
            raise BiliApiError(-999, "响应缺少 code 字段", path)
        code = to_int(raw_code, -999)
        message = str(payload.get("message") or payload.get("msg") or "")
        self._raise_for_code(code, message, path)
        return payload

    # ------------------------------------------------------------------ 公开方法

    def get(self, path: str, params: dict | None = None, wbi: bool = False) -> dict:
        """GET 接口；``wbi=True`` 时自动签名。返回完整 envelope。"""
        url = self._full_url(path)
        query = dict(params or {})
        if wbi:
            query = sign_params(query, self._ensure_mixin_key())
        response = self._request("GET", url, params=query or None)
        return self._envelope(response, path)

    def post(self, path: str, data: dict, wbi: bool = False) -> dict:
        """POST 接口；自动补 csrf，返回完整 envelope。

        写接口（取关/删动态/收藏操作）都不需要 wbi，``wbi`` 仅为接口完整性保留：
        签名参数会放在 query 上，请求体保持原样。
        """
        url = self._full_url(path)
        payload = dict(data or {})
        if not payload.get("csrf") and self.csrf:
            payload["csrf"] = self.csrf
        query = None
        if wbi:
            query = sign_params(payload, self._ensure_mixin_key())
        response = self._request("POST", url, params=query, data=payload)
        return self._envelope(response, path)

    def nav(self) -> dict:
        """拉取 ``/x/web-interface/nav``：刷新 wbi 密钥并返回 data。

        未登录时 B 站返回 code=-101，这里不抛异常而是返回 data
        （``isLogin=False``），由 ``auth.fetch_me`` 判定；风控码仍然抛出。
        """
        response = self._request("GET", API_BASE + NAV_PATH)
        payload = self._decode(response, NAV_PATH)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        self._refresh_wbi_keys(data)
        raw_code = payload.get("code")
        code = to_int(raw_code, -999) if raw_code is not None else -999
        if code == 0:
            self.user = _user_from_nav(data) if data.get("isLogin") else None
            return data
        if code in AUTH_CODES:
            self.user = None
            return data
        self._raise_for_code(code, str(payload.get("message") or ""), NAV_PATH)
        return data

    def close(self) -> None:
        """关闭底层 Session。"""
        try:
            self.session.close()
        except Exception:  # noqa: BLE001 - 关闭失败无需影响退出流程
            pass

    # ------------------------------------------------------------------ wbi

    def _refresh_wbi_keys(self, data: dict) -> None:
        img_key, sub_key = extract_wbi_keys(data)
        if img_key and sub_key:
            self._img_key = img_key
            self._sub_key = sub_key

    def _ensure_mixin_key(self) -> str:
        """拿不到 key 时先刷新一次 nav，仍拿不到则视为需要重新登录。"""
        if not (self._img_key and self._sub_key):
            self.nav()
        if not (self._img_key and self._sub_key):
            raise AuthRequiredError("拿不到 wbi 签名密钥，可能需要重新登录")
        try:
            return get_mixin_key(self._img_key, self._sub_key)
        except ValueError as exc:
            raise AuthRequiredError(f"wbi 密钥异常: {exc}") from exc


def _user_from_nav(data: dict) -> dict:
    """把 nav 的 data 归一化成契约里的 User。"""
    vip = data.get("vip") if isinstance(data.get("vip"), dict) else {}
    return {
        "mid": to_int(data.get("mid")),
        "name": str(data.get("uname") or data.get("name") or ""),
        "face": str(data.get("face") or ""),
        "vip": bool(to_int(data.get("vipStatus")) or to_int(vip.get("vipStatus"))),
    }


# ---------------------------------------------------------------------- 公共解析工具


def to_int(value: Any, default: int = 0) -> int:
    """尽量把值转成 int；失败返回 default（解析容错用）。"""
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def csrf_of(client: Any) -> str:
    """取客户端的 csrf（bili_jct）；mock 客户端没有该属性时返回空串。"""
    value = getattr(client, "csrf", "") or ""
    if callable(value):  # 容错：有的实现会写成方法
        try:
            value = value()
        except Exception:  # noqa: BLE001
            value = ""
    value = str(value or "")
    if value:
        return value
    # 再退一步：从客户端 cookies 里找
    cookies = getattr(client, "cookies", None)
    if isinstance(cookies, dict):
        return str(cookies.get("bili_jct") or "")
    return ""


def unwrap(resp: Any) -> Any:
    """取 envelope 里的 data；如果拿到的已经是 data（mock 兼容）则原样返回。"""
    if isinstance(resp, dict) and "code" in resp and "data" in resp:
        return resp.get("data")
    return resp


def resolve_mid(client: Any) -> int:
    """当前登录用户 mid：优先客户端属性，其次显式调用 nav。"""
    for attr in ("user_mid", "mid", "uid"):
        value = getattr(client, attr, None)
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001
                continue
        mid = to_int(value)
        if mid > 0:
            return mid
    nav = getattr(client, "nav", None)
    if callable(nav):
        data = unwrap(nav())
        if isinstance(data, dict):
            mid = to_int(data.get("mid") or data.get("uid") or data.get("user_mid"))
            if mid > 0:
                return mid
    raise AuthRequiredError("无法确定当前登录用户 mid，请重新登录")


__all__ = [
    "API_BASE",
    "PASSPORT_BASE",
    "VC_BASE",
    "SPACE_BASE",
    "T_BASE",
    "DESKTOP_UA",
    "DEFAULT_TIMEOUT",
    "MAX_PAGES",
    "NAV_PATH",
    "BiliClient",
    "to_int",
    "csrf_of",
    "unwrap",
    "resolve_mid",
]
