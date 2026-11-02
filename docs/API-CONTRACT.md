# BiliCleaner API 契约与模块边界

本文件是并行开发的唯一接口契约。所有子代理按此实现，不要私自改字段名；确实需要改，先改本文件并在团队消息里说明。

## 目录与归属

| 目录 | 负责人 | 说明 |
|---|---|---|
| `src/bilicleaner/{__init__,__main__,const,types,store,errors}.py` | Lead | 已写好，只读 |
| `src/bilicleaner/bili/` | backend-bili | B站接口封装（client/wbi/auth/follows/dynamics/favorites/toview） |
| `src/bilicleaner/services/` | backend-core | 限速、备份、分类、任务引擎、执行器 |
| `src/bilicleaner/api/` | backend-core | FastAPI 路由与 app 组装 |
| `src/bilicleaner/web/` | frontend | 原生 HTML/CSS/JS，Apple 风格 |
| `src/bilicleaner/mock/` | mock-tests | Mock 客户端与测试数据 |
| `tests/` | 各自补 | pytest |
| `scripts/` | mock-tests | dev/build 脚本 |

## 通用约定

- 所有接口返回 JSON；错误统一 HTTP 4xx/5xx + `{"error": {"code": "...", "message": "..."}}`。
- 时间戳：Unix 秒（int）用于 B 站原始字段；ISO 8601 字符串用于 `Task` 的 created_at 等。
- 登录态失效：业务接口返回 HTTP 401 `{"error":{"code":"NOT_LOGGED_IN"}}`。
- 触发风控：任务内处理，不向上抛 500；任务进入 `cooldown` 状态。
- 所有写操作都必须经过 `TaskManager`，API 层不得直接调用 `bili` 写方法。

## 数据模型（JSON 字段名即 Python dict key）

```jsonc
User = { "mid": 123, "name": "xxx", "face": "https://...", "vip": false }

QRCode = { "url": "https://...", "key": "32位", "expires_in": 180, "qr_png": "data:image/png;base64,..." }
QRPoll = { "status": "waiting|scanned|confirmed|expired", "user": User|null }

FollowItem = {
  "mid": 123, "name": "xxx", "sign": "签名", "face": "https://...",
  "official": "bilibili 知名科技UP主", "attribute": 2, "mutual": false,
  "groups": ["学习"], "group_ids": [74311568], "order": 1, "mtime": 1700000000,
  "category": "科技数码|null", "category_source": "rule|llm|user|null"
}
FollowList = { "total": 2000, "offset": 0, "items": [FollowItem] }
FollowGroup = { "tagid": 74311568, "name": "学习", "count": 24 }

DynamicItem = {
  "id": "1264505979038334999", "type": "DYNAMIC_TYPE_FORWARD", "type_label": "转发",
  "pub_ts": 1700000000, "text": "正文/转发语", "has_image": true,
  "video": { "bvid": "BV...", "title": "..." } | null,
  "orig_author": "原作者|null"
}
DynamicList = { "offset": "下一页游标|空串", "has_more": true, "items": [DynamicItem] }

FavFolder = { "media_id": 916673043, "title": "默认收藏夹", "count": 4565, "is_default": true, "privacy": 0 }
FavItem = { "id": "117528844047788", "type": 2, "title": "标题", "upper": "UP主",
            "bvid": "BV1...", "cover": "https://...", "fav_time": 1700000000, "invalid": false }
FavList = { "total": 4565, "offset": 0, "items": [FavItem] }

ToviewItem = { "aid": 123, "title": "标题", "upper": "UP主", "duration": 600,
               "progress": 0, "add_ts": 1700000000, "bvid": "BV1..." }
ToviewList = { "total": 875, "items": [ToviewItem] }

Task = {
  "id": "uuid", "type": "unfollow|dynamic_delete|favorite_clear|favorite_clean_invalid|toview_clear|classify_llm",
  "status": "queued|running|paused|cooldown|stopped|done|failed",
  "total": 100, "done": 96, "failed": 4,
  "created_at": "ISO", "updated_at": "ISO", "cooldown_until": "ISO|null",
  "message": "当前状态说明", "backup_path": "C:\\...|null", "params": {}
}
TaskList = { "items": [Task] }

Settings = {
  "delay_seconds": 2.5, "jitter_seconds": 0.8, "batch_size": 20, "batch_pause_seconds": 3.0,
  "cooldown_seconds": 600, "max_cooldowns": 2,
  "protect_mutual": true, "confirm_phrase": "清空",
  "backup_keep": 0,              // 0=全部保留
  "llm": { "enabled": false, "base_url": "", "api_key": "", "model": "", "batch_size": 20 }
}

BackupFile = { "path": "...", "task_id": "uuid", "count": 100, "created_at": "ISO" }
LogEntry = { "ts": "ISO", "level": "info|warn|error", "msg": "text", "task_id": "uuid|null" }
```

## HTTP 路由

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | `{ok:true, version, mock:bool}` |
| POST | `/api/auth/qrcode` | QRCode |
| GET | `/api/auth/qrcode/poll?key=` | QRPoll |
| GET | `/api/me` | `{logged_in:bool, user:User|null}` |
| POST | `/api/auth/logout` | `{ok:true}` |
| GET | `/api/follows?offset=&limit=&q=&category=&group=&mutual=` | FollowList |
| GET | `/api/follows/groups` | `{items:[FollowGroup]}` |
| POST | `/api/follows/classify` | `{updated:int}` 规则分类（同步、秒级） |
| GET | `/api/dynamics?offset=&limit=&q=&type=` | DynamicList |
| GET | `/api/favorites/folders` | `{items:[FavFolder]}` |
| GET | `/api/favorites/{media_id}/items?offset=&limit=&q=` | FavList |
| GET | `/api/toview?offset=&limit=` | ToviewList |
| POST | `/api/tasks` | body `{type, params, confirm_phrase?}` → Task |
| GET | `/api/tasks` | TaskList |
| GET | `/api/tasks/{id}` | Task |
| POST | `/api/tasks/{id}/pause` `/resume` `/stop` | Task |
| GET | `/api/backups` | `{items:[BackupFile]}` |
| GET | `/api/logs?limit=200&task_id=` | `{items:[LogEntry]}` |
| GET / PUT | `/api/settings` | Settings |
| POST | `/api/shutdown` | `{ok:true}` 后优雅退出本地服务（运行中任务落盘为可续跑状态） |
| GET | `/`、`/static/*` | 前端静态文件 |

### 任务参数

- `unfollow`: `{ "mids": [int], "reason": "rule|manual" }`；服务端强制 `protect_mutual` 过滤互关（若 settings 开启），并要求请求里带 `"ack_risk": true`。
- `dynamic_delete`: `{ "ids": ["str"] }`。
- `favorite_clear`: `{ "folder_id": int, "ids": ["id:type"] }`；需 `confirm_phrase`。
- `favorite_clean_invalid`: `{ "folder_id": int }`；需 `confirm_phrase`。
- `toview_clear`: `{}`；需 `confirm_phrase`。
- `classify_llm`: `{ "mids": [int] }`。

删除类任务必须：先写备份（CSV+JSONL）→ 校验确认词/ack → 入队。

## 模块接口（Python）

### bili/client.py
```python
class BiliClient:
    def __init__(self, store: "Store", settings: Settings, limiter: "RateLimiter"): ...
    def close(self): ...
    def nav(self) -> dict: ...                       # /x/web-interface/nav
    def get(self, path: str, params: dict | None = None, wbi: bool = False) -> dict
    def post(self, path: str, data: dict, wbi: bool = False) -> dict
    # 非 0 code 抛 BiliApiError；风控码抛 RiskControlError（定义在 errors.py）
    # 每次请求前调用 limiter.wait()
```
`wbi=True` 时内部完成 wbi 签名；img/sub key 从 nav 自动刷新。

### bili/auth.py
```python
def qrcode_generate(client) -> dict          # {"url","key","qr_png","expires_in"}
def qrcode_poll(client, key: str) -> dict    # {"status","user"|None}
def fetch_me(client) -> dict                 # User
```
登录成功后由 API 层调用 `store.save_session(cookies)`。

### bili/follows.py
```python
def iter_followings(client) -> Iterator[FollowItem]   # 自动翻页，total 由第一个响应给出
def list_groups(client) -> list[FollowGroup]
def unfollow(client, mid: int) -> None                # act=2
def get_relation(client, mid: int) -> dict
```

### bili/dynamics.py
```python
def iter_dynamics(client, mid: int) -> Iterator[DynamicItem]  # 游标翻页
def delete_dynamic(client, dyn_id: str) -> None
```

### bili/favorites.py
```python
def list_folders(client) -> list[FavFolder]
def iter_items(client, media_id: int) -> Iterator[FavItem]     # 分页；用 /x/v3/fav/resource/list
def batch_delete(client, media_id: int, resources: list[str]) -> dict   # 每批由调用方切 20
def clean_invalid(client, media_id: int) -> None
```

### bili/toview.py
```python
def list_toview(client) -> list[ToviewItem]
def clear_toview(client) -> None
```

### services/ratelimit.py
```python
class RateLimiter:
    def __init__(self, settings): ...
    def wait(self): ...                  # 每条请求前调用：基础间隔+抖动+每批休息
    def on_success(self): ...
    def on_risk(self, code) -> bool: ... # 返回 True 表示应继续冷却重试，False 表示应停止任务
```
内部状态：当前批计数、连续风控次数、cooldown_until。

### services/backup.py
```python
class BackupService:
    def export(self, task_type: str, items: list[dict], user: dict|None) -> dict
    # 返回 {"csv_path":..., "jsonl_path":..., "count":...}；失败抛 BackupError
```

### services/classify.py
```python
CATEGORIES: list[str]                  # 21 类，与 const.py 一致
def classify_rule(item: dict) -> tuple[str, float]
class LlmClassifier:
    def __init__(self, cfg: dict)
    def classify(self, items: list[dict]) -> dict[int, str]
```

### services/task_engine.py
```python
class TaskManager:
    def __init__(self, store, client_factory, limiter_factory, backup, classify, logger): ...
    def start(self); def shutdown(self)
    def create(self, type_: str, params: dict, confirm_phrase: str | None) -> dict
    def list(self) -> list[dict]; def get(self, tid: str) -> dict | None
    def pause(self, tid) -> dict; def resume(self, tid) -> dict; def stop(self, tid) -> dict
```
执行器放 `services/executors.py`，按 `task.type` 分派；每个执行器内部调用 `bili` 模块并定期 `task_engine.update_progress(task, done, failed, message)`。

### mock/
```python
class MockBiliClient:  # 与 BiliClient 同接口（含 auth/follows/dynamics/favorites/toview 所需方法）
    ...
def make_mock_data(seed: int = 42) -> dict
```
`python -m bilicleaner --mock` 时 API 层使用 MockBiliClient + 假用户。

## 前端约定

- 纯静态：`index.html` + `app.js` + `style.css`，禁止 CDN、禁止构建步骤。
- 所有请求走相对路径 `/api/...`。
- 设计语言：Apple 风格（内部设计参考：Apple 风格提示词文档，不随仓库分发）。要点：白底 + `#f5f5f7` 羊皮纸交替；唯一强调色 `#0066cc`；标题 SF Pro/Inter 600 负字距；正文 17px；主按钮黑色 44px 高或蓝色胶囊；分隔用 1px `#e0e0e0` 发丝线；危险操作区用深色 tile（`#272729`）反白；按钮按下 `transform: scale(0.95)`；不使用渐变/卡片阴影/第二个强调色。
- 四个 Tab 页 + 设置页 + 登录页；每个 Tab：标题区（大标题+一句话说明）→ 筛选工具栏 → 列表（分页/加载更多）→ 底部固定操作条（选中数量 + 主按钮）→ 危险操作深色区（清空类）。
- 任务进度用顶部条或右下角浮层，显示 done/total、暂停/继续/停止。

---

## 补充约定（2026-11-02，Lead 确认，前后端共同遵守）

1. **`GET /api/follows` 筛选参数**
   - `category`：传 21 类的中文名；传 `__none__` 表示"未分类"；不传表示全部。
   - `group`：传 tagid（数字），不传表示全部；`group=0` 表示未分组。
   - `mutual=1` 只看互关，`mutual=0` 只看非互关。
2. **新增 `GET /api/follows/stats`**：返回 `{ "total": int, "mutual": int, "uncategorized": int, "categories": {"游戏": 12, ...} }`，用于分类统计条（全量统计，不是"已加载"）。
3. **`GET /api/dynamics` 增加日期筛选**：`date_from=YYYY-MM-DD`、`date_to=YYYY-MM-DD`（含端点），后端按 `pub_ts` 过滤；不传不筛。
4. **`favorite_clear` 的 ids 语义**：
   - `ids` 为空数组/缺省 → 清空整个 `folder_id`（必须带确认词，执行器自行分页枚举全部条目并先备份）；
   - `ids` 非空 → 只取消勾选的条目（备份这批条目，不需要手打确认词，但 `params.ack_risk=true` 仍必填）。
5. **`ack_risk` 放在任务 `params` 里**（如 `{"mids":[...], "reason":"manual", "ack_risk":true}`），不在 body 顶层。
6. **设置里的备份目录**：MVP 固定在 `<data_dir>/backups`，设置页只展示真实路径，不提供目录选择。
