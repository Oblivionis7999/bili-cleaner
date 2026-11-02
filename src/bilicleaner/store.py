"""本地存储：数据目录、登录态加密、设置、任务与日志文件。"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from typing import Any

from .const import APP_NAME, DEFAULT_SETTINGS, merge_settings

# ---------------------------------------------------------------- Windows DPAPI

_IS_WINDOWS = sys.platform == "win32"
_PLAIN_PREFIX = b"plain:v1:"
_DPAPI_ENTROPY = b"BiliCleaner::session::v1"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _dpapi_call(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    blob_in = _blob(data)
    blob_entropy = _blob(_DPAPI_ENTROPY)
    blob_out = _DATA_BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, ctypes.byref(blob_entropy), None, None, 0x1,
            ctypes.byref(blob_out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def protect_bytes(data: bytes) -> bytes:
    """用 DPAPI 加密；非 Windows 平台退化为带标记的 base64（仅开发用）。"""
    if _IS_WINDOWS:
        try:
            return b"dpapi:v1:" + _dpapi_call(data, protect=True)
        except OSError:
            pass
    return _PLAIN_PREFIX + base64.b64encode(data)


def unprotect_bytes(blob: bytes) -> bytes:
    if blob.startswith(b"dpapi:v1:"):
        return _dpapi_call(blob[len(b"dpapi:v1:"):], protect=False)
    if blob.startswith(_PLAIN_PREFIX):
        return base64.b64decode(blob[len(_PLAIN_PREFIX):])
    raise ValueError("unknown session blob format")


# ---------------------------------------------------------------- Store

def default_data_dir() -> str:
    override = os.environ.get("BILICLEANER_DATA")
    if override:
        return os.path.abspath(override)
    if _IS_WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_NAME)
    return os.path.join(os.path.expanduser("~"), ".bilicleaner")


class Store:
    """所有可变状态都保存在这个目录里，方便备份和排查。"""

    def __init__(self, data_dir: str | None = None):
        self.data_dir = os.path.abspath(data_dir or default_data_dir())
        self.logs_dir = os.path.join(self.data_dir, "logs")
        self.backups_dir = os.path.join(self.data_dir, "backups")
        for d in (self.data_dir, self.logs_dir, self.backups_dir):
            os.makedirs(d, exist_ok=True)
        self.session_path = os.path.join(self.data_dir, "session.dat")
        self.settings_path = os.path.join(self.data_dir, "settings.json")
        self.tasks_path = os.path.join(self.data_dir, "tasks.json")
        self.log_path = os.path.join(self.logs_dir, "operations.jsonl")

    # ---------------- 基础读写

    def _read_json(self, path: str, default: Any) -> Any:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def _write_json(self, path: str, obj: Any) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    # ---------------- 登录态

    def load_session(self) -> dict | None:
        try:
            with open(self.session_path, "rb") as f:
                raw = f.read()
            payload = json.loads(unprotect_bytes(raw).decode("utf-8"))
            if payload.get("cookies"):
                return payload
        except (OSError, ValueError):
            return None
        return None

    def save_session(self, cookies: dict, user: dict | None = None) -> None:
        payload = {"cookies": cookies, "user": user or {}, "saved_at": int(time.time())}
        blob = protect_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        tmp = self.session_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, self.session_path)

    def clear_session(self) -> None:
        try:
            os.remove(self.session_path)
        except OSError:
            pass

    def has_session(self) -> bool:
        return self.load_session() is not None

    # ---------------- 设置

    def load_settings(self) -> dict:
        settings = self._read_json(self.settings_path, {})
        return merge_settings(DEFAULT_SETTINGS, settings if isinstance(settings, dict) else {})

    def save_settings(self, patch: dict) -> dict:
        merged = merge_settings(self.load_settings(), patch or {})
        self._write_json(self.settings_path, merged)
        return merged

    # ---------------- 任务

    def load_tasks(self) -> list[dict]:
        tasks = self._read_json(self.tasks_path, [])
        return tasks if isinstance(tasks, list) else []

    def save_tasks(self, tasks: list[dict]) -> None:
        self._write_json(self.tasks_path, tasks)

    # ---------------- 日志

    def append_log(self, level: str, msg: str, task_id: str | None = None) -> None:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "level": level,
            "msg": msg,
            "task_id": task_id,
        }
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def tail_logs(self, limit: int = 200, task_id: str | None = None) -> list[dict]:
        if not os.path.exists(self.log_path):
            return []
        lines: list[str] = []
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    lines.append(line)
                    if len(lines) > limit * 4:
                        lines = lines[-limit * 4:]
        except OSError:
            return []
        out: list[dict] = []
        for line in lines[-limit * 4:]:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if task_id and entry.get("task_id") != task_id:
                continue
            out.append(entry)
        return out[-limit:]

    # ---------------- 备份

    def backup_dir_for(self, user_mid: int | str | None) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        account = str(user_mid or "anonymous")
        path = os.path.join(self.backups_dir, account, stamp)
        os.makedirs(path, exist_ok=True)
        return path

    def list_backups(self) -> list[dict]:
        out: list[dict] = []
        if not os.path.isdir(self.backups_dir):
            return out
        for account in sorted(os.listdir(self.backups_dir)):
            acc_dir = os.path.join(self.backups_dir, account)
            if not os.path.isdir(acc_dir):
                continue
            for stamp in sorted(os.listdir(acc_dir), reverse=True):
                task_dir = os.path.join(acc_dir, stamp)
                if not os.path.isdir(task_dir):
                    continue
                files = [f for f in os.listdir(task_dir) if f.endswith((".csv", ".jsonl", ".json"))]
                out.append({"path": task_dir, "account": account, "stamp": stamp, "files": files})
        return out
