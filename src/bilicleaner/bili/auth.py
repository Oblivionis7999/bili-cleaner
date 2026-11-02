"""扫码登录与当前用户信息。

契约：``qrcode_generate`` / ``qrcode_poll`` / ``fetch_me``。
登录成功后 Cookie 由 requests.Session 自动持有，API 层再调用
``store.save_session(client.cookies, user)`` 落盘（本模块不碰 Store）。
"""

from __future__ import annotations

import base64
import io
from typing import Any

from ..errors import AuthRequiredError, BiliApiError, BiliCleanerError, RiskControlError
from .client import PASSPORT_BASE, to_int, unwrap

QRCODE_GENERATE_PATH = PASSPORT_BASE + "/x/passport-login/web/qrcode/generate"
QRCODE_POLL_PATH = PASSPORT_BASE + "/x/passport-login/web/qrcode/poll"
QRCODE_EXPIRES_IN = 180
_QR_SOURCE = "main-fe-header"

# B 站轮询接口里的 data.code -> 契约 QRPoll.status
_POLL_STATUS = {
    0: "confirmed",
    86101: "waiting",
    86090: "scanned",
    86038: "expired",
}

# 轮询时 qrcode_key 无效/过期的业务码，直接按 expired 处理
_POLL_EXPIRED_CODES = {-400, -404}


def qrcode_generate(client: Any) -> dict:
    """生成登录二维码，返回契约里的 QRCode（qr_png 由本地渲染）。"""
    payload = client.get(QRCODE_GENERATE_PATH, {"source": _QR_SOURCE})
    data = unwrap(payload)
    if not isinstance(data, dict):
        data = {}
    url = str(data.get("url") or "")
    key = str(data.get("qrcode_key") or "") or str(data.get("key") or "")
    if not url or not key:
        raise BiliApiError(-999, "二维码接口返回缺少 url/qrcode_key", QRCODE_GENERATE_PATH)
    return {
        "url": url,
        "key": key,
        "expires_in": QRCODE_EXPIRES_IN,
        "qr_png": _qr_png_data_uri(url),
    }


def qrcode_poll(client: Any, key: str) -> dict:
    """轮询扫码状态；confirmed 时附带当前用户（取不到则为 None）。"""
    try:
        payload = client.get(
            QRCODE_POLL_PATH, {"qrcode_key": str(key or ""), "source": _QR_SOURCE}
        )
    except RiskControlError:
        raise
    except AuthRequiredError:
        raise
    except BiliApiError as exc:
        if getattr(exc, "bili_code", None) in _POLL_EXPIRED_CODES:
            return {"status": "expired", "user": None}
        raise
    data = unwrap(payload)
    if not isinstance(data, dict):
        data = {}
    status = _POLL_STATUS.get(to_int(data.get("code"), -1), "waiting")
    user = None
    if status == "confirmed":
        try:
            user = fetch_me(client, refresh=True)
        except BiliCleanerError:  # 刚登录时 nav 偶发失败，前端可稍后再拉 /api/me
            user = None
    return {"status": status, "user": user}


def fetch_me(client: Any, refresh: bool = False) -> dict:
    """返回当前登录用户 User；未登录或登录失效抛 AuthRequiredError。"""
    if not refresh:
        cached = getattr(client, "user", None)
        if isinstance(cached, dict):
            user = _normalize_user(cached)
            if user["mid"] > 0:
                return user
    data = unwrap(client.nav())
    if not isinstance(data, dict) or data.get("isLogin") is False:
        raise AuthRequiredError("未登录或登录已过期")
    user = _normalize_user(data)
    if user["mid"] <= 0:
        raise AuthRequiredError("未登录或登录已过期")
    try:
        setattr(client, "user", user)
    except Exception:  # noqa: BLE001 - 只做缓存，失败不影响结果
        pass
    return user


def _normalize_user(raw: dict) -> dict:
    """把 nav 数据归一化成契约里的 User（也兼容已经归一化过的缓存）。"""
    vip_raw = raw.get("vip")
    if isinstance(vip_raw, dict):
        vip = bool(to_int(raw.get("vipStatus")) or to_int(vip_raw.get("vipStatus")))
    else:
        # 缓存里的 vip 已经是 bool
        vip = bool(vip_raw) or bool(to_int(raw.get("vipStatus")))
    face = str(raw.get("face") or "")
    if face.startswith("//"):
        face = "https:" + face
    return {
        "mid": to_int(raw.get("mid")),
        "name": str(raw.get("uname") or raw.get("name") or ""),
        "face": face,
        "vip": vip,
    }


def _qr_png_data_uri(url: str) -> str:
    """用 qrcode 库把登录链接渲染成 PNG data URI；库缺失时返回空串。"""
    try:
        import qrcode

        qr = qrcode.QRCode(box_size=8, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return "data:image/png;base64," + encoded
    except Exception:  # noqa: BLE001 - 渲染失败时前端仍可显示链接/降级提示
        return ""


__all__ = [
    "QRCODE_GENERATE_PATH",
    "QRCODE_POLL_PATH",
    "QRCODE_EXPIRES_IN",
    "qrcode_generate",
    "qrcode_poll",
    "fetch_me",
]
