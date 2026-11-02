"""HTTP 层测试：FastAPI TestClient + 假客户端 / 假 bili 模块。

测试不连 B 站、也不依赖 ``bilicleaner.mock`` 包：通过
``create_app(..., client_factory=...)`` 注入假客户端，并 monkeypatch
``AppContext._bili`` / ``executors._bili`` 注入假数据源。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bilicleaner.api import app as app_module
from bilicleaner.const import DEFAULT_SETTINGS, VERSION
from bilicleaner.services import executors
from bilicleaner.store import Store


# ---------------------------------------------------------------- 夹具


def fast_settings(**overrides) -> dict:
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    settings.update(
        {
            "delay_seconds": 0.0,
            "jitter_seconds": 0.0,
            "batch_pause_seconds": 0.0,
            "cooldown_seconds": 0.0,
        }
    )
    settings.update(overrides)
    return settings


D1 = int(datetime(2024, 1, 10, 12, 0, 0).timestamp())
D2 = int(datetime(2024, 3, 10, 12, 0, 0).timestamp())
D3 = int(datetime(2024, 5, 10, 12, 0, 0).timestamp())

USER = {"mid": 42, "name": "测试用户", "face": "", "vip": False}


class FakeClient:
    """假 BiliClient：只要接口形状对，数据全部由假 bili 模块提供。"""

    def __init__(self):
        self.cookies = {"SESSDATA": "fake", "bili_jct": "fake"}
        self.settings = fast_settings()
        self.closed = False

    def close(self):
        self.closed = True


def make_state() -> dict:
    return {
        "poll_count": 0,
        "unfollowed": [],
        "deleted": [],
        "batch_deleted": [],
        "cleared": False,
    }


def make_bili_namespace(state: dict) -> SimpleNamespace:
    follows = [
        {
            "mid": 1,
            "name": "游戏区UP",
            "sign": "王者荣耀 游戏解说",
            "face": "",
            "official": "bilibili 知名游戏UP主",
            "attribute": 2,
            "mutual": False,
            "groups": ["游戏"],
            "group_ids": [100],
            "order": 1,
            "mtime": 1700000000,
            "category": None,
            "category_source": None,
        },
        {
            "mid": 2,
            "name": "学习区UP",
            "sign": "考研英语",
            "face": "",
            "official": "",
            "attribute": 6,
            "mutual": True,
            "groups": ["学习"],
            "group_ids": [200],
            "order": 2,
            "mtime": 1700000100,
            "category": None,
            "category_source": None,
        },
        {
            "mid": 3,
            "name": "未分类UP",
            "sign": "",
            "face": "",
            "official": "",
            "attribute": 2,
            "mutual": False,
            "groups": [],
            "group_ids": [],
            "order": 3,
            "mtime": 1700000200,
            "category": None,
            "category_source": None,
        },
        {
            "mid": 4,
            "name": "生活区UP",
            "sign": "vlog 日常记录",
            "face": "",
            "official": "",
            "attribute": 2,
            "mutual": False,
            "groups": [],
            "group_ids": [],
            "order": 4,
            "mtime": 1700000300,
            "category": None,
            "category_source": None,
        },
    ]
    groups = [
        {"tagid": 100, "name": "游戏", "count": 1},
        {"tagid": 200, "name": "学习", "count": 1},
    ]
    dynamics = [
        {
            "id": "1001",
            "type": "DYNAMIC_TYPE_WORD",
            "type_label": "文字",
            "pub_ts": D1,
            "text": "今天吃了火锅",
            "has_image": False,
            "video": None,
            "orig_author": None,
        },
        {
            "id": "1002",
            "type": "DYNAMIC_TYPE_AV",
            "type_label": "视频",
            "pub_ts": D2,
            "text": "分享一个视频",
            "has_image": False,
            "video": {"bvid": "BV1xx411c7mD", "title": "测试视频"},
            "orig_author": None,
        },
        {
            "id": "1003",
            "type": "DYNAMIC_TYPE_FORWARD",
            "type_label": "转发",
            "pub_ts": D3,
            "text": "转发动态",
            "has_image": True,
            "video": None,
            "orig_author": "某个UP",
        },
    ]
    folders = [
        {"media_id": 7, "title": "默认收藏夹", "count": 2, "is_default": True, "privacy": 0},
    ]
    fav_items = [
        {
            "id": "11",
            "type": 2,
            "title": "视频A",
            "upper": "UP主A",
            "bvid": "BV1",
            "cover": "",
            "fav_time": 1700000000,
            "invalid": False,
        },
        {
            "id": "12",
            "type": 2,
            "title": "视频B",
            "upper": "UP主B",
            "bvid": "BV2",
            "cover": "",
            "fav_time": 1700000100,
            "invalid": True,
        },
    ]
    toview = [
        {"aid": 1, "title": "稍后再看1", "upper": "UP", "duration": 100, "progress": 0, "add_ts": 1700000000, "bvid": "BV1"},
        {"aid": 2, "title": "稍后再看2", "upper": "UP", "duration": 200, "progress": 0, "add_ts": 1700000100, "bvid": "BV2"},
    ]

    def unfollow(client, mid):
        state["unfollowed"].append(int(mid))

    def delete_dynamic(client, dyn_id):
        state["deleted"].append(str(dyn_id))

    def batch_delete(client, media_id, resources):
        state["batch_deleted"].append((int(media_id), list(resources)))
        return {"count": len(resources)}

    def clean_invalid(client, media_id):
        return {"count": 1}

    def clear_toview(client):
        state["cleared"] = True
        return {"count": 2}

    def qrcode_generate(client):
        return {
            "url": "https://passport.bilibili.com/qrcode/h5/login?key=" + "k" * 32,
            "key": "k" * 32,
            "expires_in": 180,
            "qr_png": "data:image/png;base64,AAAA",
        }

    def qrcode_poll(client, key):
        state["poll_count"] += 1
        if state["poll_count"] < 2:
            return {"status": "waiting", "user": None}
        return {"status": "confirmed", "user": {"mid": 7, "name": "扫码用户", "face": "", "vip": False}}

    def fetch_me(client):
        return dict(USER)

    return SimpleNamespace(
        auth=SimpleNamespace(
            qrcode_generate=qrcode_generate,
            qrcode_poll=qrcode_poll,
            fetch_me=fetch_me,
        ),
        follows=SimpleNamespace(
            iter_followings=lambda client: [dict(item) for item in follows],
            list_groups=lambda client: [dict(item) for item in groups],
            unfollow=unfollow,
            get_relation=lambda client, mid: {"mid": mid, "attribute": 2},
        ),
        dynamics=SimpleNamespace(
            iter_dynamics=lambda client, mid=None: [dict(item) for item in dynamics],
            delete_dynamic=delete_dynamic,
        ),
        favorites=SimpleNamespace(
            list_folders=lambda client: [dict(item) for item in folders],
            iter_items=lambda client, media_id: [dict(item) for item in fav_items],
            batch_delete=batch_delete,
            clean_invalid=clean_invalid,
            MAX_BATCH_SIZE=20,
        ),
        toview=SimpleNamespace(
            list_toview=lambda client: [dict(item) for item in toview],
            clear_toview=clear_toview,
        ),
    )


@pytest.fixture
def api(monkeypatch, tmp_path):
    """带登录态的 app：假 client + 假 bili 数据。"""
    store = Store(str(tmp_path / "data"))
    store.save_settings(fast_settings())
    store.save_session({"SESSDATA": "fake"}, dict(USER))
    state = make_state()
    namespace = make_bili_namespace(state)
    monkeypatch.setattr(app_module.AppContext, "_bili", lambda self: namespace)
    monkeypatch.setattr(executors, "_bili", lambda: namespace)

    app = app_module.create_app(store=store, client_factory=lambda: FakeClient())
    with TestClient(app) as client:
        yield SimpleNamespace(
            store=store,
            client=client,
            app=app,
            ctx=app.state.ctx,
            tm=app.state.ctx.tm,
            state=state,
        )


def wait_task(client: TestClient, task_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    payload: dict = {}
    while time.time() < deadline:
        response = client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload.get("status") in ("done", "failed", "stopped"):
            return payload
        time.sleep(0.03)
    raise AssertionError(f"任务没有在 {timeout}s 内结束：{payload}")


# ---------------------------------------------------------------- 健康 / 设置


def test_health(api):
    response = api.client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "version": VERSION, "mock": False}


def test_settings_get_and_put(api):
    data = api.client.get("/api/settings").json()
    assert data["delay_seconds"] == 0.0
    assert data["llm"]["enabled"] is False

    response = api.client.put("/api/settings", json={"delay_seconds": 1.5, "batch_size": 10})
    assert response.status_code == 200
    updated = response.json()
    assert updated["delay_seconds"] == 1.5
    assert updated["batch_size"] == 10
    assert updated["llm"]["batch_size"] == 20  # 嵌套对象没被覆盖

    response = api.client.put(
        "/api/settings", json={"llm": {"enabled": True, "base_url": "https://x/v1", "model": "m"}}
    )
    assert response.status_code == 200
    llm = response.json()["llm"]
    assert llm["enabled"] is True and llm["base_url"] == "https://x/v1"
    assert llm["batch_size"] == 20

    response = api.client.put("/api/settings", json={"batch_size": 0})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"

    response = api.client.put("/api/settings", json={"llm": "不是对象"})
    assert response.status_code == 400


# ---------------------------------------------------------------- 认证


def test_me_and_logout(api):
    me = api.client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["logged_in"] is True
    assert me.json()["user"]["name"] == "测试用户"

    assert api.client.post("/api/auth/logout").json() == {"ok": True}
    me = api.client.get("/api/me").json()
    assert me["logged_in"] is False and me["user"] is None

    # 登录态失效后业务接口 401
    response = api.client.get("/api/follows")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_LOGGED_IN"


def test_qrcode_login_flow(api):
    api.store.clear_session()
    assert api.client.get("/api/me").json()["logged_in"] is False

    qr = api.client.post("/api/auth/qrcode")
    assert qr.status_code == 200
    body = qr.json()
    assert body["key"] and body["url"] and body["qr_png"].startswith("data:image/png;base64,")

    first = api.client.get("/api/auth/qrcode/poll", params={"key": body["key"]})
    assert first.status_code == 200
    assert first.json()["status"] == "waiting"
    assert first.json()["user"] is None

    second = api.client.get("/api/auth/qrcode/poll", params={"key": body["key"]}).json()
    assert second["status"] == "confirmed"
    assert second["user"]["name"] == "扫码用户"

    session = api.store.load_session()
    assert session and session["user"]["mid"] == 7
    me = api.client.get("/api/me").json()
    assert me["logged_in"] is True and me["user"]["name"] == "扫码用户"


# ---------------------------------------------------------------- 关注


def test_follows_list_and_filters(api):
    data = api.client.get("/api/follows").json()
    assert data["total"] == 4
    assert len(data["items"]) == 4
    assert data["items"][0]["category"] is None
    assert "category_source" in data["items"][0]

    assert api.client.get("/api/follows", params={"q": "游戏"}).json()["total"] == 1
    assert api.client.get("/api/follows", params={"mutual": "1"}).json()["total"] == 1
    assert api.client.get("/api/follows", params={"mutual": "0"}).json()["total"] == 3
    assert api.client.get("/api/follows", params={"group": 100}).json()["total"] == 1
    assert api.client.get("/api/follows", params={"group": 200}).json()["total"] == 1
    ungrouped = api.client.get("/api/follows", params={"group": 0}).json()
    assert ungrouped["total"] == 2
    assert api.client.get("/api/follows", params={"category": "__none__"}).json()["total"] == 4

    page = api.client.get("/api/follows", params={"offset": 2, "limit": 2}).json()
    assert page["total"] == 4 and page["offset"] == 2 and len(page["items"]) == 2

    groups = api.client.get("/api/follows/groups").json()
    assert [item["name"] for item in groups["items"]] == ["游戏", "学习"]


def test_follows_classify_and_stats(api):
    response = api.client.post("/api/follows/classify")
    assert response.status_code == 200
    assert response.json()["updated"] == 4

    stats = api.client.get("/api/follows/stats").json()
    assert stats["total"] == 4
    assert stats["mutual"] == 1
    assert stats["uncategorized"] == 0
    assert sum(stats["categories"].values()) == 4
    assert stats["categories"].get("游戏") == 1

    game = api.client.get("/api/follows", params={"category": "游戏"}).json()
    assert game["total"] == 1
    assert game["items"][0]["category"] == "游戏"
    assert game["items"][0]["category_source"] == "rule"

    # 分类缓存落盘在数据目录
    assert os.path.isfile(os.path.join(api.store.data_dir, "categories_42.json"))


# ---------------------------------------------------------------- 动态 / 收藏夹 / 稍后再看


def test_dynamics_filters_and_pagination(api):
    page = api.client.get("/api/dynamics", params={"limit": 2}).json()
    assert len(page["items"]) == 2
    assert page["has_more"] is True
    assert page["offset"] == "2"

    next_page = api.client.get("/api/dynamics", params={"limit": 2, "offset": "2"}).json()
    assert len(next_page["items"]) == 1
    assert next_page["has_more"] is False
    assert next_page["offset"] == ""

    assert api.client.get("/api/dynamics", params={"q": "火锅"}).json()["items"][0]["id"] == "1001"
    assert api.client.get("/api/dynamics", params={"type": "DYNAMIC_TYPE_AV"}).json()["items"][0]["id"] == "1002"
    assert api.client.get("/api/dynamics", params={"type": "视频"}).json()["items"][0]["id"] == "1002"

    date_from = datetime(2024, 4, 1).strftime("%Y-%m-%d")
    filtered = api.client.get("/api/dynamics", params={"date_from": date_from}).json()
    assert [item["id"] for item in filtered["items"]] == ["1003"]

    date_to = datetime(2024, 2, 1).strftime("%Y-%m-%d")
    filtered = api.client.get("/api/dynamics", params={"date_to": date_to}).json()
    assert [item["id"] for item in filtered["items"]] == ["1001"]

    bad = api.client.get("/api/dynamics", params={"date_from": "2024/01/01"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "BAD_REQUEST"


def test_favorites_and_toview(api):
    folders = api.client.get("/api/favorites/folders").json()
    assert folders["items"][0]["media_id"] == 7

    items = api.client.get("/api/favorites/7/items").json()
    assert items["total"] == 2
    assert items["items"][0]["title"] == "视频A"

    filtered = api.client.get("/api/favorites/7/items", params={"q": "视频B"}).json()
    assert filtered["total"] == 1 and filtered["items"][0]["invalid"] is True

    toview = api.client.get("/api/toview").json()
    assert toview["total"] == 2 and len(toview["items"]) == 2
    page = api.client.get("/api/toview", params={"offset": 1, "limit": 1}).json()
    assert len(page["items"]) == 1 and page["items"][0]["aid"] == 2


# ---------------------------------------------------------------- 任务


def test_task_validation_errors(api):
    api.tm.shutdown()  # 冻结工作线程，保证只看状态流转

    bad_type = api.client.post("/api/tasks", json={"type": "nope", "params": {}})
    assert bad_type.status_code == 400
    assert bad_type.json()["error"]["code"] == "BAD_TASK_TYPE"

    no_confirm = api.client.post("/api/tasks", json={"type": "toview_clear", "params": {}})
    assert no_confirm.status_code == 400
    assert no_confirm.json()["error"]["code"] == "CONFIRM_REQUIRED"

    no_ack = api.client.post("/api/tasks", json={"type": "unfollow", "params": {"mids": [1]}})
    assert no_ack.status_code == 400
    assert no_ack.json()["error"]["code"] == "ACK_REQUIRED"

    bad_ids = api.client.post(
        "/api/tasks",
        json={"type": "favorite_clear", "params": {"folder_id": 7, "ids": ["123"], "ack_risk": True}},
    )
    assert bad_ids.status_code == 400
    assert bad_ids.json()["error"]["code"] == "BAD_PARAMS"


def test_task_lifecycle_and_controls(api):
    api.tm.shutdown()

    created = api.client.post(
        "/api/tasks", json={"type": "unfollow", "params": {"mids": [1, 4], "ack_risk": True}}
    )
    assert created.status_code == 200
    task = created.json()
    assert task["status"] == "queued"
    task_id = task["id"]

    listing = api.client.get("/api/tasks").json()
    assert listing["items"][0]["id"] == task_id

    assert api.client.get(f"/api/tasks/{task_id}").json()["id"] == task_id
    missing = api.client.get("/api/tasks/does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"

    paused = api.client.post(f"/api/tasks/{task_id}/pause")
    assert paused.status_code == 200 and paused.json()["status"] == "paused"
    resumed = api.client.post(f"/api/tasks/{task_id}/resume")
    assert resumed.status_code == 200 and resumed.json()["status"] == "queued"
    stopped = api.client.post(f"/api/tasks/{task_id}/stop")
    assert stopped.status_code == 200 and stopped.json()["status"] == "stopped"

    again = api.client.post(f"/api/tasks/{task_id}/pause")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "TASK_FINISHED"

    not_paused = api.client.post(f"/api/tasks/{task_id}/resume")
    assert not_paused.status_code == 409
    assert not_paused.json()["error"]["code"] == "TASK_NOT_PAUSED"


def test_task_runs_end_to_end_with_backup_and_logs(api):
    created = api.client.post(
        "/api/tasks", json={"type": "unfollow", "params": {"mids": [1, 2, 4], "ack_risk": True}}
    )
    assert created.status_code == 200
    task_id = created.json()["id"]

    done = wait_task(api.client, task_id)
    assert done["status"] == "done", done["message"]
    assert done["total"] == 2  # 互关的 mid=2 被 protect_mutual 过滤
    assert done["done"] == 2
    assert api.state["unfollowed"] == [1, 4]

    backup_dir = done["backup_path"]
    assert backup_dir and os.path.isdir(backup_dir)
    assert os.path.isfile(os.path.join(backup_dir, "unfollow.csv"))
    assert os.path.isfile(os.path.join(backup_dir, "unfollow.jsonl"))
    with open(os.path.join(backup_dir, "meta.json"), "r", encoding="utf-8") as f:
        assert json.load(f)["task_id"] == task_id

    backups = api.client.get("/api/backups").json()
    assert backups["items"] and backups["items"][0]["count"] == 2

    logs = api.client.get("/api/logs", params={"limit": 50}).json()
    assert logs["items"]
    task_logs = api.client.get("/api/logs", params={"limit": 50, "task_id": task_id}).json()
    assert all(entry["task_id"] == task_id for entry in task_logs["items"])


def test_toview_clear_task_writes_backup(api):
    created = api.client.post(
        "/api/tasks", json={"type": "toview_clear", "params": {}, "confirm_phrase": "清空"}
    )
    assert created.status_code == 200
    done = wait_task(api.client, created.json()["id"])
    assert done["status"] == "done", done["message"]
    assert done["done"] == 2
    assert api.state["cleared"] is True
    assert os.path.isfile(os.path.join(done["backup_path"], "toview_clear.jsonl"))


# ---------------------------------------------------------------- 静态文件 / 错误格式


def test_static_and_error_format(api):
    index = api.client.get("/")
    assert index.status_code == 200
    assert "text/html" in index.headers["content-type"]

    missing = api.client.get("/static/definitely-not-exist.js")
    assert missing.status_code == 404

    unknown = api.client.get("/api/definitely-not-exist")
    assert unknown.status_code == 404
    body = unknown.json()
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == "NOT_FOUND"

    wrong_method = api.client.post("/api/follows")
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_favicon_returns_no_content(api):
    """Chromium 每会话都会请求 /favicon.ico，不该在控制台留一条 404。"""
    response = api.client.get("/favicon.ico")
    assert response.status_code == 204
    assert response.content == b""


def test_shutdown_returns_ok_and_calls_hook_once(api):
    """关闭接口：先回 200，真正的退出动作走可注入 hook（测试里不杀进程）。"""
    calls: list[bool] = []
    hook_called = threading.Event()

    def fake_shutdown_hook() -> None:
        calls.append(True)
        hook_called.set()

    api.app.state.shutdown_hook = fake_shutdown_hook
    api.ctx.shutdown_delay = 0.0

    response = api.client.post("/api/shutdown")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert hook_called.wait(3.0), "shutdown hook 没有被调用"
    assert calls == [True]

    # 重复请求只安排一次退出，接口仍然幂等返回 200
    again = api.client.post("/api/shutdown")
    assert again.status_code == 200
    assert again.json() == {"ok": True}
    time.sleep(0.2)
    assert calls == [True]


def test_requires_login_when_no_session(monkeypatch, tmp_path):
    store = Store(str(tmp_path / "data2"))
    store.save_settings(fast_settings())
    state = make_state()
    namespace = make_bili_namespace(state)
    monkeypatch.setattr(app_module.AppContext, "_bili", lambda self: namespace)
    app = app_module.create_app(store=store, client_factory=lambda: FakeClient())
    with TestClient(app) as client:
        assert client.get("/api/me").json()["logged_in"] is False
        response = client.get("/api/follows")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "NOT_LOGGED_IN"
        # 未登录也能生成二维码
        assert client.post("/api/auth/qrcode").status_code == 200
