"""B 站接口层单元测试。

全部用假的 ``requests.Session.request`` 响应，不访问真实 B 站网络。
覆盖：wbi 签名稳定性、错误码映射、分页解析（关注/动态/收藏/稍后再看）、
扫码登录状态机，以及各模块返回结构是否符合 docs/API-CONTRACT.md。
"""

from __future__ import annotations

import base64
import json
from urllib.parse import urlsplit

import pytest
import requests

from bilicleaner.bili import auth, dynamics, favorites, follows, toview, wbi
from bilicleaner.bili.client import (
    PASSPORT_BASE,
    VC_BASE,
    BiliClient,
    csrf_of,
    resolve_mid,
    to_int,
    unwrap,
)
from bilicleaner.const import AUTH_CODES, RISK_CODES
from bilicleaner.errors import AuthRequiredError, BiliApiError, RiskControlError
from bilicleaner.store import Store

# --------------------------------------------------------------------------- 常量

IMG_KEY = "7cd084941338484aae1ad9425b84077c"
SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"
MIXIN_KEY = "ea1db124af3c7062474693fa704f4ff8"

NAV_PATH = "/x/web-interface/nav"
FOLLOWINGS_PATH = "/x/relation/followings"
RELATION_PATH = "/x/relation"
TAGS_PATH = "/x/relation/tags"
MODIFY_PATH = "/x/relation/modify"
FEED_PATH = "/x/polymer/web-dynamic/v1/feed/space"
RM_DYNAMIC_PATH = "/dynamic_svr/v1/dynamic_svr/rm_dynamic"
FOLDERS_PATH = "/x/v3/fav/folder/created/list-all"
RESOURCE_LIST_PATH = "/x/v3/fav/resource/list"
BATCH_DEL_PATH = "/x/v3/fav/resource/batch-del"
CLEAN_PATH = "/x/v3/fav/resource/clean"
TOVIEW_PATH = "/x/v2/history/toview"
TOVIEW_CLEAR_PATH = "/x/v2/history/toview/clear"
QR_GENERATE_PATH = "/x/passport-login/web/qrcode/generate"
QR_POLL_PATH = "/x/passport-login/web/qrcode/poll"

USER_MID = 123456789

FOLLOW_KEYS = {
    "mid", "name", "sign", "face", "official", "attribute", "mutual",
    "groups", "group_ids", "order", "mtime", "category", "category_source",
}
DYNAMIC_KEYS = {
    "id", "type", "type_label", "pub_ts", "text", "has_image", "video", "orig_author",
}
FAV_FOLDER_KEYS = {"media_id", "title", "count", "is_default", "privacy"}
FAV_ITEM_KEYS = {"id", "type", "title", "upper", "bvid", "cover", "fav_time", "invalid"}
TOVIEW_KEYS = {"aid", "title", "upper", "duration", "progress", "add_ts", "bvid"}
USER_KEYS = {"mid", "name", "face", "vip"}
QRCODE_KEYS = {"url", "key", "expires_in", "qr_png"}
QRPOLL_KEYS = {"status", "user"}
GROUP_KEYS = {"tagid", "name", "count"}

NAV_DATA = {
    "isLogin": True,
    "mid": USER_MID,
    "uname": "不会数学的sky",
    "face": "https://i0.hdslb.com/bfs/face/abc.jpg",
    "vipStatus": 1,
    "wbi_img": {
        "img_url": f"https://i0.hdslb.com/bfs/wbi/{IMG_KEY}.png",
        "sub_url": f"https://i0.hdslb.com/bfs/wbi/{SUB_KEY}.png",
    },
}

# 官方文档中的 wbi 示例参数
WBI_DEMO_PARAMS = {"foo": "114", "bar": "514", "zab": 1919810}
WBI_DEMO_WTS = 1702204169
WBI_DEMO_RID = "8f6f2b5b3d485fe1886cec6a0be8c5d4"
WBI_FILTERED_RID = "cf0d3770f55dbffd4b0c629c046ed422"


# --------------------------------------------------------------------------- 测试替身


class FakeResponse:
    """最小化的 requests.Response 替身。"""

    def __init__(self, payload=None, *, status_code: int = 200, text: str | None = None,
                 raise_on_json: bool = False):
        self.payload = payload
        self.status_code = status_code
        if text is not None:
            self.text = text
        elif payload is not None:
            self.text = json.dumps(payload, ensure_ascii=False)
        else:
            self.text = ""
        self.headers = {}
        self._raise_on_json = raise_on_json

    def json(self):
        if self._raise_on_json or self.payload is None:
            raise ValueError("response is not JSON")
        return self.payload


class FakeLimiter:
    """记录 wait() 调用次数，方便断言"每次请求前都限速"。"""

    def __init__(self):
        self.calls = 0
        self.success = 0
        self.risks: list[int] = []

    def wait(self):
        self.calls += 1

    def on_success(self):
        self.success += 1

    def on_risk(self, code):
        self.risks.append(code)
        return False


def envelope(data=None, code: int = 0, message: str = "0") -> FakeResponse:
    """构造 B 站标准 envelope。"""
    return FakeResponse({"code": code, "message": message, "ttl": 1, "data": data})


def nav_ok(record=None) -> FakeResponse:
    return envelope(dict(NAV_DATA))


def nav_unlogged(record=None) -> FakeResponse:
    return envelope({}, code=-101, message="账号未登录")


def install_fake_http(monkeypatch, handler) -> list[dict]:
    """把 requests.Session.request 换成 handler，返回调用记录列表。"""
    calls: list[dict] = []

    def fake_request(session_self, method, url, **kwargs):
        record = {"method": method.upper(), "url": url, **kwargs}
        calls.append(record)
        result = handler(record)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(requests.Session, "request", fake_request)
    return calls


def make_router(routes: dict) -> callable:
    """按 URL path 精确匹配返回假响应；value 可以是响应或函数。"""

    def handler(record):
        path = urlsplit(record["url"]).path
        for key, result in routes.items():
            if key == path:
                return result(record) if callable(result) else result
        raise AssertionError(f"未预期的请求：{record['method']} {record['url']}")

    return handler


@pytest.fixture
def limiter() -> FakeLimiter:
    return FakeLimiter()


@pytest.fixture
def client(limiter) -> BiliClient:
    instance = BiliClient(limiter=limiter, timeout=1)
    instance.session.cookies.set("bili_jct", "csrf-token")
    return instance


# --------------------------------------------------------------------------- wbi


def test_get_mixin_key_matches_official_example():
    assert wbi.get_mixin_key(IMG_KEY, SUB_KEY) == MIXIN_KEY


def test_get_mixin_key_rejects_short_keys():
    with pytest.raises(ValueError):
        wbi.get_mixin_key("too-short", "")


def test_sign_params_matches_official_example():
    """固定 wts 后签名必须稳定：和官方文档给出的 w_rid 完全一致。"""
    signed = wbi.sign_params(WBI_DEMO_PARAMS, MIXIN_KEY, wts=WBI_DEMO_WTS)
    assert signed["wts"] == WBI_DEMO_WTS
    assert signed["w_rid"] == WBI_DEMO_RID
    assert all(isinstance(value, str) for key, value in signed.items() if key != "wts")


def test_sign_params_does_not_mutate_input_and_filters_special_chars():
    params = {"a": "x!'()*y", "empty": "", "num": 1}
    signed = wbi.sign_params(params, MIXIN_KEY, wts=WBI_DEMO_WTS)
    assert params == {"a": "x!'()*y", "empty": "", "num": 1}  # 入参不被修改
    assert signed["a"] == "xy"  # 特殊字符被剔除
    assert signed["w_rid"] == WBI_FILTERED_RID


def test_sign_params_default_wts_is_second_timestamp():
    signed = wbi.sign_params({}, MIXIN_KEY)
    assert isinstance(signed["wts"], int)
    assert len(signed["w_rid"]) == 32
    int(signed["w_rid"], 16)  # 是合法 md5 hex


def test_extract_wbi_keys():
    assert wbi.extract_wbi_keys(dict(NAV_DATA)) == (IMG_KEY, SUB_KEY)
    assert wbi.extract_wbi_keys({}) == ("", "")
    assert wbi.extract_wbi_keys({"wbi_img": None}) == ("", "")


# --------------------------------------------------------------------------- client


def test_client_default_headers_are_desktop(client):
    assert "Windows NT" in client.session.headers["User-Agent"]
    assert "Chrome" in client.session.headers["User-Agent"]


def test_get_returns_envelope_and_calls_limiter(client, limiter, monkeypatch):
    install_fake_http(monkeypatch, make_router({"/x/test": envelope({"hello": 1})}))
    payload = client.get("/x/test", {"a": 1})
    assert payload["code"] == 0
    assert payload["data"] == {"hello": 1}
    assert limiter.calls == 1


def test_absolute_url_is_used_as_is(client, monkeypatch):
    calls = install_fake_http(
        monkeypatch,
        make_router({"/dynamic_svr/v1/dynamic_svr/rm_dynamic": envelope({})}),
    )
    client.get(VC_BASE + "/dynamic_svr/v1/dynamic_svr/rm_dynamic")
    assert calls[0]["url"] == VC_BASE + "/dynamic_svr/v1/dynamic_svr/rm_dynamic"


def test_post_auto_injects_csrf(client, monkeypatch):
    calls = install_fake_http(monkeypatch, make_router({"/x/modify": envelope({})}))
    client.post("/x/modify", {"fid": 1})
    assert calls[0]["method"] == "POST"
    assert calls[0]["data"] == {"fid": 1, "csrf": "csrf-token"}


def test_post_keeps_explicit_csrf(client, monkeypatch):
    calls = install_fake_http(monkeypatch, make_router({"/x/modify": envelope({})}))
    client.post("/x/modify", {"csrf": "explicit-csrf"})
    assert calls[0]["data"]["csrf"] == "explicit-csrf"


@pytest.mark.parametrize("code", sorted(RISK_CODES))
def test_risk_codes_map_to_risk_control(client, monkeypatch, code):
    install_fake_http(
        monkeypatch, make_router({"/x/test": envelope(None, code=code, message="风控")})
    )
    with pytest.raises(RiskControlError) as excinfo:
        client.get("/x/test")
    assert excinfo.value.bili_code == code
    assert excinfo.value.code == "RISK_CONTROL"
    assert excinfo.value.path == "/x/test"


@pytest.mark.parametrize("code", sorted(AUTH_CODES))
def test_auth_codes_map_to_auth_required(client, monkeypatch, code):
    install_fake_http(
        monkeypatch, make_router({"/x/test": envelope(None, code=code, message="未登录")})
    )
    with pytest.raises(AuthRequiredError) as excinfo:
        client.get("/x/test")
    assert excinfo.value.code == "NOT_LOGGED_IN"
    assert excinfo.value.extra.get("bili_code") == code


@pytest.mark.parametrize("code", [12345, -400, -500])
def test_other_codes_map_to_bili_api_error(client, monkeypatch, code):
    install_fake_http(
        monkeypatch, make_router({"/x/test": envelope(None, code=code, message="坏了")})
    )
    with pytest.raises(BiliApiError) as excinfo:
        client.get("/x/test")
    assert excinfo.value.bili_code == code
    assert excinfo.value.path == "/x/test"


def test_get_does_not_retry_on_risk_control(client, monkeypatch):
    calls = install_fake_http(
        monkeypatch, make_router({"/x/test": envelope(None, code=-352)})
    )
    with pytest.raises(RiskControlError):
        client.get("/x/test")
    assert len(calls) == 1  # 客户端不自行重试，冷却交给 services 层


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (412, RiskControlError),
        (429, RiskControlError),
        (401, AuthRequiredError),
        (403, AuthRequiredError),
    ],
)
def test_http_status_mapping(client, monkeypatch, status, expected):
    install_fake_http(
        monkeypatch, make_router({"/x/test": FakeResponse(None, status_code=status)})
    )
    with pytest.raises(expected):
        client.get("/x/test")


def test_non_json_response_raises_bili_api_error(client, monkeypatch):
    install_fake_http(
        monkeypatch,
        make_router({"/x/test": FakeResponse(None, text="<html>412</html>", raise_on_json=True)}),
    )
    with pytest.raises(BiliApiError) as excinfo:
        client.get("/x/test")
    assert excinfo.value.bili_code == -999


def test_network_error_maps_to_bili_api_error(client, monkeypatch):
    def handler(record):
        return requests.ConnectionError("boom")

    install_fake_http(monkeypatch, handler)
    with pytest.raises(BiliApiError) as excinfo:
        client.get("/x/test")
    assert excinfo.value.bili_code == -999


def test_response_without_code_raises(client, monkeypatch):
    install_fake_http(monkeypatch, make_router({"/x/test": FakeResponse({"data": {}})}))
    with pytest.raises(BiliApiError):
        client.get("/x/test")


# --------------------------------------------------------------------------- nav / wbi 集成


def test_nav_parses_user_and_refreshes_keys(client, monkeypatch):
    calls = install_fake_http(monkeypatch, make_router({NAV_PATH: nav_ok}))
    data = client.nav()
    assert data["mid"] == USER_MID
    assert client.user == {
        "mid": USER_MID,
        "name": "不会数学的sky",
        "face": "https://i0.hdslb.com/bfs/face/abc.jpg",
        "vip": True,
    }
    assert client.user_mid == USER_MID
    assert client._img_key == IMG_KEY
    assert client._sub_key == SUB_KEY
    assert len(calls) == 1


def test_nav_unlogged_returns_data_without_raising(client, monkeypatch):
    install_fake_http(monkeypatch, make_router({NAV_PATH: nav_unlogged}))
    data = client.nav()
    assert data == {}
    assert client.user is None
    assert client._img_key == ""


def test_wbi_auto_fetches_nav_and_signs(client, monkeypatch):
    calls = install_fake_http(
        monkeypatch,
        make_router(
            {
                NAV_PATH: nav_ok,
                FOLLOWINGS_PATH: envelope({"total": 0, "list": []}),
            }
        ),
    )
    client.get(FOLLOWINGS_PATH, {"vmid": USER_MID, "pn": 1}, wbi=True)
    assert len(calls) == 2
    assert calls[0]["url"].endswith(NAV_PATH)
    signed = calls[1]["params"]
    assert signed["vmid"] == str(USER_MID)  # 签名后所有参数值都是字符串
    assert isinstance(signed["wts"], int)
    # 用同一 wts 重算，w_rid 必须一致
    unsigned = {k: v for k, v in signed.items() if k not in ("wts", "w_rid")}
    recomputed = wbi.sign_params(unsigned, MIXIN_KEY, wts=signed["wts"])
    assert recomputed["w_rid"] == signed["w_rid"]


def test_wbi_keys_are_cached_between_requests(client, monkeypatch):
    calls = install_fake_http(
        monkeypatch,
        make_router(
            {
                NAV_PATH: nav_ok,
                FOLLOWINGS_PATH: envelope({"total": 0, "list": []}),
            }
        ),
    )
    client.get(FOLLOWINGS_PATH, {"vmid": 1}, wbi=True)
    client.get(FOLLOWINGS_PATH, {"vmid": 1}, wbi=True)
    nav_calls = [call for call in calls if call["url"].endswith(NAV_PATH)]
    assert len(nav_calls) == 1
    assert len(calls) == 3


def test_wbi_without_keys_raises_auth_required(client, monkeypatch):
    install_fake_http(monkeypatch, make_router({NAV_PATH: nav_unlogged}))
    with pytest.raises(AuthRequiredError):
        client.get(FOLLOWINGS_PATH, {"vmid": 1}, wbi=True)


def test_dynamic_feed_referer_is_space_page(client, monkeypatch):
    calls = install_fake_http(
        monkeypatch,
        make_router({FEED_PATH: envelope({"items": [], "has_more": False, "offset": ""})}),
    )
    client.get(FEED_PATH, {"host_mid": USER_MID, "offset": ""})
    headers = calls[0]["headers"]
    assert headers["Referer"] == f"https://space.bilibili.com/{USER_MID}/dynamic"
    assert headers["Origin"] == "https://space.bilibili.com"


def test_loads_cookies_from_store(tmp_path):
    store = Store(str(tmp_path))
    store.save_session({"SESSDATA": "sess", "bili_jct": "jct", "DedeUserID": str(USER_MID)})
    instance = BiliClient(store=store)
    assert instance.cookies["SESSDATA"] == "sess"
    assert instance.csrf == "jct"
    assert instance.user_mid == USER_MID
    assert instance.is_logged_in is True


def test_close_closes_session(client):
    closed = []
    client.session.close = lambda: closed.append(True)
    client.close()
    assert closed == [True]


# --------------------------------------------------------------------------- auth


def test_qrcode_generate_returns_contract_and_png(monkeypatch):
    login_url = (
        "https://passport.bilibili.com/h5-app/passport/login/scan"
        "?sid=abc&gourl=https%3A%2F%2Fwww.bilibili.com"
    )
    calls = install_fake_http(
        monkeypatch,
        make_router(
            {
                QR_GENERATE_PATH: envelope({"url": login_url, "qrcode_key": "k" * 32}),
            }
        ),
    )
    result = auth.qrcode_generate(BiliClient())
    assert set(result) == QRCODE_KEYS
    assert result["url"] == login_url
    assert result["key"] == "k" * 32
    assert result["expires_in"] == 180
    assert result["qr_png"].startswith("data:image/png;base64,")
    png = base64.b64decode(result["qr_png"].split(",", 1)[1])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert calls[0]["url"].startswith(PASSPORT_BASE)
    assert calls[0]["params"]["source"] == "main-fe-header"


def test_qrcode_generate_missing_fields_raises(monkeypatch):
    install_fake_http(
        monkeypatch,
        make_router({QR_GENERATE_PATH: envelope({"url": "", "qrcode_key": ""})}),
    )
    with pytest.raises(BiliApiError):
        auth.qrcode_generate(BiliClient())


@pytest.mark.parametrize(
    ("data_code", "expected"),
    [(86101, "waiting"), (86090, "scanned"), (86038, "expired")],
)
def test_qrcode_poll_status_mapping(monkeypatch, data_code, expected):
    install_fake_http(
        monkeypatch,
        make_router({QR_POLL_PATH: envelope({"code": data_code, "url": ""})}),
    )
    result = auth.qrcode_poll(BiliClient(), "key")
    assert set(result) == QRPOLL_KEYS
    assert result == {"status": expected, "user": None}


def test_qrcode_poll_confirmed_fetches_user(monkeypatch):
    calls = install_fake_http(
        monkeypatch,
        make_router(
            {
                QR_POLL_PATH: envelope({"code": 0, "url": "https://passport.bilibili.com/x"}),
                NAV_PATH: nav_ok,
            }
        ),
    )
    result = auth.qrcode_poll(BiliClient(), "key")
    assert result["status"] == "confirmed"
    assert set(result["user"]) == USER_KEYS
    assert result["user"]["mid"] == USER_MID
    assert result["user"]["name"] == "不会数学的sky"
    assert result["user"]["vip"] is True
    assert len(calls) == 2  # poll + nav


def test_qrcode_poll_invalid_key_maps_expired(monkeypatch):
    install_fake_http(
        monkeypatch,
        make_router({QR_POLL_PATH: envelope(None, code=-400, message="请求错误")}),
    )
    assert auth.qrcode_poll(BiliClient(), "bad-key") == {"status": "expired", "user": None}


def test_fetch_me_returns_user_and_caches(client, monkeypatch):
    calls = install_fake_http(monkeypatch, make_router({NAV_PATH: nav_ok}))
    user = auth.fetch_me(client)
    assert set(user) == USER_KEYS
    assert user["mid"] == USER_MID
    assert client.user == user
    # 第二次直接用缓存，不再请求
    assert auth.fetch_me(client) == user
    assert len(calls) == 1


def test_fetch_me_raises_when_not_logged_in(client, monkeypatch):
    install_fake_http(monkeypatch, make_router({NAV_PATH: nav_unlogged}))
    with pytest.raises(AuthRequiredError):
        auth.fetch_me(client)


def test_fetch_me_raises_when_mid_missing(client, monkeypatch):
    install_fake_http(
        monkeypatch,
        make_router({NAV_PATH: envelope({"isLogin": True, "uname": "x"})}),
    )
    with pytest.raises(AuthRequiredError):
        auth.fetch_me(client, refresh=True)


# --------------------------------------------------------------------------- follows


def _follow_raw(mid: int, name: str = "UP", attribute: int = 2, tag=None,
                official: str = "bilibili 知名UP主", mtime: int = 1700000000) -> dict:
    return {
        "mid": mid,
        "uname": name,
        "attribute": attribute,
        "tag": tag or [],
        "sign": f"签名-{mid}",
        "face": f"https://i0.hdslb.com/bfs/face/{mid}.jpg",
        "mtime": mtime,
        "official_verify": {"type": 0, "desc": official},
    }


def test_iter_followings_pages_and_parses(monkeypatch, limiter):
    tags = [
        {"tagid": 74311568, "name": "学习", "count": 2},
        {"tagid": 0, "name": "默认分组", "count": 51},
    ]
    page1 = {
        "total": 53,
        "list": [
            _follow_raw(
                100 + index,
                f"UP{index}",
                attribute=6 if index == 0 else 2,
                tag=[74311568],
            )
            for index in range(50)
        ],
    }
    page2 = {"total": 53, "list": [_follow_raw(200 + index) for index in range(3)]}
    pages = {1: page1, 2: page2}
    calls = install_fake_http(
        monkeypatch,
        make_router(
            {
                NAV_PATH: nav_ok,
                TAGS_PATH: envelope(tags),
                FOLLOWINGS_PATH: lambda record: envelope(
                    pages[int(record["params"]["pn"])]
                ),
            }
        ),
    )

    instance = BiliClient(limiter=limiter)
    items = list(follows.iter_followings(instance))
    assert len(items) == 53
    assert [item["order"] for item in items] == list(range(1, 54))
    assert all(set(item) == FOLLOW_KEYS for item in items)

    first = items[0]
    assert first["mid"] == 100
    assert first["name"] == "UP0"
    assert first["attribute"] == 6
    assert first["mutual"] is True
    assert first["groups"] == ["学习"]
    assert first["group_ids"] == [74311568]
    assert first["official"] == "bilibili 知名UP主"
    assert first["mtime"] == 1700000000
    assert first["category"] is None
    assert first["category_source"] is None
    assert items[1]["mutual"] is False

    follow_calls = [call for call in calls if call["url"].endswith(FOLLOWINGS_PATH)]
    assert len(follow_calls) == 2
    assert follow_calls[0]["params"]["pn"] == "1"  # wbi 签名后为字符串
    assert follow_calls[0]["params"]["ps"] == "50"
    assert follow_calls[0]["params"]["vmid"] == str(USER_MID)
    assert "w_rid" in follow_calls[0]["params"]
    assert follow_calls[1]["params"]["pn"] == "2"
    assert limiter.calls == len(calls)  # 每个请求前都限速


def test_iter_followings_respects_max_pages(monkeypatch):
    def page_for(record):
        page_number = int(record["params"]["pn"])
        return envelope(
            {
                "total": 999,
                "list": [
                    _follow_raw(page_number * 1000 + index) for index in range(50)
                ],
            }
        )

    calls = install_fake_http(
        monkeypatch,
        make_router(
            {
                NAV_PATH: nav_ok,
                TAGS_PATH: envelope([]),
                FOLLOWINGS_PATH: page_for,
            }
        ),
    )
    items = list(follows.iter_followings(BiliClient(), max_pages=2))
    assert len(items) == 100
    follow_calls = [call for call in calls if call["url"].endswith(FOLLOWINGS_PATH)]
    assert len(follow_calls) == 2


def test_iter_followings_tolerates_group_failure(monkeypatch):
    install_fake_http(
        monkeypatch,
        make_router(
            {
                NAV_PATH: nav_ok,
                TAGS_PATH: envelope(None, code=-500, message="分组挂了"),
                FOLLOWINGS_PATH: envelope(
                    {"total": 1, "list": [_follow_raw(7, tag=[999])]}
                ),
            }
        ),
    )
    items = list(follows.iter_followings(BiliClient()))
    assert len(items) == 1
    assert items[0]["groups"] == ["999"]  # 分组名缺失时退回 tagid 字符串


def test_list_groups(monkeypatch):
    tags = [
        {"tagid": -10, "name": "特别关注", "count": 4, "tip": ""},
        {"tagid": 74311568, "name": "学习", "count": 24},
    ]
    install_fake_http(monkeypatch, make_router({TAGS_PATH: envelope(tags)}))
    groups = follows.list_groups(BiliClient())
    assert groups == [
        {"tagid": -10, "name": "特别关注", "count": 4},
        {"tagid": 74311568, "name": "学习", "count": 24},
    ]
    assert all(set(group) == GROUP_KEYS for group in groups)


def test_unfollow_posts_act_2(client, monkeypatch):
    calls = install_fake_http(monkeypatch, make_router({MODIFY_PATH: envelope({})}))
    follows.unfollow(client, 123456)
    assert len(calls) == 1
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith(MODIFY_PATH)
    assert call["data"] == {
        "fid": 123456,
        "act": 2,
        "re_src": 11,
        "csrf": "csrf-token",
    }


def test_unfollow_rejects_invalid_mid(client):
    with pytest.raises(ValueError):
        follows.unfollow(client, 0)


def test_get_relation(monkeypatch):
    calls = install_fake_http(
        monkeypatch,
        make_router(
            {
                NAV_PATH: nav_ok,
                RELATION_PATH: envelope({"mid": 42, "attribute": 6}),
            }
        ),
    )
    relation = follows.get_relation(BiliClient(), 42)
    assert relation == {"mid": 42, "attribute": 6}
    assert calls[-1]["params"]["fid"] == "42"  # wbi 签名后为字符串


# --------------------------------------------------------------------------- dynamics


def _forward_raw(dyn_id: str = "1264505979038334999", orig_author: str = "神秘UP") -> dict:
    return {
        "id_str": dyn_id,
        "type": "DYNAMIC_TYPE_FORWARD",
        "modules": {
            "module_author": {"pub_ts": 1700000000, "name": "我"},
            "module_dynamic": {"desc": {"text": "转发语"}, "major": None},
        },
        "orig": {
            "modules": {
                "module_author": {"name": orig_author},
                "module_dynamic": {"desc": {"text": "原文内容"}},
            }
        },
    }


def _video_raw(dyn_id: str = "1264505979038334000") -> dict:
    return {
        "id_str": dyn_id,
        "type": "DYNAMIC_TYPE_AV",
        "modules": {
            "module_author": {"pub_ts": 1699999999},
            "module_dynamic": {
                "desc": {"text": "推荐一个视频"},
                "major": {
                    "archive": {"bvid": "BV1xx411c7mD", "title": "视频标题"},
                },
            },
        },
    }


def _word_raw(dyn_id: str = "1264505979038334001") -> dict:
    return {
        "id_str": dyn_id,
        "type": "DYNAMIC_TYPE_WORD",
        "modules": {
            "module_author": {"pub_ts": 1699999998},
            "module_dynamic": {"desc": {"text": "今天天气不错"}, "major": None},
        },
    }


def _draw_raw(dyn_id: str = "1264505979038334002") -> dict:
    return {
        "id_str": dyn_id,
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {"pub_ts": 1699999997},
            "module_dynamic": {
                "desc": {"text": "几张图"},
                "major": {
                    "draw": {
                        "items": [
                            {"src": "https://i0.hdslb.com/bfs/1.jpg"},
                            {"src": "https://i0.hdslb.com/bfs/2.jpg"},
                        ]
                    }
                },
            },
        },
    }


def test_iter_dynamics_cursor_pages(monkeypatch):
    page1 = {
        "offset": "cursor-2",
        "has_more": True,
        "items": [_forward_raw(), _video_raw()],
    }
    page2 = {
        "offset": "",
        "has_more": False,
        "items": [_word_raw(), _draw_raw()],
    }
    pages = {"": page1, "cursor-2": page2}
    calls = install_fake_http(
        monkeypatch,
        make_router({FEED_PATH: lambda record: envelope(pages[record["params"]["offset"]])}),
    )
    items = list(dynamics.iter_dynamics(BiliClient(), USER_MID))
    assert [item["id"] for item in items] == [
        "1264505979038334999",
        "1264505979038334000",
        "1264505979038334001",
        "1264505979038334002",
    ]
    assert all(set(item) == DYNAMIC_KEYS for item in items)

    forward, video, word, draw = items
    assert forward["type"] == "DYNAMIC_TYPE_FORWARD"
    assert forward["type_label"] == "转发"
    assert forward["text"] == "转发语"
    assert forward["orig_author"] == "神秘UP"
    assert forward["video"] is None
    assert forward["pub_ts"] == 1700000000

    assert video["type_label"] == "视频"
    assert video["video"] == {"bvid": "BV1xx411c7mD", "title": "视频标题"}

    assert word["has_image"] is False
    assert word["type_label"] == "文字"

    assert draw["has_image"] is True
    assert draw["video"] is None

    feed_calls = [call for call in calls if call["url"].endswith(FEED_PATH)]
    assert len(feed_calls) == 2
    assert feed_calls[0]["params"]["offset"] == ""
    assert feed_calls[1]["params"]["offset"] == "cursor-2"
    assert feed_calls[0]["params"]["host_mid"] == USER_MID
    assert feed_calls[0]["params"]["timezone_offset"] == -480
    assert feed_calls[0]["headers"]["Referer"].endswith(f"/{USER_MID}/dynamic")


def test_iter_dynamics_stops_on_repeated_offset(monkeypatch):
    response = {"offset": "same", "has_more": True, "items": [_word_raw()]}
    calls = install_fake_http(
        monkeypatch, make_router({FEED_PATH: envelope(response)})
    )
    items = list(dynamics.iter_dynamics(BiliClient(), USER_MID))
    assert len(items) == 1
    # 第一页 offset=""，第二页仍是 "same" -> 游标没前进，停止
    assert len(calls) == 2
    assert calls[1]["params"]["offset"] == "same"


def test_iter_dynamics_stops_on_empty_items(monkeypatch):
    calls = install_fake_http(
        monkeypatch,
        make_router({FEED_PATH: envelope({"offset": "x", "has_more": True, "items": []})}),
    )
    assert list(dynamics.iter_dynamics(BiliClient(), USER_MID)) == []
    assert len(calls) == 1


def test_parse_dynamic_tolerates_missing_fields():
    item = dynamics.parse_dynamic({})
    assert set(item) == DYNAMIC_KEYS
    assert item == {
        "id": "",
        "type": "",
        "type_label": "其他",
        "pub_ts": 0,
        "text": "",
        "has_image": False,
        "video": None,
        "orig_author": None,
    }


def test_delete_dynamic(client, monkeypatch):
    calls = install_fake_http(monkeypatch, make_router({RM_DYNAMIC_PATH: envelope({})}))
    assert dynamics.delete_dynamic(client, "1264505979038334999") is None
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == VC_BASE + RM_DYNAMIC_PATH
    assert call["data"] == {
        "dynamic_id": "1264505979038334999",
        "csrf_token": "csrf-token",
        "csrf": "csrf-token",
    }
    assert call["headers"]["Referer"].startswith("https://t.bilibili.com/")


def test_delete_dynamic_already_deleted_is_ok(client, monkeypatch):
    install_fake_http(
        monkeypatch,
        make_router({RM_DYNAMIC_PATH: envelope(None, code=500404, message="动态不存在")}),
    )
    assert dynamics.delete_dynamic(client, "123") is None


def test_delete_dynamic_risk_control_propagates(client, monkeypatch):
    install_fake_http(monkeypatch, make_router({RM_DYNAMIC_PATH: envelope(None, code=-352)}))
    with pytest.raises(RiskControlError):
        dynamics.delete_dynamic(client, "123")


def test_delete_dynamic_rejects_empty_id(client):
    with pytest.raises(BiliApiError):
        dynamics.delete_dynamic(client, "")


# --------------------------------------------------------------------------- favorites


def _fav_folder(media_id: int, title: str, count: int = 10, attr: int = 0) -> dict:
    return {"id": media_id, "fid": media_id, "title": title, "media_count": count, "attr": attr}


def _fav_raw(item_id: int, title: str = "收藏的视频", upper: str = "UP主",
             bvid: str = "BV1xx411c7mD", attr: int = 0,
             fav_time: int = 1700000000) -> dict:
    return {
        "id": item_id,
        "type": 2,
        "title": title,
        "upper": {"name": upper},
        "bvid": bvid,
        "cover": "https://i0.hdslb.com/bfs/cover.jpg",
        "fav_time": fav_time,
        "attr": attr,
    }


def test_list_folders_parses_default_and_privacy(monkeypatch):
    instance = BiliClient()
    instance.session.cookies.set("DedeUserID", str(USER_MID))
    data = {
        "count": 2,
        "list": [
            _fav_folder(916673043, "默认收藏夹", 4565, 0),
            _fav_folder(12345678, "考研资料", 24, 1),
        ],
    }
    calls = install_fake_http(
        monkeypatch, make_router({FOLDERS_PATH: envelope(data)})
    )
    folders = favorites.list_folders(instance)
    assert folders == [
        {
            "media_id": 916673043,
            "title": "默认收藏夹",
            "count": 4565,
            "is_default": True,
            "privacy": 0,
        },
        {
            "media_id": 12345678,
            "title": "考研资料",
            "count": 24,
            "is_default": False,
            "privacy": 1,
        },
    ]
    assert all(set(folder) == FAV_FOLDER_KEYS for folder in folders)
    assert calls[0]["params"]["up_mid"] == USER_MID


def test_iter_items_pages_with_ps_20(monkeypatch):
    page1 = {
        "info": {"media_count": 25},
        "medias": [_fav_raw(1000 + index) for index in range(20)],
        "has_more": True,
    }
    page2 = {
        "info": {"media_count": 25},
        "medias": [_fav_raw(2000 + index) for index in range(5)],
        "has_more": False,
    }
    pages = {1: page1, 2: page2}
    calls = install_fake_http(
        monkeypatch,
        make_router(
            {RESOURCE_LIST_PATH: lambda record: envelope(pages[record["params"]["pn"]])}
        ),
    )
    items = list(favorites.iter_items(BiliClient(), 916673043))
    assert len(items) == 25
    assert all(set(item) == FAV_ITEM_KEYS for item in items)
    first = items[0]
    assert first["id"] == "1000"
    assert first["type"] == 2
    assert first["title"] == "收藏的视频"
    assert first["upper"] == "UP主"
    assert first["bvid"] == "BV1xx411c7mD"
    assert first["cover"].startswith("https://")
    assert first["fav_time"] == 1700000000
    assert first["invalid"] is False

    resource_calls = [call for call in calls if call["url"].endswith(RESOURCE_LIST_PATH)]
    assert len(resource_calls) == 2
    assert resource_calls[0]["params"]["ps"] == 20
    assert resource_calls[0]["params"]["media_id"] == 916673043
    assert resource_calls[1]["params"]["pn"] == 2


def test_iter_items_marks_invalid(tmp_path, monkeypatch):
    page = {
        "info": {"media_count": 3},
        "medias": [
            _fav_raw(1, title="正常", attr=0),
            _fav_raw(2, title="", bvid="", attr=0),
            _fav_raw(3, title="已失效", attr=1),
        ],
        "has_more": False,
    }
    install_fake_http(
        monkeypatch, make_router({RESOURCE_LIST_PATH: envelope(page)})
    )
    items = list(favorites.iter_items(BiliClient(), 1))
    assert [item["invalid"] for item in items] == [False, True, True]


def test_iter_items_respects_max_pages(monkeypatch):
    page = {
        "info": {"media_count": 999},
        "medias": [_fav_raw(1) for _ in range(20)],
        "has_more": True,
    }
    calls = install_fake_http(
        monkeypatch, make_router({RESOURCE_LIST_PATH: envelope(page)})
    )
    items = list(favorites.iter_items(BiliClient(), 1, max_pages=2))
    assert len(items) == 40
    assert len(calls) == 2


def test_batch_delete_joins_resources(client, monkeypatch):
    calls = install_fake_http(
        monkeypatch, make_router({BATCH_DEL_PATH: envelope({"success": 2})})
    )
    result = favorites.batch_delete(client, 916673043, ["117528844047788:2", "816619:24"])
    assert result == {"success": 2}
    call = calls[0]
    assert call["method"] == "POST"
    assert call["data"]["resources"] == "117528844047788:2,816619:24"
    assert call["data"]["media_id"] == 916673043
    assert call["data"]["platform"] == "web"
    assert call["data"]["csrf"] == "csrf-token"


def test_batch_delete_empty_is_noop(client, monkeypatch):
    calls = install_fake_http(monkeypatch, make_router({BATCH_DEL_PATH: envelope({})}))
    assert favorites.batch_delete(client, 1, []) == {}
    assert calls == []


def test_batch_delete_rejects_more_than_20(client):
    resources = [f"{index}:2" for index in range(21)]
    with pytest.raises(ValueError):
        favorites.batch_delete(client, 1, resources)


def test_clean_invalid(client, monkeypatch):
    calls = install_fake_http(monkeypatch, make_router({CLEAN_PATH: envelope({})}))
    assert favorites.clean_invalid(client, 916673043) is None
    assert calls[0]["method"] == "POST"
    assert calls[0]["data"]["media_id"] == 916673043
    assert calls[0]["data"]["csrf"] == "csrf-token"


# --------------------------------------------------------------------------- toview


def test_list_toview_parses_items(monkeypatch):
    data = {
        "count": 2,
        "list": [
            {
                "aid": 123,
                "title": "稍后再看视频",
                "owner": {"name": "某个UP"},
                "duration": 600,
                "progress": 0,
                "add_at": 1700000000,
                "bvid": "BV1xx411c7mD",
            },
            {"aid": 456, "title": "缺字段的视频", "duration": 30},
        ],
    }
    calls = install_fake_http(monkeypatch, make_router({TOVIEW_PATH: envelope(data)}))
    items = toview.list_toview(BiliClient())
    assert len(items) == 2
    assert all(set(item) == TOVIEW_KEYS for item in items)
    assert items[0] == {
        "aid": 123,
        "title": "稍后再看视频",
        "upper": "某个UP",
        "duration": 600,
        "progress": 0,
        "add_ts": 1700000000,
        "bvid": "BV1xx411c7mD",
    }
    assert items[1]["upper"] == ""
    assert items[1]["add_ts"] == 0
    assert "params" not in calls[0]


def test_clear_toview(client, monkeypatch):
    calls = install_fake_http(monkeypatch, make_router({TOVIEW_CLEAR_PATH: envelope({})}))
    assert toview.clear_toview(client) is None
    call = calls[0]
    assert call["method"] == "POST"
    assert call["data"] == {"csrf": "csrf-token"}


# --------------------------------------------------------------------------- 公共工具


def test_unwrap_handles_envelope_and_data():
    assert unwrap({"code": 0, "data": {"x": 1}}) == {"x": 1}
    assert unwrap({"total": 1, "list": []}) == {"total": 1, "list": []}
    assert unwrap(None) is None


def test_to_int_is_tolerant():
    assert to_int("42") == 42
    assert to_int(42) == 42
    assert to_int(None) == 0
    assert to_int("abc", -1) == -1
    assert to_int(True) == 1


def test_csrf_of_tolerates_missing_attribute():
    assert csrf_of(object()) == ""
    assert csrf_of(type("C", (), {"csrf": "abc"})()) == "abc"


def test_resolve_mid_uses_cookie_or_nav(monkeypatch):
    instance = BiliClient()
    install_fake_http(monkeypatch, make_router({NAV_PATH: nav_ok}))
    assert resolve_mid(instance) == USER_MID

    anonymous = BiliClient()
    install_fake_http(monkeypatch, make_router({NAV_PATH: nav_unlogged}))
    with pytest.raises(AuthRequiredError):
        resolve_mid(anonymous)


def test_contract_module_surface():
    """契约里的函数/类都必须存在且可调用。"""
    assert callable(auth.qrcode_generate)
    assert callable(auth.qrcode_poll)
    assert callable(auth.fetch_me)
    assert callable(follows.iter_followings)
    assert callable(follows.list_groups)
    assert callable(follows.unfollow)
    assert callable(follows.get_relation)
    assert callable(dynamics.iter_dynamics)
    assert callable(dynamics.delete_dynamic)
    assert callable(favorites.list_folders)
    assert callable(favorites.iter_items)
    assert callable(favorites.batch_delete)
    assert callable(favorites.clean_invalid)
    assert callable(toview.list_toview)
    assert callable(toview.clear_toview)

    client = BiliClient()
    for method in ("get", "post", "nav", "close"):
        assert callable(getattr(client, method))
    assert isinstance(client.cookies, dict)
    assert isinstance(client.csrf, str)
