"""删除前的强制备份：CSV（人看）+ JSONL（完整字段）。

依据 docs/adr/0003-backup.md：备份不是"可恢复"承诺，而是"删了什么、
以后想重新加回来时有数据可查"。因此：

- 每次导出固定写两个文件：``<type>.csv``（utf-8-sig，Excel 直接可读）和
  ``<type>.jsonl``（一行一个完整 dict）；另外写 ``meta.json`` 记录任务号。
- 写文件先落 ``.tmp`` 再 ``os.replace``，避免半个文件。
- 任何失败都抛 :class:`~bilicleaner.errors.BackupError`，调用方必须让任务失败，
  不允许跳过备份继续删。
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import time

from ..errors import BackupError

# 各任务类型给人看的 CSV 列：(表头, item 里的取值路径)
_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "unfollow": [
        ("UID", "mid"),
        ("昵称", "name"),
        ("签名", "sign"),
        ("官方认证", "official"),
        ("互关", "mutual"),
        ("分组", "groups"),
        ("关注时间", "mtime"),
    ],
    "dynamic_delete": [
        ("动态ID", "id"),
        ("类型", "type_label"),
        ("发布时间", "pub_ts"),
        ("正文", "text"),
        ("视频标题", "video.title"),
        ("视频BV号", "video.bvid"),
        ("原作者", "orig_author"),
        ("链接", "link"),
    ],
    "favorite_clear": [
        ("收藏ID", "id"),
        ("类型", "type"),
        ("标题", "title"),
        ("UP主", "upper"),
        ("BV号", "bvid"),
        ("链接", "link"),
        ("收藏时间", "fav_time"),
        ("失效", "invalid"),
    ],
    "favorite_clean_invalid": [
        ("收藏ID", "id"),
        ("类型", "type"),
        ("标题", "title"),
        ("UP主", "upper"),
        ("BV号", "bvid"),
        ("链接", "link"),
        ("收藏时间", "fav_time"),
        ("失效", "invalid"),
    ],
    "toview_clear": [
        ("aid", "aid"),
        ("标题", "title"),
        ("UP主", "upper"),
        ("时长(秒)", "duration"),
        ("进度(秒)", "progress"),
        ("添加时间", "add_ts"),
        ("BV号", "bvid"),
        ("链接", "link"),
    ],
    "classify_llm": [
        ("UID", "mid"),
        ("昵称", "name"),
        ("分类", "category"),
        ("分类来源", "category_source"),
        ("签名", "sign"),
        ("官方认证", "official"),
    ],
}

# 这些字段是 Unix 秒，CSV 里转成人能读的时间
_TIME_KEYS = {"mtime", "fav_time", "add_ts", "pub_ts", "created_at"}

_BILI_VIDEO_URL = "https://www.bilibili.com/video/"
_BILI_DYNAMIC_URL = "https://t.bilibili.com/"


def _fmt_ts(value: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))
    except (OSError, ValueError, OverflowError):
        return str(value)


def _lookup(item: dict, path: str) -> object:
    """支持 ``video.title`` 这样的点号路径。"""
    current: object = item
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _item_link(item: dict) -> str:
    """按对象类型拼一个可点的链接（没有就留空）。"""
    if item.get("bvid"):
        return f"{_BILI_VIDEO_URL}{item['bvid']}"
    if item.get("aid"):
        return f"{_BILI_VIDEO_URL}av{item['aid']}"
    if item.get("id") and item.get("type") == "DYNAMIC_TYPE_FORWARD":
        return f"{_BILI_DYNAMIC_URL}{item['id']}"
    if item.get("id") and isinstance(item.get("id"), str) and len(item["id"]) >= 15:
        return f"{_BILI_DYNAMIC_URL}{item['id']}"
    return ""


def _cell(item: dict, path: str) -> str:
    if path == "link":
        return _item_link(item)
    value = _lookup(item, path)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, tuple)):
        return "、".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if path in _TIME_KEYS:
        return _fmt_ts(value)  # type: ignore[arg-type]
    return str(value)


def _columns_for(task_type: str, items: list[dict]) -> list[tuple[str, str]]:
    columns = _COLUMNS.get(task_type)
    if columns:
        return columns
    # 未知任务类型：退化成一列一个字段，保证仍然可读
    keys: list[str] = []
    for item in items[:50]:
        for key in item:
            if key not in keys:
                keys.append(key)
    return [(key, key) for key in keys]


class BackupService:
    """把待处理对象导出成 CSV + JSONL。"""

    def __init__(self, store):
        self.store = store

    # ------------------------------------------------------------ 导出

    def export(
        self,
        task_type: str,
        items: list[dict],
        user: dict | None,
        *,
        task_id: str | None = None,
    ) -> dict:
        """导出一次备份。

        返回 ``{"csv_path": str, "jsonl_path": str, "count": int}``；
        失败抛 :class:`BackupError`。
        """
        task_type = str(task_type or "task")
        item_list = [it for it in (items or []) if isinstance(it, dict)]
        user = user if isinstance(user, dict) else {}
        directory = ""
        try:
            directory = self.store.backup_dir_for(user.get("mid"))
            csv_path = os.path.join(directory, f"{task_type}.csv")
            jsonl_path = os.path.join(directory, f"{task_type}.jsonl")
            self._write_csv(csv_path, task_type, item_list)
            self._write_jsonl(jsonl_path, item_list)
            created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._write_meta(
                os.path.join(directory, "meta.json"),
                {
                    "task_id": task_id,
                    "type": task_type,
                    "count": len(item_list),
                    "created_at": created_at,
                    "user": {"mid": user.get("mid"), "name": user.get("name") or ""},
                    "files": [os.path.basename(csv_path), os.path.basename(jsonl_path)],
                },
            )
            self._prune(user.get("mid"))
            return {"csv_path": csv_path, "jsonl_path": jsonl_path, "count": len(item_list)}
        except BackupError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转成 BackupError
            raise BackupError(
                f"备份失败（{task_type}）：{exc}",
                code="BACKUP_FAILED",
                path=directory,
            ) from exc

    # ------------------------------------------------------------ 查询

    def list_backups(self) -> list[dict]:
        """列出所有备份目录，按时间倒序（契约里的 BackupFile 结构）。"""
        out: list[dict] = []
        for entry in self.store.list_backups():
            directory = entry.get("path") or ""
            meta = self._read_meta(os.path.join(directory, "meta.json"))
            created_at = meta.get("created_at") or self._stamp_to_iso(entry.get("stamp", ""))
            out.append(
                {
                    "path": directory,
                    "task_id": meta.get("task_id"),
                    "count": int(meta.get("count") or 0),
                    "created_at": created_at,
                }
            )
        out.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
        return out

    # ------------------------------------------------------------ 内部

    def _write_csv(self, path: str, task_type: str, items: list[dict]) -> None:
        columns = _columns_for(task_type, items)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([header for header, _ in columns])
            for item in items:
                writer.writerow([_cell(item, key) for _, key in columns])
        os.replace(tmp, path)

    def _write_jsonl(self, path: str, items: list[dict]) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        os.replace(tmp, path)

    def _write_meta(self, path: str, meta: dict) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    @staticmethod
    def _read_meta(path: str) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _stamp_to_iso(stamp: str) -> str:
        try:
            return time.strftime("%Y-%m-%dT%H:%M:%S", time.strptime(stamp, "%Y%m%d-%H%M%S"))
        except (TypeError, ValueError):
            return ""

    def _prune(self, user_mid: object) -> None:
        """设置里 backup_keep>0 时只保留最近 N 次备份。"""
        try:
            keep = int(self.store.load_settings().get("backup_keep") or 0)
        except (OSError, ValueError, TypeError):
            keep = 0
        if keep <= 0:
            return
        account = str(user_mid or "anonymous")
        account_dir = os.path.join(self.store.backups_dir, account)
        backups_root = os.path.abspath(self.store.backups_dir)
        try:
            stamps = sorted(
                (d for d in os.listdir(account_dir) if os.path.isdir(os.path.join(account_dir, d))),
                reverse=True,
            )
        except OSError:
            return
        for stamp in stamps[keep:]:
            target = os.path.abspath(os.path.join(account_dir, stamp))
            # 双保险：只删 backups 目录下的子目录
            if not target.startswith(backups_root + os.sep):
                continue
            shutil.rmtree(target, ignore_errors=True)
