"""FastAPI 应用组装：运行上下文、路由挂载、错误处理、静态文件。

``create_app(store, mock=False)`` 是唯一入口（由 ``python -m bilicleaner`` 调用）。
所有可变状态都挂在 ``app.state.ctx`` 上，路由通过 ``request.app.state.ctx`` 取用，
这样路由模块不需要 import 本模块，也不会产生循环依赖。

错误约定（docs/API-CONTRACT.md）：所有错误都是 HTTP 4xx/5xx + JSON
``{"error": {"code": "...", "message": "..."}}``；登录态失效统一 401
``NOT_LOGGED_IN``；风控不向上抛 500（读接口遇到时返回 429）。
"""

from __future__ import annotations

import copy
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..const import APP_NAME, VERSION
from ..errors import (
    AuthRequiredError,
    BackupError,
    BiliApiError,
    BiliCleanerError,
    RiskControlError,
    TaskError,
)
from ..store import Store
from ..services import classify as classify_mod
from ..services.backup import BackupService
from ..services.classify import CategoryCache
from ..services.ratelimit import RateLimiter
from ..services.task_engine import TaskManager
from . import routes_auth, routes_data, routes_misc, routes_tasks

# 静态文件目录：<repo>/src/bilicleaner/web
_WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_PLACEHOLDER_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{app} · 前端资源尚未就绪</title>
<style>
  body {{ margin: 0; font: 17px/1.5 -apple-system, "SF Pro Text", "Segoe UI", sans-serif;
         background: #fff; color: #1d1d1f; display: grid; place-items: center; min-height: 100vh; }}
  main {{ max-width: 560px; padding: 40px 24px; }}
  h1 {{ font-size: 28px; letter-spacing: -0.02em; margin: 0 0 12px; }}
  p {{ color: #6e6e73; margin: 0 0 8px; }}
  code {{ background: #f5f5f7; border-radius: 6px; padding: 2px 6px; font-size: 14px; }}
</style>
</head>
<body>
<main>
  <h1>{app} 后端已启动</h1>
  <p>前端静态文件（<code>src/bilicleaner/web/index.html</code>）还没写好。</p>
  <p>API 已经可用，例如 <code>GET /api/health</code>、<code>GET /api/settings</code>。</p>
</main>
</body>
</html>
""".format(app=APP_NAME)


class AppContext:
    """进程级运行上下文：客户端、限速器、任务引擎、各类缓存。"""

    #: 关注/动态/收藏夹等只读数据的缓存秒数
    CACHE_TTL = 120.0

    def __init__(
        self,
        store: Store,
        mock: bool = False,
        client_factory: Callable[[], object] | None = None,
    ):
        self.store = store
        self.mock = bool(mock)
        self.started_at = time.time()

        self.limiter = RateLimiter(store.load_settings())
        self.categories = CategoryCache(store)
        self.backup = BackupService(store)
        self.classify = classify_mod

        self._client_factory = client_factory
        self._client: object | None = None
        self._client_lock = threading.Lock()
        self._user: dict | None = None
        self._cache: dict[str, tuple[float, object]] = {}
        self._cache_lock = threading.Lock()

        self.tm = TaskManager(
            store=store,
            client_factory=self.get_client,
            limiter_factory=lambda: self.limiter,
            backup=self.backup,
            classify=classify_mod,
            logger=self.log,
        )
        self.tm.add_listener(self._on_task_event)

        # 关闭流程：app.state.shutdown_hook 是真正退出的动作（测试可替换），
        # shutdown_delay 让 HTTP 响应先发出去再动手。
        self.app: FastAPI | None = None
        self.shutdown_delay = 0.2
        self._shutdown_lock = threading.Lock()
        self._shutdown_started = False

    # ------------------------------------------------------------ 日志/设置

    def log(self, level: str, msg: str, task_id: str | None = None) -> None:
        try:
            self.store.append_log(level, msg, task_id)
        except OSError:
            pass

    def get_settings(self) -> dict:
        return self.store.load_settings()

    def update_settings(self, patch: dict) -> dict:
        merged = self._save_settings(patch or {})
        try:
            self.limiter.update_settings(merged)
        except Exception:  # noqa: BLE001 - 设置热更新失败不该影响保存
            pass
        self.tm.apply_settings(merged)
        client = self._client
        if client is not None and hasattr(client, "settings"):
            try:
                client.settings = merged  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        self.log("info", "设置已更新")
        return merged

    def _save_settings(self, patch: dict) -> dict:
        """保存设置；Windows 上 tasks/设置文件被并发读时 os.replace 可能短暂失败，重试几次。"""
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return self.store.save_settings(patch)
            except PermissionError as exc:  # 只在 Windows 上偶发
                last_error = exc
                time.sleep(0.05 * (attempt + 1))
        raise BiliCleanerError(f"设置保存失败：{last_error}", code="SETTINGS_FAILED") from last_error

    # ------------------------------------------------------------ 客户端

    def get_client(self, require_login: bool = True) -> object:
        """拿共享客户端；未登录且需要登录时抛 :class:`AuthRequiredError`。"""
        with self._client_lock:
            client = self._client
            if client is None:
                client = self._create_client()
                self._client = client
        if require_login and not self.is_logged_in():
            raise AuthRequiredError("尚未登录，请先扫码登录")
        return client

    def _create_client(self) -> object:
        if self._client_factory is not None:
            return self._client_factory()
        if self.mock:
            return self._create_mock_client()
        from ..bili.client import BiliClient

        return BiliClient(self.store, self.store.load_settings(), self.limiter)

    def _create_mock_client(self) -> object:
        from ..mock.mock_client import MockBiliClient

        try:
            return MockBiliClient()
        except TypeError:
            return MockBiliClient(self.store, self.store.load_settings(), self.limiter)

    def reset_client(self) -> None:
        """丢掉客户端（退出登录/切换账号时）。"""
        with self._client_lock:
            client, self._client = self._client, None
        self._user = None
        self.invalidate()
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass

    def close(self) -> None:
        self.reset_client()
        try:
            self.limiter.set_abort_check(None)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def client_cookies(client: object) -> dict:
        """从客户端上抠出 Cookie（兼容 dict 与 requests.Session）。"""
        cookies = getattr(client, "cookies", None)
        if isinstance(cookies, dict):
            return {str(k): str(v) for k, v in cookies.items()}
        session = getattr(client, "session", None)
        jar = getattr(session, "cookies", None)
        if jar is not None:
            try:
                from requests.utils import dict_from_cookiejar

                return {str(k): str(v) for k, v in dict_from_cookiejar(jar).items()}
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def is_logged_in(self) -> bool:
        """mock 模式看 mock 客户端的登录态，真实模式看客户端/本地 session。"""
        if self.mock:
            try:
                client = self.get_client(require_login=False)
            except Exception:  # noqa: BLE001 - mock 包未就绪
                return False
            flag = getattr(client, "logged_in", None)
            if isinstance(flag, bool):
                return flag
            return self.store.has_session()
        try:
            client = self.get_client(require_login=False)
        except Exception:  # noqa: BLE001 - 客户端都建不起来就按未登录处理
            return self.store.has_session()
        flag = getattr(client, "is_logged_in", None)
        if isinstance(flag, bool):
            return flag
        return self.store.has_session()

    # ------------------------------------------------------------ 用户

    def get_user(self) -> dict:
        """当前登录用户；拿不到登录态抛 :class:`AuthRequiredError`。"""
        if isinstance(self._user, dict) and self._user.get("mid"):
            return dict(self._user)
        if not self.is_logged_in():
            raise AuthRequiredError("尚未登录，请先扫码登录")
        if self.mock:
            user = self._mock_user() or self._fallback_user()
        else:
            session = self.store.load_session() or {}
            user = dict(session.get("user") or {})
            if not user.get("mid"):
                # BiliClient 可能在 nav 时缓存了用户信息，先用它省一次请求
                try:
                    client = self.get_client(require_login=False)
                    cached_user = getattr(client, "user", None)
                    if isinstance(cached_user, dict) and cached_user.get("mid"):
                        user = dict(cached_user)
                except Exception:  # noqa: BLE001
                    pass
            if not user.get("mid"):
                user = self._fetch_me()
        self._user = user
        return dict(user)

    def require_user_mid(self) -> int:
        try:
            return int(self.get_user().get("mid") or 0)
        except (TypeError, ValueError):
            return 0

    def _fetch_me(self) -> dict:
        try:
            client = self.get_client(require_login=False)
            user = self._bili().auth.fetch_me(client)
        except BiliCleanerError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AuthRequiredError(f"读取账号信息失败：{exc}") from exc
        if not isinstance(user, dict) or not user.get("mid"):
            raise AuthRequiredError("尚未登录，请先扫码登录")
        return user

    def _mock_user(self) -> dict | None:
        client = self.get_client(require_login=False)
        getters: list[Callable[[], object]] = []
        fetch = getattr(client, "fetch_me", None)
        if callable(fetch):
            getters.append(fetch)
        getters.append(lambda: self._bili().auth.fetch_me(client))
        for getter in getters:
            try:
                user = getter()
            except Exception:  # noqa: BLE001 - mock 客户端能力可能不全
                continue
            if isinstance(user, dict) and user.get("mid"):
                return user
        candidate = getattr(client, "user", None)
        if isinstance(candidate, dict) and candidate.get("mid"):
            return candidate
        return None

    @staticmethod
    def _fallback_user() -> dict:
        return {"mid": 10000, "name": "模拟用户", "face": "", "vip": False}

    # ------------------------------------------------------------ 认证

    def qrcode_generate(self) -> dict:
        client = self.get_client(require_login=False)
        data = self._bili().auth.qrcode_generate(client)
        result = dict(data or {}) if isinstance(data, dict) else {}
        result.setdefault("expires_in", 180)
        self.log("info", "已生成登录二维码")
        return result

    def qrcode_poll(self, key: str) -> dict:
        client = self.get_client(require_login=False)
        data = self._bili().auth.qrcode_poll(client, key)
        result = dict(data or {}) if isinstance(data, dict) else {"status": "waiting", "user": None}
        if result.get("status") == "confirmed":
            user = result.get("user") if isinstance(result.get("user"), dict) else {}
            cookies = self.client_cookies(client)
            if not cookies and self.mock:
                cookies = {"mock_session": "1"}
            try:
                self.store.save_session(cookies, user)
            except OSError as exc:
                self.log("warn", f"登录态保存失败：{exc}")
            self._user = user if user.get("mid") else None
            self.invalidate()
            self.log("info", "扫码登录成功")
        return result

    def me(self) -> dict:
        try:
            user = self.get_user()
        except BiliCleanerError:
            return {"logged_in": False, "user": None}
        return {"logged_in": True, "user": user}

    def logout(self) -> dict:
        client = self._client
        if client is not None:
            logout_fn = getattr(client, "logout", None)
            if callable(logout_fn):
                try:
                    logout_fn()
                except Exception:  # noqa: BLE001
                    pass
        self.store.clear_session()
        if self.mock:
            # mock 模式保留同一个客户端对象：reset 会把它内存里的清理结果重置回初始数据
            self._user = None
            self.invalidate()
        else:
            self.reset_client()
        self.log("info", "已退出登录")
        return {"ok": True}

    # ------------------------------------------------------------ 只读数据

    def _bili(self) -> SimpleNamespace:
        """懒加载 bili 子模块；测试可以 monkeypatch 本方法注入假实现。"""
        from ..bili import auth, dynamics, favorites, follows, toview

        return SimpleNamespace(
            auth=auth,
            dynamics=dynamics,
            favorites=favorites,
            follows=follows,
            toview=toview,
        )

    # -- 缓存

    def _cache_get(self, key: str) -> object | None:
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            created, value = entry
            if time.time() - created > self.CACHE_TTL:
                self._cache.pop(key, None)
                return None
            return value

    def _cache_set(self, key: str, value: object) -> None:
        with self._cache_lock:
            self._cache[key] = (time.time(), value)

    def _cached(self, key: str, loader: Callable[[], object]) -> object:
        value = self._cache_get(key)
        if value is not None:
            return value
        value = loader()
        self._cache_set(key, value)
        return value

    def invalidate(self, *keys: str) -> None:
        """清缓存；不传参数则全清。"""
        with self._cache_lock:
            if not keys:
                self._cache.clear()
                return
            for key in keys:
                self._cache.pop(key, None)

    # -- 关注

    def follows(self) -> list[dict]:
        """全量关注列表，并把本地分类缓存合并到 category 字段。"""
        raw = self._cached(
            "follows",
            lambda: [copy.deepcopy(item) for item in self._bili().follows.iter_followings(self.get_client())],
        )
        categories = self.categories.load()
        merged: list[dict] = []
        for item in raw:  # type: ignore[union-attr]
            row = dict(item)
            cached = categories.get(str(row.get("mid")))
            row["category"] = cached.get("category") if cached else row.get("category")
            row["category_source"] = cached.get("source") if cached else row.get("category_source")
            merged.append(row)
        return merged

    def groups(self) -> list[dict]:
        return list(
            self._cached(
                "groups",
                lambda: [copy.deepcopy(g) for g in self._bili().follows.list_groups(self.get_client())],
            )
        )

    def follows_stats(self) -> dict:
        """分类统计（全量），结果按 CACHE_TTL 缓存。"""

        def load() -> dict:
            items = self.follows()
            mutual = 0
            uncategorized = 0
            categories: dict[str, int] = {}
            for item in items:
                try:
                    if bool(item.get("mutual")) or int(item.get("attribute") or 0) == 6:
                        mutual += 1
                except (TypeError, ValueError):
                    pass
                category = item.get("category")
                if category:
                    categories[str(category)] = categories.get(str(category), 0) + 1
                else:
                    uncategorized += 1
            return {
                "total": len(items),
                "mutual": mutual,
                "uncategorized": uncategorized,
                "categories": categories,
            }

        return dict(self._cached("follows:stats", load))  # type: ignore[arg-type]

    def rule_classify(self) -> int:
        """规则分类全量关注，写入 CategoryCache；返回更新条数。"""
        items = self.follows()
        mapping: dict[int | str, tuple[str, str]] = {}
        for item in items:
            try:
                mid = int(item.get("mid"))
            except (TypeError, ValueError):
                continue
            category, _confidence = self.classify.classify_rule(item)
            mapping[mid] = (category, "rule")
        updated = self.categories.update(mapping) if mapping else 0
        self.invalidate("follows:stats")
        self.log("info", f"规则分类完成，更新 {updated} 个关注")
        return updated

    # -- 动态 / 收藏夹 / 稍后再看

    def dynamics(self) -> list[dict]:
        mid = self.require_user_mid()
        return list(
            self._cached(
                "dynamics",
                lambda: [
                    copy.deepcopy(item)
                    for item in self._bili().dynamics.iter_dynamics(self.get_client(), mid)
                ],
            )
        )

    def favorite_folders(self) -> list[dict]:
        return list(
            self._cached(
                "favorites:folders",
                lambda: [copy.deepcopy(f) for f in self._bili().favorites.list_folders(self.get_client())],
            )
        )

    def favorite_items(self, media_id: int) -> list[dict]:
        return list(
            self._cached(
                f"favorites:items:{int(media_id)}",
                lambda: [
                    copy.deepcopy(item)
                    for item in self._bili().favorites.iter_items(self.get_client(), int(media_id))
                ],
            )
        )

    def toview(self) -> list[dict]:
        return list(
            self._cached(
                "toview",
                lambda: [copy.deepcopy(item) for item in self._bili().toview.list_toview(self.get_client())],
            )
        )

    # ------------------------------------------------------------ 任务回调

    def _on_task_event(self, task: dict) -> None:
        """任务结束时清掉只读缓存，前端下次请求就能看到最新数据。"""
        if task.get("status") in ("done", "failed", "stopped"):
            self.invalidate()

    # ------------------------------------------------------------ 关闭程序

    def schedule_shutdown(self, delay: float | None = None) -> bool:
        """安排进程退出：立即返回，让 HTTP 响应先发出去。

        真正的退出动作是可替换的 ``app.state.shutdown_hook``（测试注入假实现，
        避免 TestClient 把 pytest 进程杀掉）。返回 False 表示已经在退出流程里。
        """
        with self._shutdown_lock:
            if self._shutdown_started:
                return False
            self._shutdown_started = True
        wait = self.shutdown_delay if delay is None else max(0.0, float(delay))
        threading.Thread(
            target=self._perform_shutdown,
            args=(wait,),
            name="bilicleaner-shutdown",
            daemon=True,
        ).start()
        return True

    def _perform_shutdown(self, delay: float) -> None:
        """延迟后：先把任务引擎停成可续跑状态，再执行退出动作。"""
        if delay > 0:
            time.sleep(delay)
        # 1) 先停任务引擎：正在跑的任务会被标记为 queued，下次启动从操作日志续跑
        try:
            self.tm.shutdown(timeout=5.0)
        except Exception as exc:  # noqa: BLE001 - 退不干净也要继续退出
            self.log("warn", f"任务引擎停机失败：{exc}")
        self.log("info", "收到关闭请求，正在退出")

        # 2) 真正退出：默认走 uvicorn 优雅退出；测试/嵌入场景可替换 hook
        hook = None
        app = self.app
        if app is not None:
            hook = getattr(app.state, "shutdown_hook", None)
        if callable(hook):
            try:
                hook()
                return
            except Exception as exc:  # noqa: BLE001 - hook 失败则强退
                self.log("warn", f"退出 hook 失败，强制退出：{exc}")
        _force_exit()


def _force_exit(delay: float = 0.0) -> None:
    """兜底退出：直接结束进程（任务状态已落盘，不依赖 atexit）。"""
    if delay > 0:
        time.sleep(delay)
    os._exit(0)


# ---------------------------------------------------------------- 错误处理


def _error_json(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message or code}},
    )


_HTTP_CODE_MAP = {
    400: "BAD_REQUEST",
    401: "NOT_LOGGED_IN",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "TOO_LARGE",
    422: "BAD_REQUEST",
    429: "RATE_LIMITED",
    500: "INTERNAL",
    503: "UNAVAILABLE",
}

_TASK_ERROR_STATUS = {
    "NOT_FOUND": 404,
    "BAD_TASK_TYPE": 400,
    "BAD_PARAMS": 400,
    "CONFIRM_REQUIRED": 400,
    "ACK_REQUIRED": 400,
    "TASK_NOT_PAUSED": 409,
    "TASK_FINISHED": 409,
    "TASK_STOPPED": 409,
}


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AuthRequiredError)
    async def _handle_auth(request: Request, exc: AuthRequiredError) -> JSONResponse:
        return _error_json(401, "NOT_LOGGED_IN", exc.message)

    @app.exception_handler(RiskControlError)
    async def _handle_risk(request: Request, exc: RiskControlError) -> JSONResponse:
        return _error_json(429, "RISK_CONTROL", "触发 B 站风控，请过一会儿再试")

    @app.exception_handler(BackupError)
    async def _handle_backup(request: Request, exc: BackupError) -> JSONResponse:
        return _error_json(500, "BACKUP_FAILED", str(exc))

    @app.exception_handler(BiliApiError)
    async def _handle_bili(request: Request, exc: BiliApiError) -> JSONResponse:
        return _error_json(502, exc.code, exc.message)

    @app.exception_handler(TaskError)
    async def _handle_task(request: Request, exc: TaskError) -> JSONResponse:
        status = _TASK_ERROR_STATUS.get(exc.code, 400)
        return _error_json(status, exc.code, exc.message)

    @app.exception_handler(BiliCleanerError)
    async def _handle_app(request: Request, exc: BiliCleanerError) -> JSONResponse:
        return _error_json(500, exc.code, str(exc))

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        message = "请求参数不合法"
        errors = exc.errors()
        if errors:
            first = errors[0]
            location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
            detail = str(first.get("msg") or "参数不合法")
            message = f"{location}: {detail}" if location else detail
        return _error_json(400, "BAD_REQUEST", message)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else "请求失败"
        return _error_json(exc.status_code, _HTTP_CODE_MAP.get(exc.status_code, "HTTP_ERROR"), message)

    @app.exception_handler(Exception)
    async def _handle_internal(request: Request, exc: Exception) -> JSONResponse:
        ctx = getattr(request.app.state, "ctx", None)
        if ctx is not None:
            ctx.log("error", f"未处理异常：{exc!r}")
        return _error_json(500, "INTERNAL", "服务器内部错误，请查看日志")


# ---------------------------------------------------------------- 静态文件


def _register_static(app: FastAPI) -> None:
    """挂载前端静态文件；目录不存在时用占位页兜底，保证服务能起来。

    前端用的是相对路径（``href="style.css"``），所以除了契约要求的 ``/static``
    之外，还要把 web 目录挂到根路径上——根挂载必须放在所有 ``/api`` 路由之后，
    否则会把接口请求吞掉。
    """
    if _WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_WEB_DIR)), name="static")
    else:

        @app.get("/static/{path:path}", include_in_schema=False)
        def _static_missing(path: str) -> JSONResponse:
            return _error_json(404, "NOT_FOUND", "前端静态资源尚未就绪")

    @app.get("/", include_in_schema=False)
    def _index() -> object:
        index_file = _WEB_DIR / "index.html"
        if index_file.is_file():
            return FileResponse(str(index_file))
        return HTMLResponse(_PLACEHOLDER_HTML)

    @app.get("/favicon.ico", include_in_schema=False)
    def _favicon() -> object:
        """浏览器每会话都会请求一次；没有图标就回 204，不在控制台留一条资源错误。"""
        icon = _WEB_DIR / "favicon.ico"
        if icon.is_file():
            return FileResponse(str(icon))
        return Response(status_code=204)

    if _WEB_DIR.is_dir():
        # 兜底：/style.css、/app.js、/index.html 等相对路径资源；html=True 让 / 回到 index
        app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")


# ---------------------------------------------------------------- 入口


def create_app(
    store: Store | None = None,
    mock: bool = False,
    client_factory: Callable[[], object] | None = None,
) -> FastAPI:
    """组装 FastAPI 应用。

    ``client_factory`` 只给测试用：注入假客户端，避免测试真连 B 站。
    """
    store = store or Store()
    ctx = AppContext(store=store, mock=mock, client_factory=client_factory)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        ctx.log("info", f"{APP_NAME} {VERSION} 启动（{'模拟数据' if mock else '真实账号'}）")
        ctx.tm.start()
        if mock:
            # mock 演示用：启动后先跑一次规则分类，省得前端演示前还要手动点"自动分类"
            try:
                updated = ctx.rule_classify()
                ctx.log("info", f"mock 模式已自动完成规则分类（{updated} 个关注）")
            except Exception as exc:  # noqa: BLE001 - 分类失败不影响启动
                ctx.log("warn", f"mock 自动分类失败（不影响启动）：{exc}")
        try:
            yield
        finally:
            ctx.tm.shutdown(timeout=5.0)
            ctx.close()
            ctx.log("info", f"{APP_NAME} 已退出")

    app = FastAPI(
        title=f"{APP_NAME} API",
        version=VERSION,
        description="本地 B 站账号清理工具的服务端接口",
        lifespan=lifespan,
    )
    app.state.ctx = ctx
    ctx.app = app

    def _default_shutdown_hook() -> None:
        """真正退出进程：优先 uvicorn 优雅退出，最终 os._exit 兜底。

        - 启动器如果把 ``uvicorn.Server`` 挂在 ``app.state.server`` 上，直接用
          ``server.should_exit = True``；
        - 否则向本进程发 SIGINT —— ``uvicorn.run()`` 在主线程注册了信号处理器，
          会走正常的优雅关停流程（源码模式与 PyInstaller exe 模式都成立）；
        - 3 秒内还没退干净就 ``os._exit(0)``（任务状态早已落盘）。
        """
        server = getattr(app.state, "server", None)
        should_exit = getattr(server, "should_exit", None) if server is not None else None
        if server is not None and isinstance(should_exit, bool):
            server.should_exit = True
            _start_hard_exit_timer()
            return
        try:
            import signal

            signal.raise_signal(signal.SIGINT)
        except Exception:  # noqa: BLE001 - 拿不到信号就强退
            _force_exit()
            return
        _start_hard_exit_timer()

    app.state.shutdown_hook = _default_shutdown_hook
    _register_error_handlers(app)

    app.include_router(routes_auth.router)
    app.include_router(routes_data.router)
    app.include_router(routes_tasks.router)
    app.include_router(routes_misc.router)
    _register_static(app)
    return app


def _start_hard_exit_timer(delay: float = 3.0) -> None:
    """优雅退出没兜住的保险丝：daemon 线程，不能阻止主进程正常退出。"""
    timer = threading.Timer(delay, _force_exit)
    timer.daemon = True
    timer.start()
