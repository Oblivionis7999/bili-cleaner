"""任务引擎：单个写者线程 + 队列 + 暂停/继续/停止 + 冷却 + 断点续跑。

为什么是单写者：B 站没有批量写接口，并发写请求最容易触发风控（ADR 0002）。
因此所有写任务都进同一个队列，由唯一的工作线程串行执行；读接口不受影响。

断点续跑靠两层记录：
- ``tasks.json``：任务本身的状态与进度（原子替换写盘）；
- ``task_ops/<task_id>.jsonl``：每个对象一行操作结果。重启后从操作日志里
  取"已成功"的对象集合，跳过它们继续跑，所以中途崩溃不会重复删除。

线程安全约定：所有对 ``_tasks`` 的读写都在 ``_lock`` 内；``_persist_locked``
只在持锁时调用。执行器通过 :meth:`update_progress` / :meth:`append_op` 等方法
回报进度，不直接改任务 dict。
"""

from __future__ import annotations

import copy
import json
import os
import queue
import threading
import time
from typing import Callable

from ..const import DESTRUCTIVE_CLEAR_TASKS, TASK_TYPES
from ..errors import BiliCleanerError, TaskError
from ..types import new_task, now_iso, task_is_finished, touch_task


class TaskStopped(BiliCleanerError):
    """任务被停止（用户点了停止，或连续风控后决定放弃）。"""

    code = "TASK_STOPPED"


class TaskInterrupted(BiliCleanerError):
    """程序正在退出，任务先放回队列，下次启动自动续跑。"""

    code = "TASK_INTERRUPTED"


class TaskFailed(BiliCleanerError):
    """任务失败（备份失败、参数错误等不该重试的情况）。"""

    code = "TASK_FAILED"


def _iso_in(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + max(0.0, seconds)))


def _as_mid_list(value: object) -> list[int]:
    result: list[int] = []
    if not isinstance(value, (list, tuple)):
        return result
    for raw in value:
        try:
            mid = int(raw)
        except (TypeError, ValueError):
            continue
        if mid > 0 and mid not in result:
            result.append(mid)
    return result


def _as_str_list(value: object) -> list[str]:
    result: list[str] = []
    if not isinstance(value, (list, tuple)):
        return result
    for raw in value:
        text = str(raw).strip()
        if text and text not in result:
            result.append(text)
    return result


def _valid_resource_key(key: str) -> bool:
    """收藏条目 key 必须是 ``id:type`` 两段数字。"""
    left, sep, right = str(key).partition(":")
    return bool(sep and left.isdigit() and right.isdigit())


class TaskManager:
    """管理任务的生命周期与持久化。"""

    def __init__(
        self,
        store,
        client_factory: Callable[[], object],
        limiter_factory: Callable[[], object],
        backup,
        classify,
        logger: Callable[..., None],
    ):
        self.store = store
        self._client_factory = client_factory
        self._limiter_factory = limiter_factory
        self.backup = backup
        self.classify = classify
        self.logger = logger or (lambda *a, **k: None)

        self._lock = threading.RLock()
        self._tasks: dict[str, dict] = {}
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._active_tid: str | None = None
        self._listeners: list[Callable[[dict], None]] = []
        self._limiter: object | None = None
        # 已入队但还没执行完的任务数（多次入队去重），wait_idle 用
        self._pending = 0
        self._enqueued: set[str] = set()

        self.ops_dir = os.path.join(self.store.data_dir, "task_ops")
        try:
            os.makedirs(self.ops_dir, exist_ok=True)
        except OSError:
            pass

        self._load_persisted()

    # ============================================================ 生命周期

    def start(self) -> None:
        """加载持久化任务、恢复断点，然后启动唯一的工作线程。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._shutdown.clear()
            self._recover_locked()
            self._thread = threading.Thread(
                target=self._worker, name="bilicleaner-task-writer", daemon=True
            )
            self._thread.start()

    def shutdown(self, timeout: float = 5.0) -> None:
        """请求退出：正在跑的任务会尽快中断，下次启动自动续跑。"""
        self._shutdown.set()
        thread = self._thread
        try:
            self._queue.put(None, timeout=1.0)
        except queue.Full:
            pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                self._log("warn", "任务线程未能及时退出（可能卡在网络请求）")

    @property
    def active_task_id(self) -> str | None:
        with self._lock:
            return self._active_tid

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """等队列清空且没有任务在跑（测试/退出前用）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                idle = self._active_tid is None and self._queue.empty() and self._pending <= 0
            if idle:
                return True
            time.sleep(0.05)
        return False

    def add_listener(self, listener: Callable[[dict], None]) -> None:
        """注册进度回调（参数是任务快照）。"""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def log(self, level: str, msg: str, task_id: str | None = None) -> None:
        """写一条操作日志（执行器也可以调用）。"""
        self._log(level, msg, task_id)

    # ============================================================ 创建与校验

    def create(self, type_: str, params: dict, confirm_phrase: str | None = None) -> dict:
        """创建任务并入队。"""
        self._validate_create(type_, params, confirm_phrase)
        clean_params = self._clean_params(type_, params or {})
        if type_ in DESTRUCTIVE_CLEAR_TASKS:
            # 存下实际输入的确认词，执行器会在发写请求前再核对一次
            clean_params["confirm_phrase"] = str(confirm_phrase or "").strip()
        settings = self.store.load_settings()
        total = self._initial_total(type_, clean_params)
        with self._lock:
            task = new_task(type_, clean_params, total=total)
            task["message"] = "排队中"
            self._tasks[task["id"]] = task
            self._persist_locked()
            self._enqueue_locked(task["id"])
        self._notify(task)
        self._log("info", f"创建任务 {task['id']}（{type_}），共 {total} 条", task["id"])
        return copy.deepcopy(task)

    def _validate_create(self, type_: str, params: object, confirm_phrase: str | None) -> None:
        if type_ not in TASK_TYPES:
            raise TaskError(f"未知任务类型：{type_}", code="BAD_TASK_TYPE")
        params = params if isinstance(params, dict) else {}
        settings = self.store.load_settings()

        # favorite_clear 传了 ids 就是"只删这批"，按契约补充约定不需要确认词
        needs_confirm = type_ in DESTRUCTIVE_CLEAR_TASKS
        if type_ == "favorite_clear" and _as_str_list(params.get("ids")):
            needs_confirm = False
        if needs_confirm:
            expected = str(settings.get("confirm_phrase") or "清空")
            actual = str(confirm_phrase or "").strip()
            if actual != expected:
                raise TaskError(
                    f"清空类操作需要输入确认词“{expected}”", code="CONFIRM_REQUIRED"
                )

        if type_ == "unfollow":
            if not params.get("ack_risk"):
                raise TaskError(
                    "取关前需要勾选“我已了解取关后无法恢复原关注顺序”", code="ACK_REQUIRED"
                )
            if not _as_mid_list(params.get("mids")):
                raise TaskError("缺少要取关的 mids 列表", code="BAD_PARAMS")
        elif type_ == "dynamic_delete":
            if not _as_str_list(params.get("ids")):
                raise TaskError("缺少要删除的动态 ids 列表", code="BAD_PARAMS")
        elif type_ == "favorite_clear":
            if not self._as_folder_id(params.get("folder_id")):
                raise TaskError("缺少收藏夹 folder_id", code="BAD_PARAMS")
            # ids 为空 = 清空整个收藏夹（已在上面校验确认词）；
            # ids 非空 = 只删勾选的这批，必须勾选风险确认
            ids = _as_str_list(params.get("ids"))
            if ids:
                bad = [key for key in ids if not _valid_resource_key(key)]
                if bad:
                    raise TaskError(
                        f"收藏条目参数格式错误（应为 id:type）：{bad[0]}", code="BAD_PARAMS"
                    )
                if not params.get("ack_risk"):
                    raise TaskError(
                        "批量取消收藏前需要勾选风险确认（ack_risk）", code="ACK_REQUIRED"
                    )
        elif type_ == "favorite_clean_invalid":
            if not self._as_folder_id(params.get("folder_id")):
                raise TaskError("缺少收藏夹 folder_id", code="BAD_PARAMS")
        elif type_ == "classify_llm":
            if not _as_mid_list(params.get("mids")):
                raise TaskError("缺少要分类的 mids 列表", code="BAD_PARAMS")

    @staticmethod
    def _as_folder_id(value: object) -> int:
        try:
            folder_id = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0
        return folder_id if folder_id > 0 else 0

    def _clean_params(self, type_: str, params: dict) -> dict:
        if type_ in ("unfollow", "classify_llm"):
            clean = dict(params)
            clean["mids"] = _as_mid_list(params.get("mids"))
            return clean
        if type_ == "dynamic_delete":
            clean = dict(params)
            clean["ids"] = _as_str_list(params.get("ids"))
            return clean
        if type_ in ("favorite_clear", "favorite_clean_invalid"):
            clean = dict(params)
            clean["folder_id"] = self._as_folder_id(params.get("folder_id"))
            if type_ == "favorite_clear":
                clean["ids"] = _as_str_list(params.get("ids"))
            return clean
        return dict(params)

    @staticmethod
    def _needs_confirm(type_: str, params: dict) -> bool:
        """清空整个收藏夹/清理失效/清空稍后再看需要确认词。"""
        if type_ not in DESTRUCTIVE_CLEAR_TASKS:
            return False
        if type_ == "favorite_clear" and _as_str_list(params.get("ids")):
            return False
        return True

    @staticmethod
    def _initial_total(type_: str, params: dict) -> int:
        if type_ in ("unfollow", "classify_llm"):
            return len(params.get("mids") or [])
        if type_ == "dynamic_delete":
            return len(params.get("ids") or [])
        if type_ == "favorite_clear":
            return len(params.get("ids") or [])
        return 0

    # ============================================================ 查询

    def list(self) -> list[dict]:
        """任务列表（新的在前）。"""
        with self._lock:
            return [copy.deepcopy(task) for task in reversed(list(self._tasks.values()))]

    def get(self, tid: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(str(tid))
            return copy.deepcopy(task) if task else None

    def _get_locked(self, tid: str) -> dict:
        task = self._tasks.get(str(tid))
        if task is None:
            raise TaskError(f"任务不存在：{tid}", code="NOT_FOUND")
        return task

    # ============================================================ 控制

    def pause(self, tid: str) -> dict:
        with self._lock:
            task = self._get_locked(tid)
            status = task.get("status")
            if status == "paused":
                return copy.deepcopy(task)
            if task_is_finished(task):
                raise TaskError("任务已结束，无法暂停", code="TASK_FINISHED")
            touch_task(task, status="paused", message="已暂停", cooldown_until=None)
            self._persist_locked()
            snapshot = copy.deepcopy(task)
            active = self._active_tid == task["id"]
        self._log("info", f"暂停任务 {task['id']}", task["id"])
        self._notify(task)
        return snapshot

    def resume(self, tid: str) -> dict:
        with self._lock:
            task = self._get_locked(tid)
            status = task.get("status")
            if status != "paused":
                raise TaskError("任务当前不是暂停状态", code="TASK_NOT_PAUSED")
            active = self._active_tid == task["id"]
            if active:
                touch_task(task, status="running", message="继续执行")
            else:
                touch_task(task, status="queued", message="等待继续", cooldown_until=None)
                self._enqueue_locked(task["id"])
            self._persist_locked()
            snapshot = copy.deepcopy(task)
        self._log("info", f"继续任务 {task['id']}", task["id"])
        self._notify(task)
        return snapshot

    def stop(self, tid: str) -> dict:
        with self._lock:
            task = self._get_locked(tid)
            status = task.get("status")
            if task_is_finished(task):
                return copy.deepcopy(task)
            touch_task(task, status="stopped", message="已停止", cooldown_until=None)
            self._persist_locked()
            snapshot = copy.deepcopy(task)
        self._log("info", f"停止任务 {task['id']}", task["id"])
        self._notify(task)
        return snapshot

    # ============================================================ 执行器回调

    def update_progress(
        self,
        task: dict,
        done: int | None = None,
        failed: int | None = None,
        message: str | None = None,
    ) -> None:
        """执行器回报进度：更新计数 + 落盘（不改变状态）。"""
        with self._lock:
            if done is not None:
                task["done"] = max(0, int(done))
            if failed is not None:
                task["failed"] = max(0, int(failed))
            if message is not None:
                task["message"] = str(message)
            task["updated_at"] = now_iso()
            self._persist_locked()
        self._notify(task)

    def set_total(self, task: dict, total: int, message: str | None = None) -> None:
        """执行器拿到真实对象数后校正 total。"""
        with self._lock:
            task["total"] = max(0, int(total))
            if message is not None:
                task["message"] = str(message)
            task["updated_at"] = now_iso()
            self._persist_locked()
        self._notify(task)

    def cooldown(self, task: dict, seconds: float, message: str | None = None) -> None:
        """进入风控冷却状态。"""
        with self._lock:
            if task.get("status") != "running":
                return
            touch_task(
                task,
                status="cooldown",
                cooldown_until=_iso_in(seconds),
                message=message or f"触发风控，冷却 {int(seconds)} 秒",
            )
            self._persist_locked()
        self._log(
            "warn",
            f"任务 {task['id']} 触发风控，冷却 {int(seconds)} 秒",
            task["id"],
        )
        self._notify(task)

    def end_cooldown(self, task: dict, message: str | None = None) -> None:
        """冷却结束回到运行状态（若期间被暂停则保持暂停）。"""
        with self._lock:
            if task.get("status") != "cooldown":
                return
            touch_task(
                task,
                status="running",
                cooldown_until=None,
                message=message or "冷却结束，继续执行",
            )
            self._persist_locked()
        self._notify(task)

    def backup_export(self, task: dict, items: list[dict], user: dict | None = None) -> dict:
        """写强制备份，并把备份目录记到 task.backup_path。"""
        result = self.backup.export(
            task.get("type") or "task", items, user, task_id=task.get("id")
        )
        directory = os.path.dirname(str(result.get("csv_path") or ""))
        with self._lock:
            task["backup_path"] = directory or None
            task["updated_at"] = now_iso()
            self._persist_locked()
        self._log(
            "info",
            f"任务 {task['id']} 备份 {result.get('count', 0)} 条到 {directory}",
            task["id"],
        )
        self._notify(task)
        return result

    # ------------------------------------------------------------ 操作日志

    def _ops_path(self, task: dict) -> str:
        return os.path.join(self.ops_dir, f"{task['id']}.jsonl")

    def append_op(
        self,
        task: dict,
        key: str,
        ok: bool,
        code: int = 0,
        msg: str = "",
    ) -> None:
        """对象级操作结果写一行 JSONL（断点续跑的依据）。"""
        record = {
            "key": str(key),
            "ok": bool(ok),
            "code": int(code or 0),
            "msg": str(msg or "")[:200],
            "ts": now_iso(),
        }
        try:
            with open(self._ops_path(task), "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
        except OSError as exc:
            self._log("warn", f"操作日志写入失败：{exc}", task["id"])

    def succeeded_keys(self, task: dict) -> set[str]:
        """从操作日志恢复"已经成功"的对象集合。"""
        keys: set[str] = set()
        path = self._ops_path(task)
        if not os.path.exists(path):
            return keys
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("ok"):
                        keys.add(str(record.get("key")))
        except OSError:
            return keys
        return keys

    # ------------------------------------------------------------ 暂停/停止协作

    def checkpoint(self, task: dict) -> None:
        """执行器在每个对象前调用：处理暂停阻塞、停止与程序退出。"""
        while True:
            with self._lock:
                status = task.get("status")
                interrupted = self._shutdown.is_set()
            if interrupted:
                raise TaskInterrupted("程序退出，任务将在下次启动时续跑")
            if status == "stopped":
                raise TaskStopped("任务已停止")
            if status != "paused":
                return
            time.sleep(0.1)

    def sleep_interruptible(self, task: dict, seconds: float) -> None:
        """可分片中断的等待（冷却/批次休息用）。"""
        remaining = max(0.0, float(seconds))
        while remaining > 0:
            self.checkpoint(task)
            step = min(remaining, 0.2)
            time.sleep(step)
            remaining -= step
        self.checkpoint(task)

    def current_limiter(self) -> object | None:
        return self._limiter

    def current_settings(self) -> dict:
        return self.store.load_settings()

    def apply_settings(self, settings: dict) -> None:
        """设置变更后同步给当前 limiter（不重置计数）。"""
        limiter = self._limiter
        if limiter is None:
            return
        updater = getattr(limiter, "update_settings", None)
        if callable(updater):
            try:
                updater(settings)
            except Exception as exc:  # noqa: BLE001 - 设置更新失败不该崩
                self._log("warn", f"同步限速设置失败：{exc}")

    # ============================================================ 内部：线程

    def _worker(self) -> None:
        while not self._shutdown.is_set():
            try:
                tid = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if tid is None:
                    break
                self._run_one(tid)
            finally:
                with self._lock:
                    if tid is not None:
                        self._enqueued.discard(tid)
                        self._pending = max(0, self._pending - 1)
                self._queue.task_done()

    def _run_one(self, tid: str) -> None:
        with self._lock:
            task = self._tasks.get(tid)
            if task is None:
                return
            status = task.get("status")
            if status == "paused" or task_is_finished(task):
                return
            self._active_tid = tid
            touch_task(task, status="running", message="执行中", cooldown_until=None)
            self._persist_locked()
        self._notify(task)
        self._log("info", f"开始任务 {tid}（{task.get('type')}）", tid)

        status = "failed"
        message = ""
        try:
            settings = self.store.load_settings()
            limiter = self._prepare_limiter(settings)
            client = self._client_factory()
            from . import executors

            executors.run_task(self, task, client=client, settings=settings, limiter=limiter)
            status, message = "done", "已完成"
        except TaskInterrupted:
            status, message = "queued", "程序退出，任务将在下次启动时续跑"
            self._log("warn", f"任务 {tid} 因程序退出中断，等待续跑", tid)
        except TaskStopped as exc:
            status, message = "stopped", str(exc) or "已停止"
            self._log("warn", f"任务 {tid} 已停止：{message}", tid)
        except TaskFailed as exc:
            status, message = "failed", str(exc) or "任务失败"
            self._log("error", f"任务 {tid} 失败：{message}", tid)
        except BiliCleanerError as exc:
            status, message = "failed", str(exc) or "任务失败"
            self._log("error", f"任务 {tid} 失败：{message}", tid)
        except Exception as exc:  # noqa: BLE001 - 兜底，避免线程死掉
            status, message = "failed", f"任务异常：{exc}"
            self._log("error", f"任务 {tid} 异常：{exc!r}", tid)
        finally:
            with self._lock:
                if task.get("status") == "stopped":
                    status, message = "stopped", task.get("message") or "已停止"
                touch_task(task, status=status, message=message, cooldown_until=None)
                task["updated_at"] = now_iso()
                self._active_tid = None
                self._persist_locked()
            if status == "done":
                self._log("info", f"任务 {tid} 完成：成功 {task.get('done')} 条", tid)
            self._notify(task)

    def _prepare_limiter(self, settings: dict) -> object | None:
        """每个任务开始时拿一个新限速器并重置状态。"""
        try:
            limiter = self._limiter_factory()
        except Exception as exc:  # noqa: BLE001
            self._log("warn", f"创建限速器失败：{exc}")
            return None
        updater = getattr(limiter, "update_settings", None)
        if callable(updater):
            try:
                updater(settings)
            except Exception:  # noqa: BLE001
                pass
        resetter = getattr(limiter, "reset", None)
        if callable(resetter):
            try:
                resetter()
            except Exception:  # noqa: BLE001
                pass
        setter = getattr(limiter, "set_abort_check", None)
        if callable(setter):
            setter(lambda: self._shutdown.is_set())
        self._limiter = limiter
        return limiter

    # ============================================================ 内部：持久化

    def _load_persisted(self) -> None:
        try:
            tasks = self.store.load_tasks()
        except OSError:
            tasks = []
        with self._lock:
            for task in tasks:
                if not isinstance(task, dict) or not task.get("id"):
                    continue
                self._tasks[str(task["id"])] = task

    def _recover_locked(self) -> None:
        """把上次中断的任务重新排进队列；用户主动暂停的任务保持暂停。"""
        changed = False
        for task in self._tasks.values():
            status = task.get("status")
            if status in ("running", "cooldown"):
                touch_task(
                    task,
                    status="queued",
                    message="检测到上次未完成，从断点继续",
                    cooldown_until=None,
                )
                changed = True
            if task.get("status") == "queued":
                self._enqueue_locked(task["id"])
                self._log(
                    "info",
                    f"恢复排队任务 {task['id']}（{task.get('type')}）",
                    task["id"],
                )
        if changed:
            self._persist_locked()

    def _enqueue_locked(self, tid: str) -> None:
        """把任务排进队列（同一任务不会重复入队，避免重复计数）。"""
        if tid in self._enqueued:
            return
        self._enqueued.add(tid)
        self._pending += 1
        self._queue.put(tid)

    def _persist_locked(self) -> None:
        try:
            self.store.save_tasks(list(self._tasks.values()))
        except OSError as exc:
            self._log("error", f"任务状态写入失败：{exc}")

    # ============================================================ 内部：回调

    def _notify(self, task: dict) -> None:
        snapshot = None
        for listener in list(self._listeners):
            try:
                if snapshot is None:
                    snapshot = copy.deepcopy(task)
                listener(snapshot)
            except Exception:  # noqa: BLE001 - 回调异常不能影响任务
                continue

    def _log(self, level: str, msg: str, task_id: str | None = None) -> None:
        try:
            self.logger(level, msg, task_id)
        except TypeError:
            try:
                self.logger(level, msg)  # type: ignore[call-arg]
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass
