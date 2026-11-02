"""Store 的单元测试：DPAPI 登录态、设置合并、任务/日志读写、备份目录。"""

from __future__ import annotations

import json
import os
import sys

import pytest

from bilicleaner.const import DEFAULT_SETTINGS
from bilicleaner.store import Store, default_data_dir, protect_bytes, unprotect_bytes
from bilicleaner.types import new_task, task_is_finished, touch_task

IS_WINDOWS = sys.platform == "win32"


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(str(tmp_path / "data"))


# ---------------------------------------------------------------- 数据目录与加密


def test_store_creates_directories(store: Store):
    assert os.path.isdir(store.data_dir)
    assert os.path.isdir(store.logs_dir)
    assert os.path.isdir(store.backups_dir)
    assert store.data_dir == os.path.abspath(store.data_dir)
    assert store.settings_path.startswith(store.data_dir)
    assert store.tasks_path.startswith(store.data_dir)


def test_default_data_dir_env_override(tmp_path, monkeypatch):
    custom = tmp_path / "custom-data"
    monkeypatch.setenv("BILICLEANER_DATA", str(custom))
    assert default_data_dir() == os.path.abspath(str(custom))
    custom_store = Store()
    assert custom_store.data_dir == os.path.abspath(str(custom))
    assert os.path.isdir(custom_store.data_dir)


def test_protect_unprotect_roundtrip():
    raw = "SESSDATA=中文测试值; bili_jct=abc123".encode("utf-8")
    blob = protect_bytes(raw)
    assert isinstance(blob, bytes)
    assert raw not in blob  # 不能是明文
    assert unprotect_bytes(blob) == raw


def test_unprotect_unknown_blob_raises():
    with pytest.raises(ValueError):
        unprotect_bytes(b"definitely-not-a-session")


@pytest.mark.skipif(not IS_WINDOWS, reason="DPAPI 加解密只在 Windows 上有意义")
def test_session_dpapi_roundtrip(tmp_path):
    store = Store(str(tmp_path / "data"))
    assert store.load_session() is None
    assert store.has_session() is False

    cookies = {"SESSDATA": "secret-sessdata-value", "bili_jct": "csrf-token", "DedeUserID": "12345"}
    user = {"mid": 12345, "name": "测试账号", "face": "https://example.com/a.jpg", "vip": False}
    store.save_session(cookies, user)
    assert os.path.exists(store.session_path)

    with open(store.session_path, "rb") as f:
        blob = f.read()
    # 优先 DPAPI；极端情况下 store 会退化成带标记的 base64
    assert blob.startswith(b"dpapi:v1:") or blob.startswith(b"plain:v1:")
    assert b"secret-sessdata-value" not in blob

    assert store.has_session() is True
    loaded = store.load_session()
    assert loaded is not None
    assert loaded["cookies"] == cookies
    assert loaded["user"] == user
    assert loaded["saved_at"] > 0

    store.clear_session()
    assert store.load_session() is None
    assert store.has_session() is False
    # 重复清理不报错
    store.clear_session()


def test_load_session_tolerates_corrupt_file(store: Store):
    with open(store.session_path, "wb") as f:
        f.write(b"garbage-bytes")
    assert store.load_session() is None


# ---------------------------------------------------------------- 设置


def test_settings_defaults(store: Store):
    settings = store.load_settings()
    assert settings == DEFAULT_SETTINGS
    assert settings is not DEFAULT_SETTINGS  # 不能把可变默认值直接交出去
    # 修改返回值不应该影响下一次读取
    settings["delay_seconds"] = 99
    assert store.load_settings()["delay_seconds"] == DEFAULT_SETTINGS["delay_seconds"]


def test_settings_partial_merge_persisted(tmp_path):
    data_dir = str(tmp_path / "data")
    store = Store(data_dir)
    merged = store.save_settings(
        {"delay_seconds": 1.25, "llm": {"enabled": True, "model": "deepseek-chat"}}
    )
    assert merged["delay_seconds"] == 1.25
    assert merged["llm"]["enabled"] is True
    assert merged["llm"]["model"] == "deepseek-chat"
    # 嵌套对象是"合并"不是"替换"，其余默认值保留
    assert merged["llm"]["base_url"] == DEFAULT_SETTINGS["llm"]["base_url"]
    assert merged["llm"]["api_key"] == DEFAULT_SETTINGS["llm"]["api_key"]
    assert merged["batch_size"] == DEFAULT_SETTINGS["batch_size"]

    # 新 Store 实例（同目录）读回来一致
    again = Store(data_dir).load_settings()
    assert again == merged


def test_settings_corrupt_file_falls_back_to_default(store: Store):
    with open(store.settings_path, "w", encoding="utf-8") as f:
        f.write("{ this is not valid json")
    assert store.load_settings() == DEFAULT_SETTINGS
    # 坏文件仍然可以正常覆盖写入
    store.save_settings({"protect_mutual": False})
    assert store.load_settings()["protect_mutual"] is False


# ---------------------------------------------------------------- 任务


def test_task_read_write_roundtrip(store: Store):
    task1 = new_task("unfollow", {"mids": [1, 2, 3]}, total=3)
    task2 = new_task("toview_clear", {}, total=120)

    assert task1["id"] != task2["id"]
    assert len(task1["id"]) == 12
    assert task1["status"] == "queued"
    assert task1["created_at"] == task1["updated_at"]
    assert task1["params"] == {"mids": [1, 2, 3]}

    store.save_tasks([task1, task2])
    loaded = store.load_tasks()
    assert loaded == [task1, task2]
    assert isinstance(loaded, list)


def test_task_touch_and_finished(store: Store):
    task = new_task("dynamic_delete", {"ids": ["1"]}, total=1)
    before = task["updated_at"]
    touch_task(task, status="running", done=1)
    assert task["status"] == "running"
    assert task["done"] == 1
    assert task["updated_at"] >= before
    assert task_is_finished(task) is False

    for status in ("done", "failed", "stopped"):
        task["status"] = status
        assert task_is_finished(task) is True


def test_touch_task_recovers_invalid_status():
    task = new_task("unfollow")
    touch_task(task, status="bogus-status")
    assert task["status"] == "running"


def test_load_tasks_corrupt_file(store: Store):
    with open(store.tasks_path, "w", encoding="utf-8") as f:
        f.write("[not valid json")
    assert store.load_tasks() == []


# ---------------------------------------------------------------- 日志


def test_logs_append_and_filter(store: Store):
    assert store.tail_logs() == []

    store.append_log("info", "任务开始", "task-a")
    store.append_log("warn", "触发风控，冷却中", "task-a")
    store.append_log("error", "删除失败", None)
    store.append_log("info", "任务完成", "task-b")

    logs = store.tail_logs()
    assert len(logs) == 4
    assert [entry["level"] for entry in logs] == ["info", "warn", "error", "info"]
    assert logs[0]["msg"] == "任务开始"
    assert logs[0]["ts"]  # ISO 时间字符串非空
    assert logs[2]["task_id"] is None

    only_a = store.tail_logs(task_id="task-a")
    assert len(only_a) == 2
    assert all(entry["task_id"] == "task-a" for entry in only_a)

    assert store.tail_logs(task_id="not-exist") == []


def test_logs_limit_and_corrupt_lines(store: Store):
    for i in range(20):
        store.append_log("info", f"第 {i} 条", "task-x")

    tail = store.tail_logs(limit=5)
    assert len(tail) == 5
    assert tail[-1]["msg"] == "第 19 条"
    assert [entry["msg"] for entry in tail] == [f"第 {i} 条" for i in range(15, 20)]

    # 手工插入一行坏 JSON，读取时应跳过而不是炸掉
    with open(store.log_path, "a", encoding="utf-8") as f:
        f.write("this is not json\n")
    logs = store.tail_logs(limit=3)
    assert len(logs) == 3
    assert all("msg" in entry for entry in logs)


def test_logs_are_utf8_jsonl(store: Store):
    store.append_log("info", "中文日志 · 带标点", "t1")
    with open(store.log_path, "r", encoding="utf-8") as f:
        line = f.readline()
    entry = json.loads(line)
    assert entry["msg"] == "中文日志 · 带标点"


# ---------------------------------------------------------------- 备份目录


def test_backup_dir_creation(store: Store):
    before = store.list_backups()
    assert before == []

    path = store.backup_dir_for(12345)
    assert os.path.isdir(path)
    assert os.path.abspath(path).startswith(os.path.abspath(store.backups_dir))
    assert "12345" in path

    anonymous = store.backup_dir_for(None)
    assert os.path.isdir(anonymous)
    assert "anonymous" in anonymous

    # 同一账号同一秒内重复调用应该复用目录
    assert store.backup_dir_for(12345) == path


def test_list_backups(store: Store):
    account_dir = store.backup_dir_for(12345)
    with open(os.path.join(account_dir, "follows.csv"), "w", encoding="utf-8") as f:
        f.write("mid,name\n1,测试\n")
    with open(os.path.join(account_dir, "follows.jsonl"), "w", encoding="utf-8") as f:
        f.write('{"mid": 1}\n')
    # 无关文件不应该出现在 files 里
    with open(os.path.join(account_dir, "notes.txt"), "w", encoding="utf-8") as f:
        f.write("ignore me")

    other_dir = store.backup_dir_for(None)
    with open(os.path.join(other_dir, "toview.jsonl"), "w", encoding="utf-8") as f:
        f.write('{"aid": 1}\n')

    backups = store.list_backups()
    assert len(backups) == 2
    accounts = [item["account"] for item in backups]
    assert "12345" in accounts
    assert "anonymous" in accounts

    for item in backups:
        assert set(item) >= {"path", "account", "stamp", "files"}
        assert os.path.isdir(item["path"])

    first = next(item for item in backups if item["account"] == "12345")
    assert sorted(first["files"]) == ["follows.csv", "follows.jsonl"]
    assert "notes.txt" not in first["files"]
