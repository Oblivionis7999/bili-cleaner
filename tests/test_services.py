"""服务层测试：限速、备份、分类、任务引擎与执行器。

全部离线：不连 B 站，用 monkeypatch 注入假 bili 模块与假客户端。
"""

from __future__ import annotations

import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from bilicleaner.const import CATEGORIES, DEFAULT_SETTINGS
from bilicleaner.errors import BackupError, BiliCleanerError, RiskControlError, TaskError
from bilicleaner.services import classify as classify_mod
from bilicleaner.services import executors
from bilicleaner.services.backup import BackupService
from bilicleaner.services.classify import CategoryCache, LlmClassifier, classify_rule
from bilicleaner.services.ratelimit import RateLimiter
from bilicleaner.services.task_engine import TaskManager
from bilicleaner.store import Store


# ---------------------------------------------------------------- 夹具


def fast_settings(**overrides) -> dict:
    """把等待时间全部归零，让测试不会真的睡 2.5 秒。"""
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


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "data"))


@pytest.fixture
def fast_store(store):
    store.save_settings(fast_settings())
    return store


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def make_follows() -> list[dict]:
    return [
        {
            "mid": 1,
            "name": "游戏区UP",
            "sign": "王者荣耀 游戏解说",
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
            "sign": "考研英语 每天一节",
            "official": "",
            "attribute": 2,
            "mutual": False,
            "groups": ["学习"],
            "group_ids": [200],
            "order": 2,
            "mtime": 1700000100,
            "category": None,
            "category_source": None,
        },
        {
            "mid": 3,
            "name": "生活记录",
            "sign": "vlog 日常",
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
            "name": "互关好友",
            "sign": "",
            "official": "",
            "attribute": 6,
            "mutual": True,
            "groups": ["朋友"],
            "group_ids": [300],
            "order": 4,
            "mtime": 1700000300,
            "category": None,
            "category_source": None,
        },
    ]


@pytest.fixture
def fake_state():
    state = {
        "calls": [],
        "risk": {},
        "read_risk": 0,
        "gates": {},
        "cleared": False,
        "batch_calls": [],
        "clean_calls": [],
        "delete_calls": [],
        "follows": make_follows(),
        "dynamics": [
            {"id": "111", "type": "DYNAMIC_TYPE_WORD", "type_label": "文字", "pub_ts": 1700000000, "text": "第一条"},
            {"id": "222", "type": "DYNAMIC_TYPE_AV", "type_label": "视频", "pub_ts": 1700100000, "text": "第二条"},
        ],
        "folders": [{"media_id": 7, "title": "默认收藏夹", "count": 2, "is_default": True, "privacy": 0}],
        "fav_items": {
            7: [
                {"id": "11", "type": 2, "title": "视频A", "upper": "UP主A", "bvid": "BV1", "fav_time": 1700000000, "invalid": False},
                {"id": "12", "type": 2, "title": "视频B", "upper": "UP主B", "bvid": "BV2", "fav_time": 1700000100, "invalid": True},
            ]
        },
        "toview": [
            {"aid": 1, "title": "稍后再看1", "upper": "UP", "duration": 100, "progress": 0, "add_ts": 1700000000, "bvid": "BV1"},
            {"aid": 2, "title": "稍后再看2", "upper": "UP", "duration": 200, "progress": 0, "add_ts": 1700000100, "bvid": "BV2"},
        ],
    }

    def _unfollow(client, mid):
        state["calls"].append(int(mid))
        gate = state["gates"].get(int(mid))
        if gate is not None:
            assert gate.wait(5), "测试卡住：gate 没有被释放"
        remaining = state["risk"].get(int(mid), 0)
        if remaining > 0:
            state["risk"][int(mid)] = remaining - 1
            raise RiskControlError(-352, "风控")

    def _batch_delete(client, media_id, resources):
        state["batch_calls"].append((int(media_id), list(resources)))
        return {"count": len(resources)}

    def _clean_invalid(client, media_id):
        state["clean_calls"].append(int(media_id))
        return {"count": 1}

    def _delete_dynamic(client, dyn_id):
        state["delete_calls"].append(str(dyn_id))
        return None

    def _iter_items(client, media_id):
        # 模拟读接口偶尔撞风控，执行器应该冷却后重试而不是让任务直接失败
        if state["read_risk"] > 0:
            state["read_risk"] -= 1
            raise RiskControlError(-412, "风控")
        return [dict(item) for item in state["fav_items"].get(int(media_id), [])]

    def _clear_toview(client):
        state["cleared"] = True
        state["toview"] = []
        return {"count": 1}

    namespace = SimpleNamespace(
        auth=SimpleNamespace(fetch_me=lambda client: {"mid": 42, "name": "测试账号"}),
        follows=SimpleNamespace(
            iter_followings=lambda client: [dict(item) for item in state["follows"]],
            list_groups=lambda client: [],
            unfollow=_unfollow,
            get_relation=lambda client, mid: {"mid": mid, "attribute": 2},
        ),
        dynamics=SimpleNamespace(
            iter_dynamics=lambda client, mid: [dict(item) for item in state["dynamics"]],
            delete_dynamic=_delete_dynamic,
        ),
        favorites=SimpleNamespace(
            list_folders=lambda client: [dict(item) for item in state["folders"]],
            iter_items=_iter_items,
            batch_delete=_batch_delete,
            clean_invalid=_clean_invalid,
        ),
        toview=SimpleNamespace(
            list_toview=lambda client: [dict(item) for item in state["toview"]],
            clear_toview=_clear_toview,
        ),
    )
    return state, namespace


@pytest.fixture
def fake_bili(monkeypatch, fake_state):
    state, namespace = fake_state
    monkeypatch.setattr(executors, "_bili", lambda: namespace)
    return state, namespace


def make_tm(store, backup=None, classify=None, logs=None) -> TaskManager:
    logs = logs if logs is not None else []

    def _logger(level, msg, task_id=None):
        logs.append({"level": level, "msg": msg, "task_id": task_id})

    return TaskManager(
        store=store,
        client_factory=lambda: SimpleNamespace(cookies={}),
        limiter_factory=lambda: RateLimiter(store.load_settings()),
        backup=backup if backup is not None else BackupService(store),
        classify=classify if classify is not None else classify_mod,
        logger=_logger,
    )


# ================================================================ RateLimiter


def test_ratelimiter_batch_pause():
    limiter = RateLimiter(fast_settings(batch_size=2, batch_pause_seconds=0.15))
    limiter.wait()
    limiter.wait()  # 批内第 2 条
    started = time.time()
    limiter.wait()  # 第 3 条前应该先休息
    assert time.time() - started >= 0.12
    assert limiter.stats()["batch_count"] == 1


def test_ratelimiter_cooldown_state_machine():
    limiter = RateLimiter(fast_settings(cooldown_seconds=0.05, max_cooldowns=2))
    assert limiter.on_risk(-352) is True
    assert limiter.consecutive_risks == 1
    assert limiter.cooldown_remaining() > 0
    assert limiter.cooldown_until_iso() is not None
    assert limiter.on_risk(-412) is True
    # 第三次连续风控超过 max_cooldowns，必须停
    assert limiter.on_risk(-799) is False
    limiter.on_success()
    limiter.reset()
    assert limiter.consecutive_risks == 0
    assert limiter.cooldown_until_iso() is None


def test_ratelimiter_waits_out_cooldown():
    limiter = RateLimiter(fast_settings(cooldown_seconds=0.12, max_cooldowns=3))
    assert limiter.on_risk(-352) is True
    started = time.time()
    limiter.wait()
    assert time.time() - started >= 0.1


def test_ratelimiter_risk_code_helper():
    assert RateLimiter.is_risk_code(-352)
    assert RateLimiter.is_risk_code("-799")
    assert not RateLimiter.is_risk_code(0)
    assert not RateLimiter.is_risk_code(None)


def test_ratelimiter_abort_check_breaks_long_sleep():
    limiter = RateLimiter(fast_settings(cooldown_seconds=5, max_cooldowns=3))
    limiter.set_abort_check(lambda: True)
    assert limiter.on_risk(-352) is True
    started = time.time()
    limiter.wait()
    assert time.time() - started < 1.0


# ================================================================ BackupService


def test_backup_export_writes_csv_and_jsonl(store):
    service = BackupService(store)
    items = make_follows()
    result = service.export("unfollow", items, {"mid": 42, "name": "测试账号"}, task_id="t1")

    assert result["count"] == 4
    assert os.path.isfile(result["csv_path"])
    assert os.path.isfile(result["jsonl_path"])

    with open(result["csv_path"], "r", encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    assert lines[0].startswith("UID,昵称")
    assert len(lines) == 5

    with open(result["jsonl_path"], "r", encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]
    assert len(records) == 4
    assert records[0]["name"] == "游戏区UP"

    meta_path = os.path.join(os.path.dirname(result["csv_path"]), "meta.json")
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    assert meta["task_id"] == "t1"
    assert meta["count"] == 4

    listed = service.list_backups()
    assert listed and listed[0]["count"] == 4
    assert listed[0]["task_id"] == "t1"


def test_backup_failure_raises_backup_error(store, monkeypatch):
    service = BackupService(store)

    def boom(_mid):
        raise OSError("磁盘满了")

    monkeypatch.setattr(store, "backup_dir_for", boom)
    with pytest.raises(BackupError):
        service.export("unfollow", make_follows(), {"mid": 1})


def test_backup_prune_keeps_latest(store):
    store.save_settings(fast_settings(backup_keep=1))
    account_dir = os.path.join(store.backups_dir, "42")
    for stamp in ("20200101-000000", "20200102-000000"):
        os.makedirs(os.path.join(account_dir, stamp), exist_ok=True)
    BackupService(store).export("unfollow", make_follows(), {"mid": 42})

    remaining = os.listdir(account_dir)
    assert len(remaining) == 1
    assert remaining[0] not in ("20200101-000000", "20200102-000000")


# ================================================================ classify


def test_categories_match_contract():
    assert len(CATEGORIES) == 21
    assert CATEGORIES[-1] == "其他·难判断"


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"name": "老番茄", "sign": "游戏区UP", "official": "bilibili 知名游戏UP主"}, "游戏"),
        ({"name": "影视飓风", "sign": "影像科技", "official": "bilibili 知名科技UP主"}, "科技数码"),
        ({"name": "零基础学Python", "sign": "编程 算法", "official": ""}, "编程·AI"),
        ({"name": "考研数学", "sign": "每天一节", "official": "", "groups": ["学习"]}, "学习·教育"),
        ({"name": "嘉然今天吃什么", "sign": "虚拟主播", "official": "bilibili 知名虚拟UP主"}, "虚拟UP主·VTuber"),
        ({"name": "美食作家王刚", "sign": "做菜", "official": "bilibili 知名美食UP主"}, "美食"),
    ],
)
def test_classify_rule_examples(item, expected):
    category, confidence = classify_rule(item)
    assert category == expected
    assert 0.0 <= confidence <= 1.0


def test_classify_rule_fallback():
    category, confidence = classify_rule({"name": "张三", "sign": "", "official": ""})
    assert category == "其他·难判断"
    assert confidence <= 0.3


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


def test_llm_classifier_parses_and_filters(monkeypatch):
    content = json.dumps(
        [
            {"mid": 1, "category": "游戏"},
            {"mid": 2, "category": "不存在的类别"},
        ],
        ensure_ascii=False,
    )
    payload = {"choices": [{"message": {"content": content}}]}
    monkeypatch.setattr("requests.post", lambda *a, **k: _FakeResponse(payload))

    classifier = LlmClassifier({"base_url": "https://example.com/v1", "api_key": "k", "model": "m"})
    assert classifier.configured
    result = classifier.classify([{"mid": 1, "name": "a"}, {"mid": 2, "name": "b"}])
    assert result == {1: "游戏"}


def test_llm_classifier_failure_raises(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("网络炸了")

    monkeypatch.setattr("requests.post", boom)
    classifier = LlmClassifier({"base_url": "https://example.com/v1", "model": "m"})
    with pytest.raises(BiliCleanerError):
        classifier.classify([{"mid": 1}])


def test_category_cache_roundtrip(store):
    store.save_session({"SESSDATA": "x"}, {"mid": 42, "name": "我"})
    cache = CategoryCache(store)
    assert cache.path().endswith("categories_42.json")
    assert cache.update({1: ("游戏", "rule"), 2: ("学习·教育", "llm")}) == 2
    assert cache.get(1)["category"] == "游戏"
    assert cache.get(2)["source"] == "llm"
    # 新实例（新进程）也能读到
    again = CategoryCache(store)
    assert again.get(1)["category"] == "游戏"


# ================================================================ 任务引擎 / 执行器


def test_create_validates_params(fast_store):
    tm = make_tm(fast_store)

    with pytest.raises(TaskError) as info:
        tm.create("nope", {})
    assert info.value.code == "BAD_TASK_TYPE"

    with pytest.raises(TaskError) as info:
        tm.create("toview_clear", {})
    assert info.value.code == "CONFIRM_REQUIRED"

    with pytest.raises(TaskError) as info:
        tm.create("unfollow", {"mids": [1]})
    assert info.value.code == "ACK_REQUIRED"

    with pytest.raises(TaskError) as info:
        tm.create("favorite_clear", {"folder_id": 7, "ids": ["1:2"]})
    assert info.value.code == "ACK_REQUIRED"

    # 部分删除 + ack 即可，不需要确认词
    task = tm.create("favorite_clear", {"folder_id": 7, "ids": ["1:2"], "ack_risk": True})
    assert task["status"] == "queued"
    assert task["params"]["ids"] == ["1:2"]


def test_unfollow_task_end_to_end(fast_store, fake_bili):
    state, _ = fake_bili
    tm = make_tm(fast_store)
    tm.start()
    try:
        task = tm.create("unfollow", {"mids": [1, 2, 3, 4], "ack_risk": True, "reason": "manual"})
        assert task["status"] == "queued"
        assert tm.wait_idle(5)

        done_task = tm.get(task["id"])
        assert done_task["status"] == "done"
        assert done_task["total"] == 3  # 互关的 mid=4 被保护过滤
        assert done_task["done"] == 3
        assert state["calls"] == [1, 2, 3]

        # 备份文件齐全
        backup_dir = done_task["backup_path"]
        assert backup_dir and os.path.isdir(backup_dir)
        assert os.path.isfile(os.path.join(backup_dir, "unfollow.csv"))
        assert os.path.isfile(os.path.join(backup_dir, "unfollow.jsonl"))
        with open(os.path.join(backup_dir, "unfollow.jsonl"), "r", encoding="utf-8") as f:
            assert len([line for line in f if line.strip()]) == 3

        # 操作日志可续跑
        ops = os.path.join(fast_store.data_dir, "task_ops", f"{task['id']}.jsonl")
        with open(ops, "r", encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
        assert {rec["key"] for rec in records if rec["ok"]} == {"1", "2", "3"}
    finally:
        tm.shutdown()


def test_unfollow_backup_failure_fails_task(fast_store, fake_bili):
    state, _ = fake_bili

    class BrokenBackup:
        def export(self, *args, **kwargs):
            raise BackupError("磁盘满了")

    tm = make_tm(fast_store, backup=BrokenBackup())
    tm.start()
    try:
        task = tm.create("unfollow", {"mids": [1, 3], "ack_risk": True})
        assert tm.wait_idle(5)
        done_task = tm.get(task["id"])
        assert done_task["status"] == "failed"
        assert "备份" in done_task["message"]
        assert state["calls"] == []  # 备份失败时一条写请求都不能发
    finally:
        tm.shutdown()


def test_risk_control_cools_down_then_continues(fast_store, fake_bili):
    state, _ = fake_bili
    state["risk"][1] = 2  # 前两次触发风控
    fast_store.save_settings(fast_settings(cooldown_seconds=0, max_cooldowns=3))
    tm = make_tm(fast_store)
    tm.start()
    try:
        task = tm.create("unfollow", {"mids": [1], "ack_risk": True})
        assert tm.wait_idle(5)
        done_task = tm.get(task["id"])
        assert done_task["status"] == "done"
        assert done_task["done"] == 1
        assert state["calls"] == [1, 1, 1]
    finally:
        tm.shutdown()


def test_risk_control_gives_up_after_max_cooldowns(fast_store, fake_bili):
    state, _ = fake_bili
    state["risk"][1] = 99
    fast_store.save_settings(fast_settings(cooldown_seconds=0, max_cooldowns=1))
    tm = make_tm(fast_store)
    tm.start()
    try:
        task = tm.create("unfollow", {"mids": [1], "ack_risk": True})
        assert tm.wait_idle(5)
        done_task = tm.get(task["id"])
        assert done_task["status"] == "stopped"
        assert "风控" in done_task["message"]
        assert state["calls"] == [1, 1]
    finally:
        tm.shutdown()


def test_pause_then_resume(fast_store, fake_bili):
    state, _ = fake_bili
    gate = threading.Event()
    state["gates"][2] = gate

    tm = make_tm(fast_store)
    tm.start()
    try:
        task = tm.create("unfollow", {"mids": [1, 2, 3], "ack_risk": True})
        assert wait_for(lambda: 2 in state["calls"])

        paused = tm.pause(task["id"])
        assert paused["status"] == "paused"
        gate.set()
        # 2 号完成，3 号前被暂停拦住
        assert wait_for(lambda: (tm.get(task["id"])["done"] or 0) == 2)
        time.sleep(0.1)
        assert state["calls"] == [1, 2]

        resumed = tm.resume(task["id"])
        assert resumed["status"] == "running"
        assert tm.wait_idle(5)
        assert state["calls"] == [1, 2, 3]
        assert tm.get(task["id"])["status"] == "done"
    finally:
        gate.set()
        tm.shutdown()


def test_stop_mid_task(fast_store, fake_bili):
    state, _ = fake_bili
    gate = threading.Event()
    state["gates"][1] = gate

    tm = make_tm(fast_store)
    tm.start()
    try:
        task = tm.create("unfollow", {"mids": [1, 2, 3], "ack_risk": True})
        assert wait_for(lambda: 1 in state["calls"])
        stopped = tm.stop(task["id"])
        assert stopped["status"] == "stopped"
        gate.set()
        assert tm.wait_idle(5)
        assert state["calls"] == [1]
        assert tm.get(task["id"])["status"] == "stopped"
    finally:
        gate.set()
        tm.shutdown()


def test_resume_from_operation_log(fast_store, fake_bili):
    """模拟进程重启：tasks.json 里还是 queued，操作日志里已有成功的对象。"""
    state, _ = fake_bili
    first = make_tm(fast_store)
    task = first.create("unfollow", {"mids": [1, 3], "ack_risk": True})
    first.append_op(task, "1", True, 0, "上次已成功")
    first.shutdown()  # 从未 start，等价于进程被杀

    second = make_tm(fast_store)
    second.start()
    try:
        assert second.wait_idle(5)
        done_task = second.get(task["id"])
        assert done_task["status"] == "done"
        assert state["calls"] == [3]  # 跳过已成功的 1
        assert done_task["done"] == 2
    finally:
        second.shutdown()


def test_favorite_clear_full_folder(fast_store, fake_bili):
    state, _ = fake_bili
    tm = make_tm(fast_store)
    tm.start()
    try:
        task = tm.create("favorite_clear", {"folder_id": 7}, confirm_phrase="清空")
        assert tm.wait_idle(5)
        done_task = tm.get(task["id"])
        assert done_task["status"] == "done"
        assert done_task["done"] == 2
        assert state["batch_calls"] == [(7, ["11:2", "12:2"])]
        backup_dir = done_task["backup_path"]
        assert os.path.isfile(os.path.join(backup_dir, "favorite_clear.csv"))
    finally:
        tm.shutdown()


def test_favorite_clear_selected_items(fast_store, fake_bili):
    state, _ = fake_bili
    tm = make_tm(fast_store)
    tm.start()
    try:
        task = tm.create("favorite_clear", {"folder_id": 7, "ids": ["11:2"], "ack_risk": True})
        assert tm.wait_idle(5)
        done_task = tm.get(task["id"])
        assert done_task["status"] == "done"
        assert done_task["done"] == 1
        assert state["batch_calls"] == [(7, ["11:2"])]
    finally:
        tm.shutdown()


def test_toview_clear_and_confirm_recheck(fast_store, fake_bili):
    state, _ = fake_bili
    tm = make_tm(fast_store)
    tm.start()
    try:
        task = tm.create("toview_clear", {}, confirm_phrase="清空")
        assert tm.wait_idle(5)
        done_task = tm.get(task["id"])
        assert done_task["status"] == "done"
        assert done_task["done"] == 2
        assert state["cleared"] is True
    finally:
        tm.shutdown()

    # 创建后偷偷改掉确认词：执行器必须拒绝执行（先改设置再启动，避免并发写 settings.json）
    state["cleared"] = False
    slow = make_tm(fast_store)
    task = slow.create("toview_clear", {}, confirm_phrase="清空")
    fast_store.save_settings(fast_settings(confirm_phrase="删光"))
    slow.start()
    try:
        assert slow.wait_idle(5)
        failed_task = slow.get(task["id"])
        assert failed_task["status"] == "failed"
        assert "确认词" in failed_task["message"]
        assert state["cleared"] is False
    finally:
        slow.shutdown()


def test_favorite_clear_rejects_bad_ids_format(fast_store):
    tm = make_tm(fast_store)
    with pytest.raises(TaskError) as info:
        tm.create("favorite_clear", {"folder_id": 7, "ids": ["123"], "ack_risk": True})
    assert info.value.code == "BAD_PARAMS"
    with pytest.raises(TaskError) as info:
        tm.create("favorite_clear", {"folder_id": 7, "ids": ["abc:2"], "ack_risk": True})
    assert info.value.code == "BAD_PARAMS"


def test_read_risk_control_cools_then_retries(fast_store, fake_bili):
    state, _ = fake_bili
    state["read_risk"] = 1
    fast_store.save_settings(fast_settings(cooldown_seconds=0, max_cooldowns=3))
    tm = make_tm(fast_store)
    tm.start()
    try:
        task = tm.create("favorite_clean_invalid", {"folder_id": 7}, confirm_phrase="清空")
        assert tm.wait_idle(5)
        done_task = tm.get(task["id"])
        assert done_task["status"] == "done", done_task["message"]
        assert done_task["done"] == 1
        assert state["clean_calls"] == [7]
    finally:
        tm.shutdown()


def test_dynamic_delete_end_to_end(fast_store, fake_bili):
    state, _ = fake_bili
    tm = make_tm(fast_store)
    tm.start()
    try:
        task = tm.create("dynamic_delete", {"ids": ["111", "222"]})
        assert tm.wait_idle(5)
        done_task = tm.get(task["id"])
        assert done_task["status"] == "done"
        assert done_task["done"] == 2
        assert state["delete_calls"] == ["111", "222"]
        backup_dir = done_task["backup_path"]
        with open(os.path.join(backup_dir, "dynamic_delete.csv"), "r", encoding="utf-8-sig") as f:
            assert "第一条" in f.read()
    finally:
        tm.shutdown()


def test_favorite_clean_invalid(fast_store, fake_bili):
    state, _ = fake_bili
    tm = make_tm(fast_store)
    tm.start()
    try:
        task = tm.create("favorite_clean_invalid", {"folder_id": 7}, confirm_phrase="清空")
        assert tm.wait_idle(5)
        done_task = tm.get(task["id"])
        assert done_task["status"] == "done"
        assert done_task["done"] == 1
        assert state["clean_calls"] == [7]
        backup_dir = done_task["backup_path"]
        with open(
            os.path.join(backup_dir, "favorite_clean_invalid.jsonl"), "r", encoding="utf-8"
        ) as f:
            records = [json.loads(line) for line in f if line.strip()]
        assert len(records) == 1 and records[0]["invalid"] is True
    finally:
        tm.shutdown()


def test_classify_llm_falls_back_to_rule(fast_store, fake_bili):
    tm = make_tm(fast_store)
    tm.start()
    try:
        task = tm.create("classify_llm", {"mids": [1, 2]})
        assert tm.wait_idle(5)
        done_task = tm.get(task["id"])
        assert done_task["status"] == "done"
        assert done_task["done"] == 2
        cache = CategoryCache(fast_store)
        assert cache.get(1)["category"] == "游戏"
        assert cache.get(1)["source"] == "rule"
    finally:
        tm.shutdown()


def test_classify_llm_uses_injected_classifier(fast_store, fake_bili):
    class FakeLlm:
        def __init__(self, cfg):
            self.configured = True

        def classify(self, items):
            return {int(item["mid"]): "游戏" for item in items}

    fake_mod = SimpleNamespace(
        CategoryCache=CategoryCache,
        LlmClassifier=FakeLlm,
        classify_rule=classify_mod.classify_rule,
    )
    fast_store.save_settings(
        fast_settings(llm={"enabled": True, "base_url": "https://x/v1", "api_key": "k", "model": "m", "batch_size": 20})
    )
    tm = make_tm(fast_store, classify=fake_mod)
    tm.start()
    try:
        task = tm.create("classify_llm", {"mids": [1, 2]})
        assert tm.wait_idle(5)
        assert tm.get(task["id"])["status"] == "done"
        cache = CategoryCache(fast_store)
        assert cache.get(2)["category"] == "游戏"
        assert cache.get(2)["source"] == "llm"
    finally:
        tm.shutdown()


def test_task_persisted_across_managers(fast_store, fake_bili):
    tm = make_tm(fast_store)
    tm.start()
    try:
        task = tm.create("unfollow", {"mids": [1], "ack_risk": True})
        assert tm.wait_idle(5)
    finally:
        tm.shutdown()

    reloaded = make_tm(fast_store)
    tasks = reloaded.list()
    assert len(tasks) == 1
    assert tasks[0]["id"] == task["id"]
    assert tasks[0]["status"] == "done"
    assert tasks[0]["backup_path"]


def test_pause_and_resume_queued_task(fast_store):
    """没启动工作线程时，暂停/继续只改状态和队列。"""
    tm = make_tm(fast_store)
    task = tm.create("unfollow", {"mids": [1], "ack_risk": True})
    assert tm.pause(task["id"])["status"] == "paused"
    resumed = tm.resume(task["id"])
    assert resumed["status"] == "queued"
    stopped = tm.stop(task["id"])
    assert stopped["status"] == "stopped"

    with pytest.raises(TaskError):
        tm.resume(task["id"])
    with pytest.raises(TaskError):
        tm.pause(task["id"])
