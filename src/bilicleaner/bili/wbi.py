"""B 站 wbi 签名：mixin_key 生成与请求参数签名。

wbi 是 B 站网页端接口的请求签名方案，流程：
1. 从 ``/x/web-interface/nav`` 的 ``wbi_img`` 取 ``img_key`` / ``sub_key``（各 32 位）；
2. 两段拼成 64 位字符串，按官方混淆表重排后取前 32 位，得到 ``mixin_key``；
3. 请求参数加上 ``wts``（秒级时间戳），按 key 排序后 urlencode，
   末尾拼上 ``mixin_key`` 做 md5，得到 ``w_rid``。

客户端（client.py）负责自动获取和刷新 key，本模块只做纯计算，方便单测。
参考：SocialSisterYi/bilibili-API-collect 的 wbi 签名文档。
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from typing import Any

# 官方混淆表：把 img_key + sub_key（64 位）按下标重排
MIXIN_KEY_ENC_TAB: tuple[int, ...] = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
)

# mixin_key 只有前 32 位有效
MIXIN_KEY_LENGTH = 32

# 签名前必须从参数值里剔除的字符（官方实现如此）
SPECIAL_CHARS = "!'()*"


def key_from_url(url: str) -> str:
    """从 ``wbi_img`` 的 URL 中截出 32 位 key（取文件名、去扩展名）。"""
    if not url:
        return ""
    name = str(url).rsplit("/", 1)[-1]
    return name.split(".")[0]


def extract_wbi_keys(nav_data: dict) -> tuple[str, str]:
    """从 nav 返回的 data 中取出 (img_key, sub_key)，缺失时返回两个空串。"""
    if not isinstance(nav_data, dict):
        return "", ""
    wbi_img = nav_data.get("wbi_img")
    if not isinstance(wbi_img, dict):
        return "", ""
    return (
        key_from_url(wbi_img.get("img_url") or ""),
        key_from_url(wbi_img.get("sub_url") or ""),
    )


def get_mixin_key(img_key: str, sub_key: str) -> str:
    """用 img_key + sub_key 计算 mixin_key（32 位）。"""
    raw = f"{img_key or ''}{sub_key or ''}"
    if len(raw) < 64:
        raise ValueError(
            f"wbi key 长度不足：img_key={len(img_key or '')}，sub_key={len(sub_key or '')}"
        )
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:MIXIN_KEY_LENGTH]


def _clean_value(value: Any) -> str:
    """把参数值转成字符串，并剔除特殊字符。"""
    return "".join(ch for ch in str(value) if ch not in SPECIAL_CHARS)


def sign_params(params: dict, mixin_key: str, wts: int | None = None) -> dict:
    """给参数加上 ``wts`` / ``w_rid`` 并返回新 dict（不修改入参）。

    ``wts`` 可显式传入，便于测试稳定性；默认取当前秒级时间戳。
    """
    signed = {str(k): _clean_value(v) for k, v in (params or {}).items()}
    signed["wts"] = int(time.time()) if wts is None else int(wts)
    ordered = dict(sorted(signed.items()))
    query = urllib.parse.urlencode(ordered)
    ordered["w_rid"] = hashlib.md5((query + (mixin_key or "")).encode("utf-8")).hexdigest()
    return ordered
