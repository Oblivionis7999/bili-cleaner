"""Mock 包的测试：假数据规模/字段完整性、分页、写操作、登录态、风控注入。"""

from __future__ import annotations

import itertools
import time

import pytest

from bilicleaner.const import CATEGORIES, DYNAMIC_TYPE_LABELS
from bilicleaner.errors import AuthRequiredError, BiliApiError, RiskControlError
from bilicleaner.mock import (
    DEFAULT_FAV_COUNT,
    DYNAMIC_COUNT,
    FOLLOW_COUNT,
    GROUP_NAMES,
    MUTUAL_COUNT,
    SELF_FAV_COUNTS,
    TOVIEW_COUNT,
    MockBiliClient,
    make_mock_data,
)

FAV_DYNAMICS_PATH = "/x/polymer/web-dynamic/v1/feed/space"
FOLLOWINGS_PATH = "/x/relation/followings"
FAV_RESOURCE_PATH = "/x/v3/fav/resource/list"


@pytest.fixture(scope="module")
def data() -> dict:
    return make_mock_data(42)


@pytest.fixture()
def client() -> MockBiliClient:
    return MockBiliClient()


# ================================================================ 假数据规模 / 字段


def test_mock_data_scale(data: dict):
    assert data["seed"] == 42
    assert len(data["follows"]) == FOLLOW_COUNT == 2000
    assert sum(1 for f in data["follows"] if f["mutual"]) == MUTUAL_COUNT == 40
    assert len(data["groups"]) == len(GROUP_NAMES) == 6
    assert len(data["dynamics"]) == DYNAMIC_COUNT == 300
    assert len(data["toview"]) == TOVIEW_COUNT == 120
    assert len(data["folders"]) == 3
    assert data["counts"] == {
        "follows": 2000,
        "mutuals": 40,
        "groups": 6,
        "categories": 21,
        "dynamics": 300,
        "folders": 3,
        "favorites": DEFAULT_FAV_COUNT + sum(SELF_FAV_COUNTS),
        "toview": 120,
    }


def test_mock_data_deterministic():
    assert make_mock_data(42) == make_mock_data(42)
    assert make_mock_data(7) != make_mock_data(42)


def test_follow_item_fields(data: dict):
    expected = {
        "mid", "name", "sign", "face", "official", "attribute", "mutual",
        "groups", "group_ids", "order", "mtime", "category", "category_source",
    }
    mids = set()
    for item in data["follows"]:
        assert set(item) == expected
        assert isinstance(item["mid"], int) and item["mid"] > 0
        assert isinstance(item["name"], str) and item["name"]
        assert isinstance(item["sign"], str)
        assert item["face"].startswith("https://")
        assert isinstance(item["official"], str)
        assert item["attribute"] in (2, 6)
        assert item["mutual"] is (item["attribute"] == 6)
        assert len(item["groups"]) == len(item["group_ids"])
        assert all(name in GROUP_NAMES for name in item["groups"])
        assert isinstance(item["order"], int) and item["order"] >= 1
        assert isinstance(item["mtime"], int) and 1_500_000_000 < item["mtime"] < 1_800_000_000
        if item["category"] is None:
            assert item["category_source"] is None
        else:
            assert item["category"] in CATEGORIES
            assert item["category_source"] in ("rule", "llm", "user")
        mids.add(item["mid"])
    assert len(mids) == FOLLOW_COUNT

    # 21 类分类全部覆盖
    assert {f["category"] for f in data["follows"] if f["category"]} == set(CATEGORIES)


def test_group_counts_are_consistent(data: dict):
    expected: dict[int, int] = {}
    for item in data["follows"]:
        for gid in item["group_ids"]:
            expected[int(gid)] = expected.get(int(gid), 0) + 1
    for group in data["groups"]:
        assert set(group) == {"tagid", "name", "count"}
        assert group["count"] == expected.get(int(group["tagid"]), 0)
    assert sum(group["count"] for group in data["groups"]) > 0


def test_dynamic_item_fields(data: dict):
    expected = {"id", "type", "type_label", "pub_ts", "text", "has_image", "video", "orig_author"}
    type_counts: dict[str, int] = {}
    ids = set()
    for item in data["dynamics"]:
        assert set(item) == expected
        assert item["id"].isdigit() and len(item["id"]) == 19
        assert item["type"] in DYNAMIC_TYPE_LABELS
        assert item["type_label"] == DYNAMIC_TYPE_LABELS[item["type"]]
        assert isinstance(item["pub_ts"], int)
        assert isinstance(item["text"], str) and item["text"]
        assert isinstance(item["has_image"], bool)
        if item["video"] is not None:
            assert set(item["video"]) == {"bvid", "title"}
            assert item["video"]["bvid"].startswith("BV1")
        assert item["orig_author"] is None or isinstance(item["orig_author"], str)
        ids.add(item["id"])
        type_counts[item["type"]] = type_counts.get(item["type"], 0) + 1
    assert len(ids) == DYNAMIC_COUNT
    # 转发 / 图文 / 视频混合，另外带少量纯文字与专栏
    assert type_counts == {
        "DYNAMIC_TYPE_FORWARD": 120,
        "DYNAMIC_TYPE_DRAW": 99,
        "DYNAMIC_TYPE_AV": 60,
        "DYNAMIC_TYPE_WORD": 15,
        "DYNAMIC_TYPE_ARTICLE": 6,
    }
    # 时间倒序
    assert [d["pub_ts"] for d in data["dynamics"]] == sorted(
        (d["pub_ts"] for d in data["dynamics"]), reverse=True
    )
    for item in data["dynamics"]:
        if item["type"] == "DYNAMIC_TYPE_FORWARD":
            assert item["orig_author"]
        if item["type"] == "DYNAMIC_TYPE_AV":
            assert item["video"] is not None
        if item["type"] == "DYNAMIC_TYPE_DRAW":
            assert item["has_image"] is True


def test_favorites_fields(data: dict):
    folders = data["folders"]
    assert [f["media_id"] for f in folders] == [916673043, 916673044, 916673045]
    assert [f["title"] for f in folders] == ["默认收藏夹", "学习资料", "音乐现场"]
    assert folders[0]["is_default"] is True
    assert folders[0]["count"] == DEFAULT_FAV_COUNT == 500
    assert folders[1]["count"] == SELF_FAV_COUNTS[0]
    assert folders[2]["count"] == SELF_FAV_COUNTS[1]
    for folder in folders:
        assert set(folder) == {"media_id", "title", "count", "is_default", "privacy"}
        assert len(data["favorites"][folder["media_id"]]) == folder["count"]

    expected = {"id", "type", "title", "upper", "bvid", "cover", "fav_time", "invalid"}
    invalid_total = 0
    for media_id, items in data["favorites"].items():
        assert media_id in {f["media_id"] for f in folders}
        for item in items:
            assert set(item) == expected
            assert item["id"].isdigit()
            assert item["type"] in (2, 12, 21)
            assert isinstance(item["title"], str) and item["title"]
            assert item["fav_time"] > 0
            assert isinstance(item["invalid"], bool)
            invalid_total += int(item["invalid"])
    # 含少量失效条目，但不能太多
    assert 0 < invalid_total < 0.05 * (DEFAULT_FAV_COUNT + sum(SELF_FAV_COUNTS))


def test_toview_fields(data: dict):
    expected = {"aid", "title", "upper", "duration", "progress", "add_ts", "bvid"}
    aids = set()
    for item in data["toview"]:
        assert set(item) == expected
        assert isinstance(item["aid"], int) and item["aid"] > 0
        assert isinstance(item["title"], str) and item["title"]
        assert isinstance(item["upper"], str)
        assert item["duration"] > 0
        assert 0 <= item["progress"] < item["duration"]
        assert item["add_ts"] > 0
        assert item["bvid"].startswith("BV1")
        aids.add(item["aid"])
    assert len(aids) == TOVIEW_COUNT


def test_mock_data_is_deep_copied_by_client(data: dict):
    client = MockBiliClient(data=data)
    assert client.data == data
    assert client.data is not data
    client.data["follows"].clear()
    assert len(data["follows"]) == FOLLOW_COUNT


# ================================================================ 底层接口（bili.* 用）


def test_nav_returns_logged_in_data(client: MockBiliClient):
    nav = client.nav()
    assert nav["isLogin"] is True
    assert nav["mid"] == client.user_mid
    assert nav["uname"] == "在摸鱼的阿卷"
    assert nav["wbi_img"]["img_url"].startswith("https://")
    assert nav["wbi_img"]["sub_url"].startswith("https://")

    envelope = client.get("/x/web-interface/nav")
    assert envelope["code"] == 0
    assert envelope["data"]["isLogin"] is True


def test_followings_pagination(client: MockBiliClient):
    first = client.get(
        FOLLOWINGS_PATH,
        {"vmid": client.user_mid, "pn": 1, "ps": 50, "order": "desc"},
        wbi=True,
    )
    assert first["code"] == 0
    assert first["data"]["total"] == FOLLOW_COUNT
    assert len(first["data"]["list"]) == 50

    raw = first["data"]["list"][0]
    assert {"mid", "uname", "face", "sign", "attribute", "official_verify", "tag", "mtime"} <= set(raw)
    assert isinstance(raw["mid"], int)
    assert isinstance(raw["uname"], str)
    assert raw["attribute"] in (2, 6)
    assert isinstance(raw["official_verify"], dict)
    assert isinstance(raw["tag"], list)

    last = client.get(FOLLOWINGS_PATH, {"pn": 40, "ps": 50})
    assert len(last["data"]["list"]) == 50
    beyond = client.get(FOLLOWINGS_PATH, {"pn": 41, "ps": 50})
    assert beyond["data"]["list"] == []
    assert beyond["data"]["total"] == FOLLOW_COUNT


def test_relation_tags(client: MockBiliClient, data: dict):
    tags = client.get("/x/relation/tags")["data"]
    assert isinstance(tags, list)
    assert tags == data["groups"]
    assert all({"tagid", "name", "count"} == set(tag) for tag in tags)


def test_relation_single(client: MockBiliClient, data: dict):
    follow = data["follows"][0]
    relation = client.get("/x/relation", {"fid": follow["mid"]})["data"]
    assert int(relation["attribute"]) == int(follow["attribute"])
    stranger = client.get("/x/relation", {"fid": 1})["data"]
    assert int(stranger["attribute"]) == 0


def test_dynamics_cursor_pagination(client: MockBiliClient):
    offset = ""
    items: list[dict] = []
    pages = 0
    while pages < 100:
        payload = client.get(
            FAV_DYNAMICS_PATH,
            {
                "host_mid": client.user_mid,
                "offset": offset,
                "timezone_offset": -480,
                "features": "itemOpusStyle",
                "platform": "web",
            },
        )["data"]
        pages += 1
        assert set(payload) >= {"offset", "has_more", "items"}
        items.extend(payload["items"])
        if not payload["has_more"]:
            break
        offset = payload["offset"]
    assert pages == DYNAMIC_COUNT // 20
    assert len(items) == DYNAMIC_COUNT
    assert len({item["id_str"] for item in items}) == DYNAMIC_COUNT

    by_type: dict[str, dict] = {}
    for item in items:
        by_type.setdefault(item["type"], item)
        assert set(item) >= {"id_str", "type", "modules"}
        assert "module_author" in item["modules"]
        assert "module_dynamic" in item["modules"]
        author = item["modules"]["module_author"]
        assert isinstance(author["pub_ts"], int)
        assert author["name"]

    draw = by_type["DYNAMIC_TYPE_DRAW"]
    draw_items = draw["modules"]["module_dynamic"]["major"]["draw"]["items"]
    assert draw_items and draw_items[0]["src"].startswith("https://")
    assert draw["modules"]["module_dynamic"]["desc"]["text"]

    video = by_type["DYNAMIC_TYPE_AV"]
    archive = video["modules"]["module_dynamic"]["major"]["archive"]
    assert archive["bvid"].startswith("BV1") and archive["title"]

    forward = by_type["DYNAMIC_TYPE_FORWARD"]
    assert forward["orig"]["modules"]["module_author"]["name"]

    article = by_type["DYNAMIC_TYPE_ARTICLE"]
    assert article["modules"]["module_dynamic"]["major"]["article"]["title"]


def test_fav_folders_and_resources(client: MockBiliClient):
    folders = client.get("/x/v3/fav/folder/created/list-all", {"up_mid": client.user_mid})["data"]
    assert folders["count"] == 3
    by_media = {int(f["id"]): f for f in folders["list"]}
    assert set(by_media) == {916673043, 916673044, 916673045}
    assert by_media[916673043]["media_count"] == DEFAULT_FAV_COUNT
    assert by_media[916673043]["is_default"] is True
    assert by_media[916673044]["is_default"] is False
    assert by_media[916673043]["title"] == "默认收藏夹"

    page = client.get(
        FAV_RESOURCE_PATH,
        {"media_id": 916673043, "pn": 1, "ps": 20, "keyword": "", "order": "mtime"},
    )["data"]
    assert page["info"]["media_count"] == DEFAULT_FAV_COUNT
    assert len(page["medias"]) == 20
    assert page["has_more"] is True
    media = page["medias"][0]
    assert isinstance(media["id"], int)
    assert media["type"] == 2
    assert media["upper"]["name"]
    assert media["bvid"].startswith("BV1")
    assert media["attr"] in (0, 9)

    last = client.get(FAV_RESOURCE_PATH, {"media_id": 916673043, "pn": 25, "ps": 20})["data"]
    assert len(last["medias"]) == 20
    assert last["has_more"] is False

    # 默认收藏夹里一定有失效条目，且原始 attr 能看出来
    invalid = [
        media
        for pn in range(1, 26)
        for media in client.get(
            FAV_RESOURCE_PATH, {"media_id": 916673043, "pn": pn, "ps": 20}
        )["data"]["medias"]
        if media["attr"] != 0
    ]
    assert invalid
    assert all(media["title"] == "已失效视频" for media in invalid)


def test_toview_raw(client: MockBiliClient):
    payload = client.get("/x/v2/history/toview")["data"]
    assert payload["count"] == TOVIEW_COUNT
    assert len(payload["list"]) == TOVIEW_COUNT
    item = payload["list"][0]
    assert item["owner"]["name"]
    assert item["add_at"] == item["add_ts"]
    assert item["bvid"].startswith("BV1")
    assert item["duration"] > 0


def test_unknown_endpoint_raises(client: MockBiliClient):
    with pytest.raises(BiliApiError) as excinfo:
        client.get("/x/not-a-real-endpoint")
    assert excinfo.value.bili_code == -404

    with pytest.raises(BiliApiError):
        client.post("/x/not-a-real-endpoint", {})


# ================================================================ 写操作（数据真的减少）


def test_unfollow_removes_follow(client: MockBiliClient):
    mids = [f["mid"] for f in client.data["follows"][:3]]
    response = client.post(
        "/x/relation/modify",
        {"fid": mids[0], "act": 2, "re_src": 11, "csrf": client.csrf},
    )
    assert response["code"] == 0
    assert len(client.data["follows"]) == FOLLOW_COUNT - 1
    assert client.get(FOLLOWINGS_PATH, {"pn": 1, "ps": 50})["data"]["total"] == FOLLOW_COUNT - 1
    assert client.get("/x/relation", {"fid": mids[0]})["data"]["attribute"] == 0
    # 删不存在的（或重复删）不报错
    assert client.post("/x/relation/modify", {"fid": mids[0], "act": 2})["code"] == 0


def test_delete_dynamic(client: MockBiliClient):
    dyn_id = client.data["dynamics"][0]["id"]
    assert client.post(
        "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/rm_dynamic",
        {"dynamic_id": dyn_id, "csrf_token": client.csrf, "csrf": client.csrf},
    )["code"] == 0
    assert len(client.data["dynamics"]) == DYNAMIC_COUNT - 1
    first_page = client.get(FAV_DYNAMICS_PATH, {"offset": ""})["data"]
    assert dyn_id not in {item["id_str"] for item in first_page["items"]}

    with pytest.raises(BiliApiError) as excinfo:
        client.post(
            "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/rm_dynamic",
            {"dynamic_id": dyn_id},
        )
    assert excinfo.value.bili_code == 500404


def test_batch_delete_favorites(client: MockBiliClient):
    folder = client.data["favorites"][916673043]
    targets = folder[:2]
    resources = ",".join(f"{item['id']}:{item['type']}" for item in targets)
    result = client.post(
        "/x/v3/fav/resource/batch-del",
        {"media_id": 916673043, "resources": resources, "platform": "web", "csrf": client.csrf},
    )["data"]
    assert result["count"] == 2
    assert len(client.data["favorites"][916673043]) == DEFAULT_FAV_COUNT - 2
    page = client.get(FAV_RESOURCE_PATH, {"media_id": 916673043, "pn": 1, "ps": 20})["data"]
    assert page["info"]["media_count"] == DEFAULT_FAV_COUNT - 2
    assert targets[0]["id"] not in {str(media["id"]) for media in page["medias"]}


def test_clean_invalid_favorites(client: MockBiliClient):
    media_id = 916673043
    invalid_count = sum(1 for item in client.data["favorites"][media_id] if item["invalid"])
    assert invalid_count > 0
    result = client.post("/x/v3/fav/resource/clean", {"media_id": media_id, "csrf": client.csrf})["data"]
    assert result["count"] == invalid_count
    assert all(not item["invalid"] for item in client.data["favorites"][media_id])
    page = client.get(FAV_RESOURCE_PATH, {"media_id": media_id, "pn": 1, "ps": 20})["data"]
    assert page["info"]["media_count"] == DEFAULT_FAV_COUNT - invalid_count


def test_clear_toview(client: MockBiliClient):
    result = client.post("/x/v2/history/toview/clear", {"csrf": client.csrf})["data"]
    assert result["count"] == TOVIEW_COUNT
    assert client.data["toview"] == []
    assert client.get("/x/v2/history/toview")["data"]["count"] == 0


# ================================================================ 登录态


def test_logged_out_blocks_data_endpoints():
    client = MockBiliClient(logged_in=False)
    assert client.logged_in is False
    assert client.user is None
    assert client.user_mid == 0
    assert client.cookies == {}

    for call in (
        client.list_followings,
        client.list_groups,
        client.list_dynamics,
        client.list_folders,
        client.list_toview,
        client.fetch_me,
    ):
        with pytest.raises(AuthRequiredError):
            call()
    # 与真实 BiliClient.nav 对齐：未登录时返回 isLogin=False，而不是抛异常
    nav = client.nav()
    assert nav["isLogin"] is False
    assert nav["mid"] == 0
    with pytest.raises(AuthRequiredError):
        client.get("/x/web-interface/nav")
    with pytest.raises(AuthRequiredError):
        client.get("/x/relation/tags")
    with pytest.raises(AuthRequiredError):
        client.post("/x/v2/history/toview/clear", {})


def test_qr_login_flow():
    client = MockBiliClient(logged_in=False)
    qr = client.qrcode_generate()
    assert set(qr) == {"url", "key", "expires_in", "qr_png"}
    assert len(qr["key"]) == 32
    assert qr["url"].startswith("https://")
    assert qr["qr_png"].startswith("data:image/png;base64,")
    assert qr["expires_in"] > 0

    assert client.qrcode_poll("not-a-real-key")["status"] == "expired"

    first = client.qrcode_poll(qr["key"])
    assert first == {"status": "waiting", "user": None}
    second = client.qrcode_poll(qr["key"])
    assert second["status"] == "scanned"
    assert client.logged_in is False  # 还没确认

    third = client.qrcode_poll(qr["key"])
    assert third["status"] == "confirmed"
    assert third["user"]["mid"] == client.data["user"]["mid"]
    assert client.logged_in is True
    assert {"SESSDATA", "bili_jct", "DedeUserID"} <= set(client.cookies)
    assert client.fetch_me()["name"] == client.data["user"]["name"]

    # 登录后数据接口可用
    assert client.get("/x/relation/tags")["code"] == 0

    # 第四次轮询仍然返回 confirmed（与真实接口一致）
    assert client.qrcode_poll(qr["key"])["status"] == "confirmed"


def test_logout_then_requires_login(client: MockBiliClient):
    assert client.logged_in is True
    response = client.post("/x/web-interface/logout", {})
    assert response["code"] == 0
    assert client.logged_in is False
    assert client.cookies == {}
    with pytest.raises(AuthRequiredError):
        client.list_groups()

    # 也可以手工切回登录态
    client.set_logged_in(True)
    assert client.list_groups()
    assert client.user_mid == client.data["user"]["mid"]


# ================================================================ 写延迟 / 风控注入


def test_write_delay_only_affects_writes():
    client = MockBiliClient(write_delay=0.05)
    read_start = time.perf_counter()
    client.list_groups()
    read_elapsed = time.perf_counter() - read_start

    write_start = time.perf_counter()
    client.unfollow(client.data["follows"][0]["mid"])
    write_elapsed = time.perf_counter() - write_start

    assert write_elapsed >= 0.04
    assert read_elapsed < write_elapsed


def test_risk_control_injection_every_n_requests():
    client = MockBiliClient(risk_every=3)
    client.list_groups()  # 1
    client.list_groups()  # 2
    with pytest.raises(RiskControlError) as excinfo:
        client.list_groups()  # 3
    assert excinfo.value.bili_code == -352
    # 风控不改变数据
    assert len(client.data["follows"]) == FOLLOW_COUNT
    # 下一次请求恢复正常
    assert client.list_groups()  # 4


def test_risk_control_applies_to_writes_without_side_effects():
    client = MockBiliClient(risk_every=2)
    mids = [item["mid"] for item in client.data["follows"][:2]]
    client.unfollow(mids[0])  # 第 1 次请求，正常
    assert len(client.data["follows"]) == FOLLOW_COUNT - 1
    with pytest.raises(RiskControlError):
        client.unfollow(mids[1])  # 第 2 次请求，注入风控
    assert len(client.data["follows"]) == FOLLOW_COUNT - 1  # 写操作没有发生


def test_risk_control_off_by_default():
    client = MockBiliClient()
    for _ in range(20):
        client.list_groups()


# ================================================================ 高层便捷方法


def test_high_level_follows_filters(client: MockBiliClient, data: dict):
    page = client.list_followings(offset=0, limit=10)
    assert page["total"] == FOLLOW_COUNT
    assert page["offset"] == 0
    assert len(page["items"]) == 10
    assert set(page["items"][0]) == {
        "mid", "name", "sign", "face", "official", "attribute", "mutual",
        "groups", "group_ids", "order", "mtime", "category", "category_source",
    }

    mutual = client.list_followings(mutual="true", limit=100)
    assert mutual["total"] == MUTUAL_COUNT
    assert all(item["mutual"] for item in mutual["items"])
    assert client.list_followings(mutual=False)["total"] == FOLLOW_COUNT - MUTUAL_COUNT
    assert client.list_followings(mutual=None)["total"] == FOLLOW_COUNT

    group_name = GROUP_NAMES[0]
    group_total = next(g["count"] for g in data["groups"] if g["name"] == group_name)
    assert client.list_followings(group=group_name)["total"] == group_total
    # 也支持按 tagid 过滤
    tagid = next(g["tagid"] for g in data["groups"] if g["name"] == group_name)
    assert client.list_followings(group=str(tagid))["total"] == group_total

    category = data["follows"][0]["category"] or CATEGORIES[0]
    category_total = sum(1 for f in data["follows"] if f["category"] == category)
    assert client.list_followings(category=category)["total"] == category_total

    target = data["follows"][5]
    found = client.list_followings(q=target["name"])
    assert found["total"] >= 1
    assert any(item["mid"] == target["mid"] for item in found["items"])


def test_high_level_iter_helpers(client: MockBiliClient):
    follows = list(itertools.islice(client.iter_followings(), 120))
    assert len(follows) == 120
    assert len({item["mid"] for item in follows}) == 120

    dynamics = list(itertools.islice(client.iter_dynamics(), 25))
    assert len(dynamics) == 25
    assert all("type_label" in item for item in dynamics)

    fav_items = list(itertools.islice(client.iter_items(916673043), 25))
    assert len(fav_items) == 25
    assert all("invalid" in item for item in fav_items)


def test_high_level_dynamics_filters(client: MockBiliClient):
    page = client.list_dynamics(limit=20)
    assert page["has_more"] is True
    assert page["offset"] == "20"
    assert len(page["items"]) == 20

    forwards = client.list_dynamics(type_="转发", limit=1000)
    assert len(forwards["items"]) == 120
    assert forwards["has_more"] is False
    assert all(item["type_label"] == "转发" for item in forwards["items"])

    draws = client.list_dynamics(type_=DYNAMIC_TYPE_LABELS["DYNAMIC_TYPE_DRAW"], limit=1000)
    assert len(draws["items"]) == 99

    keyword = client.data["dynamics"][3]["text"][:2]
    matched = client.list_dynamics(q=keyword, limit=1000)
    assert len(matched["items"]) >= 1


def test_high_level_favorites(client: MockBiliClient):
    folders = client.list_folders()
    assert [f["title"] for f in folders] == ["默认收藏夹", "学习资料", "音乐现场"]
    assert folders[0]["count"] == DEFAULT_FAV_COUNT
    assert folders[0]["is_default"] is True

    page = client.list_fav_items(916673044, offset=0, limit=30)
    assert page["total"] == SELF_FAV_COUNTS[0]
    assert len(page["items"]) == 30

    with pytest.raises(BiliApiError):
        client.list_fav_items(123456)


def test_high_level_toview(client: MockBiliClient):
    items = client.list_toview()
    assert len(items) == TOVIEW_COUNT
    page = client.list_toview_page(offset=100, limit=50)
    assert page["total"] == TOVIEW_COUNT
    assert len(page["items"]) == 20
    assert client.clear_toview()["count"] == TOVIEW_COUNT
    assert client.list_toview() == []


def test_high_level_get_relation_and_categories(client: MockBiliClient):
    mutual = next(item for item in client.data["follows"] if item["mutual"])
    relation = client.get_relation(mutual["mid"])
    assert relation["followed"] is True
    assert relation["mutual"] is True
    assert relation["attribute"] == 6
    assert client.get_relation(1)["followed"] is False

    target = client.data["follows"][7]
    assert client.set_follow_category(target["mid"], "游戏") is True
    updated = client.list_followings(q=target["name"])["items"][0]
    assert updated["category"] == "游戏"
    assert updated["category_source"] == "user"

    assert client.set_categories({target["mid"]: "音乐", 99999999: "影视"}, source="llm") == 1
    updated = client.list_followings(q=target["name"])["items"][0]
    assert updated["category"] == "音乐"
    assert updated["category_source"] == "llm"


# ================================================================ 构造 / 生命周期


def test_constructor_is_tolerant():
    class Dummy:
        pass

    # 无参、BiliClient 同款位置参数、关键字参数都要能用
    assert MockBiliClient().logged_in is True
    assert MockBiliClient(Dummy(), Dummy(), Dummy()).logged_in is True
    custom = MockBiliClient(seed=7)
    assert custom.seed == 7
    assert custom.data != make_mock_data(42)
    assert custom.data == make_mock_data(7)
    assert MockBiliClient(logged_in=False).logged_in is False


def test_close_is_idempotent():
    client = MockBiliClient()
    client.close()
    client.close()
    assert client._closed is True


def test_reset_restores_initial_state():
    client = MockBiliClient()
    client.unfollow(client.data["follows"][0]["mid"])
    client.clear_toview()
    client.set_logged_in(False)
    assert len(client.data["follows"]) == FOLLOW_COUNT - 1

    client.reset()
    assert len(client.data["follows"]) == FOLLOW_COUNT
    assert len(client.data["toview"]) == TOVIEW_COUNT
    assert client.logged_in is True
    assert client.list_followings()["total"] == FOLLOW_COUNT


def test_user_and_session_facade(client: MockBiliClient):
    assert client.user["mid"] == 16888842
    assert client.user_mid == 16888842
    assert client.csrf
    assert client.session is not None
    assert client.session.cookies == client.cookies
    assert "User-Agent" in client.session.headers
    assert client.mock is True
    assert client.is_mock is True
