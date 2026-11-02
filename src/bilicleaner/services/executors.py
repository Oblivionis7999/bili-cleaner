"""任务执行器：按 ``task.type`` 分派到具体实现。

每个执行器都遵守同一套顺序（见 docs/API-CONTRACT.md 与 ADR 0002/0003）：

1. **强制备份**：先把待处理对象写成 CSV+JSONL，备份失败则任务直接失败；
2. **校验确认**：清空类任务重新核对确认词，取关类任务重新核对风险勾选；
3. **逐条/逐批执行**：每个对象前调用 ``tm.checkpoint``（响应暂停/停止）；
4. **回报进度**：``tm.update_progress`` + 每个对象写一行操作日志（断点续跑）；
5. **尊重风控**：``RateLimiter.on_risk`` 说冷却就冷却重试，说停就停。

``bili`` 包是懒加载的：这样测试可以注入假实现，也避免没登录时加载网络层。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Callable

from ..const import AUTH_CODES, DESTRUCTIVE_CLEAR_TASKS
from ..errors import (
    AuthRequiredError,
    BackupError,
    BiliApiError,
    BiliCleanerError,
    RiskControlError,
)
from .task_engine import TaskFailed, TaskStopped

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from .task_engine import TaskManager

# 连续失败多少次后放弃任务（历史脚本的经验值）
MAX_FAIL_STREAK = 10


# ---------------------------------------------------------------- bili 懒加载


def _bili() -> SimpleNamespace:
    """返回 bili 子模块集合；测试可以 monkeypatch 本函数注入假实现。"""
    from ..bili import auth, dynamics, favorites, follows, toview

    return SimpleNamespace(
        auth=auth,
        dynamics=dynamics,
        favorites=favorites,
        follows=follows,
        toview=toview,
    )


# ---------------------------------------------------------------- 公共工具


def _user(tm: "TaskManager", client: object) -> dict:
    """当前账号信息（备份文件名/CSV 里要用 mid）。"""
    try:
        session = tm.store.load_session() or {}
        user = session.get("user") or {}
        if user.get("mid"):
            return user
    except OSError:
        pass
    try:
        user = _bili().auth.fetch_me(client)
        if isinstance(user, dict) and user.get("mid"):
            return user
    except Exception:  # noqa: BLE001 - 拿不到账号信息不致命
        pass
    return {"mid": None, "name": ""}


def _is_mutual(item: dict | None) -> bool:
    """判断是否互关（attribute=6 或 mutual=True）。"""
    if not item:
        return False
    if item.get("mutual"):
        return True
    try:
        return int(item.get("attribute") or 0) == 6
    except (TypeError, ValueError):
        return False


def _verify_ack(task: dict) -> None:
    """取关类任务：再确认一次风险勾选。"""
    params = task.get("params") or {}
    if not params.get("ack_risk"):
        raise TaskFailed("缺少风险确认（ack_risk），任务中止")


def _verify_confirm(task: dict, settings: dict) -> None:
    """清空类任务：再核对一次确认词（防止设置被中途改掉）。"""
    task_type = task.get("type")
    params = task.get("params") or {}
    if task_type not in DESTRUCTIVE_CLEAR_TASKS:
        return
    # 契约补充约定：favorite_clear 传了 ids 表示"只删勾选的这批"，不需要确认词
    if task_type == "favorite_clear" and params.get("ids"):
        return
    expected = str(settings.get("confirm_phrase") or "清空")
    actual = str(params.get("confirm_phrase") or "").strip()
    if actual != expected:
        raise TaskFailed("确认词校验失败，任务中止（未发出任何删除请求）")


def _backup(tm: "TaskManager", task: dict, items: list[dict], user: dict) -> dict:
    """写强制备份；失败抛 :class:`TaskFailed`。"""
    try:
        return tm.backup_export(task, items, user)
    except BackupError as exc:
        raise TaskFailed(f"备份失败，任务中止：{exc}") from exc


def _handle_risk(
    tm: "TaskManager",
    task: dict,
    limiter: object | None,
    settings: dict,
    code: object,
) -> bool:
    """风控处理：返回 True=冷却后重试，False=应当停止任务。"""
    on_risk = getattr(limiter, "on_risk", None) if limiter is not None else None
    if not callable(on_risk):
        return False
    try:
        should_continue = bool(on_risk(code))
    except Exception:  # noqa: BLE001 - 限速器异常按"停止"处理更安全
        return False
    if not should_continue:
        return False
    try:
        seconds = float(settings.get("cooldown_seconds"))
    except (TypeError, ValueError):
        seconds = 600.0
    seconds = max(0.0, seconds)
    tm.cooldown(task, seconds, message=f"触发风控（{code}），冷却 {int(seconds)} 秒")
    tm.sleep_interruptible(task, seconds)
    tm.end_cooldown(task, message="冷却结束，继续执行")
    return True


def _run_operation(
    tm: "TaskManager",
    task: dict,
    limiter: object | None,
    settings: dict,
    op: Callable[[], None],
) -> tuple[bool, str]:
    """执行一次写操作，内部处理风控冷却重试。

    返回 ``(是否成功, 错误消息)``；风控次数用尽时抛 :class:`TaskStopped`，
    登录失效时抛 :class:`TaskFailed`。
    """
    while True:
        tm.checkpoint(task)
        try:
            op()
        except RiskControlError as exc:
            code = getattr(exc, "bili_code", None)
            if _handle_risk(tm, task, limiter, settings, code):
                continue
            raise TaskStopped(
                f"连续触发风控（{code}），任务已停止；建议过几小时再试"
            ) from exc
        except AuthRequiredError as exc:
            raise TaskFailed("登录已失效，请重新扫码登录") from exc
        except BiliApiError as exc:
            if int(getattr(exc, "bili_code", 0) or 0) in AUTH_CODES:
                raise TaskFailed("登录已失效，请重新扫码登录") from exc
            return False, str(exc)
        except BiliCleanerError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 - 网络异常记一次失败，继续下一条
            return False, f"{type(exc).__name__}: {exc}"
        else:
            on_success = getattr(limiter, "on_success", None) if limiter is not None else None
            if callable(on_success):
                try:
                    on_success()
                except Exception:  # noqa: BLE001
                    pass
            return True, ""


def _valid_fav_key(key: str) -> bool:
    """收藏条目 key 必须是 ``id:type`` 两段数字。"""
    left, sep, right = str(key).partition(":")
    return bool(sep and left.isdigit() and right.isdigit())


def _guard_read(
    tm: "TaskManager",
    task: dict,
    limiter: object | None,
    settings: dict,
    what: str,
    fetch: Callable[[], object],
) -> list:
    """读取类接口：撞风控时冷却重试；登录失效/业务错误转成 :class:`TaskFailed`。"""
    while True:
        tm.checkpoint(task)
        try:
            return list(fetch())  # type: ignore[arg-type]
        except RiskControlError as exc:
            code = getattr(exc, "bili_code", None)
            if _handle_risk(tm, task, limiter, settings, code):
                continue
            raise TaskFailed(
                f"读取{what}时连续触发风控（{code}），任务已停止；建议过几小时再试"
            ) from exc
        except AuthRequiredError as exc:
            raise TaskFailed("登录已失效，请重新扫码登录") from exc
        except BiliApiError as exc:
            if int(getattr(exc, "bili_code", 0) or 0) in AUTH_CODES:
                raise TaskFailed("登录已失效，请重新扫码登录") from exc
            raise TaskFailed(f"读取{what}失败：{exc}") from exc
        except BiliCleanerError as exc:
            raise TaskFailed(f"读取{what}失败：{exc}") from exc
        except Exception as exc:  # noqa: BLE001 - 网络异常同样转成任务失败
            raise TaskFailed(f"读取{what}失败：{type(exc).__name__}: {exc}") from exc


def _finish_message(done: int, failed: int, total: int, extra: str = "") -> str:
    text = f"完成 {done}/{total}"
    if failed:
        text += f"，失败 {failed}"
    if extra:
        text += f"；{extra}"
    return text


# ---------------------------------------------------------------- 取关


def _run_unfollow(tm: "TaskManager", task: dict, client: object, settings: dict, limiter: object | None) -> None:
    _verify_ack(task)
    params = task.get("params") or {}
    requested = [int(m) for m in (params.get("mids") or [])]
    if not requested:
        raise TaskFailed("任务没有要取关的对象")

    bili = _bili()
    user = _user(tm, client)
    follows_list = _guard_read(
        tm, task, limiter, settings, "关注列表", lambda: bili.follows.iter_followings(client)
    )
    follow_map = {int(item["mid"]): item for item in follows_list}

    protect_mutual = bool(settings.get("protect_mutual", True))
    targets: list[dict] = []
    skipped_mutual = 0
    for mid in requested:
        item = follow_map.get(mid)
        if protect_mutual and _is_mutual(item):
            skipped_mutual += 1
            continue
        targets.append(
            item
            or {
                "mid": mid,
                "name": "",
                "sign": "",
                "official": "",
                "mutual": False,
                "attribute": 0,
                "groups": [],
            }
        )

    total = len(targets)
    tm.set_total(
        task,
        total,
        message=f"待取关 {total} 个" + (f"，跳过互关 {skipped_mutual} 个" if skipped_mutual else ""),
    )
    if not targets:
        tm.update_progress(
            task,
            done=0,
            failed=0,
            message=f"没有需要取关的对象（跳过互关 {skipped_mutual} 个）",
        )
        return

    _backup(tm, task, targets, user)

    done_keys = tm.succeeded_keys(task)
    todo = [item for item in targets if str(item.get("mid")) not in done_keys]
    done = total - len(todo)
    failed = 0
    fail_streak = 0
    tm.update_progress(task, done=done, failed=0, message=f"已取关 {done}/{total}")

    for item in todo:
        mid = int(item["mid"])
        ok, msg = _run_operation(
            tm, task, limiter, settings, lambda mid=mid: bili.follows.unfollow(client, mid)
        )
        tm.append_op(task, str(mid), ok, 0 if ok else -1, msg)
        if ok:
            done += 1
            fail_streak = 0
        else:
            failed += 1
            fail_streak += 1
            tm.log("warn", f"取关 {mid} 失败：{msg}", task["id"])
            if fail_streak >= MAX_FAIL_STREAK:
                raise TaskFailed(f"连续失败 {fail_streak} 次，任务中止（可稍后重试续跑）")
        tm.update_progress(
            task, done=done, failed=failed, message=_finish_message(done, failed, total)
        )


# ---------------------------------------------------------------- 删动态


def _run_dynamic_delete(tm: "TaskManager", task: dict, client: object, settings: dict, limiter: object | None) -> None:
    params = task.get("params") or {}
    ids = [str(i) for i in (params.get("ids") or [])]
    if not ids:
        raise TaskFailed("任务没有要删除的动态")

    bili = _bili()
    user = _user(tm, client)
    mid = user.get("mid")
    if not mid:
        raise TaskFailed("读取账号信息失败，请重新登录后再试")
    dynamics_list = _guard_read(
        tm,
        task,
        limiter,
        settings,
        "动态列表",
        lambda: bili.dynamics.iter_dynamics(client, int(mid)),
    )
    item_map = {str(item.get("id")): item for item in dynamics_list}

    targets = [item_map.get(dyn_id) or {"id": dyn_id} for dyn_id in ids]
    total = len(targets)
    tm.set_total(task, total, message=f"待删除 {total} 条动态")
    _backup(tm, task, targets, user)

    done_keys = tm.succeeded_keys(task)
    todo = [item for item in targets if str(item.get("id")) not in done_keys]
    done = total - len(todo)
    failed = 0
    fail_streak = 0
    tm.update_progress(task, done=done, failed=0, message=f"已删除 {done}/{total}")

    for item in todo:
        dyn_id = str(item.get("id"))
        ok, msg = _run_operation(
            tm, task, limiter, settings, lambda dyn_id=dyn_id: bili.dynamics.delete_dynamic(client, dyn_id)
        )
        tm.append_op(task, dyn_id, ok, 0 if ok else -1, msg)
        if ok:
            done += 1
            fail_streak = 0
        else:
            failed += 1
            fail_streak += 1
            tm.log("warn", f"删除动态 {dyn_id} 失败：{msg}", task["id"])
            if fail_streak >= MAX_FAIL_STREAK:
                raise TaskFailed(f"连续失败 {fail_streak} 次，任务中止（可稍后重试续跑）")
        tm.update_progress(
            task, done=done, failed=failed, message=_finish_message(done, failed, total)
        )


# ---------------------------------------------------------------- 清空收藏夹


def _run_favorite_clear(tm: "TaskManager", task: dict, client: object, settings: dict, limiter: object | None) -> None:
    params = task.get("params") or {}
    folder_id = int(params.get("folder_id") or 0)
    requested = [str(r) for r in (params.get("ids") or [])]
    if not folder_id:
        raise TaskFailed("收藏夹参数不完整")
    bad_keys = [key for key in requested if not _valid_fav_key(key)]
    if bad_keys:
        raise TaskFailed(f"收藏条目参数格式错误（应为 id:type）：{bad_keys[0]}")
    if requested:
        # 部分删除：只需要风险勾选，不需要手打确认词
        if not params.get("ack_risk"):
            raise TaskFailed("缺少风险确认（ack_risk），任务中止")
    else:
        _verify_confirm(task, settings)

    bili = _bili()
    user = _user(tm, client)
    fav_items = _guard_read(
        tm, task, limiter, settings, "收藏夹", lambda: bili.favorites.iter_items(client, folder_id)
    )
    item_map = {f"{item.get('id')}:{item.get('type')}": item for item in fav_items}

    if requested:
        resources = requested
    else:
        # 清空整个收藏夹：分页枚举全部条目（备份要全量）
        resources = list(item_map.keys())

    targets: list[dict] = []
    for key in resources:
        targets.append(item_map.get(key) or {"id": key.split(":")[0], "type": key.split(":")[-1]})
    total = len(targets)
    tm.set_total(
        task,
        total,
        message=("待取消收藏 " if requested else "待清空收藏夹 ") + f"{total} 条",
    )
    _backup(tm, task, targets, user)
    if total == 0:
        tm.update_progress(task, done=0, failed=0, message="收藏夹已经是空的，无需清理")
        return

    done_keys = tm.succeeded_keys(task)
    todo = [key for key in resources if key not in done_keys]
    done = total - len(todo)
    failed = 0
    # batch-del 接口单批最多 20 个 resources，即使用户把设置调大也要夹住
    max_batch = int(getattr(bili.favorites, "MAX_BATCH_SIZE", 20) or 20)
    batch_size = max(1, min(max_batch, int(settings.get("batch_size") or 20)))
    fail_streak = 0
    tm.update_progress(task, done=done, failed=0, message=f"已取消收藏 {done}/{total}")

    for start in range(0, len(todo), batch_size):
        chunk = todo[start : start + batch_size]
        ok, msg = _run_operation(
            tm,
            task,
            limiter,
            settings,
            lambda chunk=chunk: bili.favorites.batch_delete(client, folder_id, chunk),
        )
        for key in chunk:
            tm.append_op(task, key, ok, 0 if ok else -1, msg)
        if ok:
            done += len(chunk)
            fail_streak = 0
        else:
            failed += len(chunk)
            fail_streak += 1
            tm.log("warn", f"批量取消收藏失败（{len(chunk)} 条）：{msg}", task["id"])
            if fail_streak >= MAX_FAIL_STREAK:
                raise TaskFailed(f"连续失败 {fail_streak} 批，任务中止（可稍后重试续跑）")
        tm.update_progress(
            task, done=done, failed=failed, message=_finish_message(done, failed, total)
        )


# ---------------------------------------------------------------- 清理失效收藏


def _run_favorite_clean_invalid(
    tm: "TaskManager", task: dict, client: object, settings: dict, limiter: object | None
) -> None:
    _verify_confirm(task, settings)
    params = task.get("params") or {}
    folder_id = int(params.get("folder_id") or 0)
    if not folder_id:
        raise TaskFailed("收藏夹参数不完整")

    bili = _bili()
    user = _user(tm, client)
    items = _guard_read(
        tm, task, limiter, settings, "收藏夹", lambda: bili.favorites.iter_items(client, folder_id)
    )

    invalid = [item for item in items if item.get("invalid")]
    total = len(invalid)
    tm.set_total(task, total, message=f"待清理失效内容 {total} 条")
    _backup(tm, task, invalid, user)

    op_key = f"clean_invalid:{folder_id}"
    if op_key in tm.succeeded_keys(task):
        tm.update_progress(task, done=total, failed=0, message=f"失效内容已清理（{total} 条）")
        return
    if not invalid:
        tm.update_progress(task, done=0, failed=0, message="没有失效内容，无需清理")
        return

    ok, msg = _run_operation(
        tm, task, limiter, settings, lambda: bili.favorites.clean_invalid(client, folder_id)
    )
    tm.append_op(task, op_key, ok, 0 if ok else -1, msg)
    if ok:
        tm.update_progress(task, done=total, failed=0, message=f"已清理失效内容 {total} 条")
    else:
        raise TaskFailed(f"清理失效内容失败：{msg}")


# ---------------------------------------------------------------- 清空稍后再看


def _run_toview_clear(tm: "TaskManager", task: dict, client: object, settings: dict, limiter: object | None) -> None:
    _verify_confirm(task, settings)
    bili = _bili()
    user = _user(tm, client)
    items = _guard_read(
        tm, task, limiter, settings, "稍后再看", lambda: bili.toview.list_toview(client)
    )

    total = len(items)
    tm.set_total(task, total, message=f"待清空稍后再看 {total} 条")
    _backup(tm, task, items, user)

    op_key = "clear_toview"
    if op_key in tm.succeeded_keys(task):
        tm.update_progress(task, done=total, failed=0, message=f"稍后再看已清空（{total} 条）")
        return
    if not items:
        tm.update_progress(task, done=0, failed=0, message="稍后再看已经是空的")
        return

    ok, msg = _run_operation(tm, task, limiter, settings, lambda: bili.toview.clear_toview(client))
    tm.append_op(task, op_key, ok, 0 if ok else -1, msg)
    if ok:
        tm.update_progress(task, done=total, failed=0, message=f"已清空稍后再看 {total} 条")
    else:
        raise TaskFailed(f"清空稍后再看失败：{msg}")


# ---------------------------------------------------------------- LLM 分类


def _run_classify_llm(tm: "TaskManager", task: dict, client: object, settings: dict, limiter: object | None) -> None:
    params = task.get("params") or {}
    requested = [int(m) for m in (params.get("mids") or [])]
    if not requested:
        raise TaskFailed("任务没有要分类的对象")

    bili = _bili()
    user = _user(tm, client)
    follows_list = _guard_read(
        tm, task, limiter, settings, "关注列表", lambda: bili.follows.iter_followings(client)
    )
    follow_map = {int(item["mid"]): item for item in follows_list}

    targets = [follow_map[mid] for mid in requested if mid in follow_map]
    total = len(targets)
    tm.set_total(task, total, message=f"待分类 {total} 个关注")
    if not targets:
        tm.update_progress(task, done=0, failed=0, message="没有找到要分类的关注对象")
        return

    _backup(tm, task, targets, user)

    classify_mod = tm.classify
    cache = classify_mod.CategoryCache(tm.store)
    llm_cfg = settings.get("llm") or {}
    classifier = None
    if llm_cfg.get("enabled"):
        candidate = classify_mod.LlmClassifier(llm_cfg)
        if candidate.configured:
            classifier = candidate
        else:
            tm.log("warn", "LLM 分类已开启但未配置 base_url/model，退回规则分类", task["id"])

    done_keys = tm.succeeded_keys(task)
    todo = [item for item in targets if str(item.get("mid")) not in done_keys]
    done = total - len(todo)
    failed = 0
    tm.update_progress(task, done=done, failed=0, message=f"已分类 {done}/{total}")

    batch_size = max(1, int(llm_cfg.get("batch_size") or 20))
    if classifier is None:
        batch_size = 50
    set_category = getattr(client, "set_follow_category", None)

    for start in range(0, len(todo), batch_size):
        tm.checkpoint(task)
        chunk = todo[start : start + batch_size]
        mapping: dict[int, tuple[str, str]] = {}
        if classifier is not None:
            try:
                llm_result = classifier.classify(chunk)
                mapping.update({mid: (category, "llm") for mid, category in llm_result.items()})
            except BiliCleanerError as exc:
                tm.log("warn", f"LLM 分类失败，本批退回规则分类：{exc}", task["id"])
        # 没被 LLM 判定的（失败/漏判）用规则兜底
        for item in chunk:
            mid = int(item.get("mid"))
            if mid in mapping:
                continue
            category, _confidence = classify_mod.classify_rule(item)
            mapping[mid] = (category, "rule")

        if mapping:
            cache.update(mapping)
            if callable(set_category):
                for mid, (category, source) in mapping.items():
                    try:
                        set_category(mid, category, source)
                    except Exception:  # noqa: BLE001 - mock 客户端可选能力
                        break
        for item in chunk:
            mid = int(item.get("mid"))
            tm.append_op(task, str(mid), True, 0, mapping.get(mid, ("", ""))[0])
        done += len(chunk)
        tm.update_progress(
            task,
            done=done,
            failed=failed,
            message=f"已分类 {done}/{total}" + ("（规则分类）" if classifier is None else ""),
        )


# ---------------------------------------------------------------- 分派表

_HANDLERS: dict[str, Callable[..., None]] = {
    "unfollow": _run_unfollow,
    "dynamic_delete": _run_dynamic_delete,
    "favorite_clear": _run_favorite_clear,
    "favorite_clean_invalid": _run_favorite_clean_invalid,
    "toview_clear": _run_toview_clear,
    "classify_llm": _run_classify_llm,
}


def run_task(
    tm: "TaskManager",
    task: dict,
    *,
    client: object,
    settings: dict,
    limiter: object | None = None,
) -> None:
    """按任务类型执行；执行器返回即表示成功，失败用异常表达。"""
    task_type = str(task.get("type") or "")
    handler = _HANDLERS.get(task_type)
    if handler is None:
        raise TaskFailed(f"未知任务类型：{task_type}")
    started = time.time()
    handler(tm, task, client, settings, limiter)
    tm.log(
        "info",
        f"任务 {task.get('id')} 执行结束，耗时 {int(time.time() - started)} 秒",
        task.get("id"),
    )
