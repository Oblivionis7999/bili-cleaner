# BiliCleaner 架构

写这篇是为了让后来改代码的人（包括三个月后的我）不用先读一遍全部源码，就能知道东西是怎么拼起来的、哪些地方不能乱动。

模块边界以 [`docs/API-CONTRACT.md`](API-CONTRACT.md) 为准，策略细节来自 [`docs/adr/`](adr/)。本文描述 v0.1 的模块切分和已经落地的行为；模拟数据全链路已验证，真实账号也做过一次小规模冒烟（只读 7/7 + 取关/删动态/清稍后再看）。文档和代码打架时，先改契约，再改代码，最后回来改这篇。

## 1. 一句话

浏览器里的静态页面 → 本机 FastAPI 的 `/api/*` → 任务引擎（唯一的写者线程）→ B 站接口层 → B 站；所有状态落本机文件，不引数据库。

## 2. 进程与线程模型

只有一个进程（uvicorn 托管 FastAPI），只监听 `127.0.0.1`。没有守护进程、没有系统服务、没有托盘。

```text
主线程        uvicorn / FastAPI，处理 HTTP 路由；同步路由跑在 threadpool
写线程        1 个，TaskManager 持有；从 queue.Queue 取任务，串行发写请求
读请求        在 threadpool 里调 bili 读接口；和任务共用同一把限速器，
              任务冷却时读请求一起等；读请求自己撞风控会返回 429
限速器        全局一个实例；每条 bili 请求前 wait()，读写共享冷却状态
```

为什么写操作要单独一个线程：B 站没有批量接口，一次任务可能持续一两个小时，如果把写请求放在 HTTP 请求线程里，页面关掉任务就断了；而且多个请求线程同时发写请求等于自己给自己叠风控。所以写操作只有一个出口，串行。

读写共用同一把限速器是有意的：任务因为风控进入冷却时，翻列表、查分组这些读请求也一起停下来，避免"一边等冷却一边把账号又点着"。反过来说，读请求撞上风控不会被当成 500，API 会返回 `429 RATE_LIMITED` 让前端提示过一会儿再试。

**启动时会自动接上上次的进度。** `TaskManager.start()` 会把持久化任务里状态是 `running` / `cooldown` 的改成 `queued` 重新排队，message 写"检测到上次未完成，从断点继续"；用户主动暂停的任务保持 `paused`，不会自己动。也就是说关掉程序不等于放弃任务，下次打开它会接着跑。不想继续的，在它真正开跑之前点停止。

**退出程序**走 `POST /api/shutdown`：路由先返回 `{ok:true}`，后台线程延迟 0.2 秒、调 `TaskManager.shutdown()` 把运行中的任务中断成 `queued` 落盘，再让 uvicorn 优雅关停（拿不到 server 实例时用 SIGINT，另加 3 秒 `os._exit` 保险丝）。前端左下角红色按钮就是这个入口。

## 3. 模块图

```text
┌────────────────────────── 浏览器 ──────────────────────────┐
│ web/   index.html + app.js + style.css                     │
│        纯静态、无构建、无 CDN；请求全部走相对路径 /api/*     │
└─────────────────────────────┬──────────────────────────────┘
                              │ HTTP (只走 127.0.0.1)
┌─────────────────────────────▼──────────────────────────────┐
│ api/    FastAPI 应用，create_app(store, mock=False)        │
│   routes_auth   扫码登录、/api/me、退出                     │
│   routes_data   关注/动态/收藏/稍后再看 的只读列表与筛选     │
│   routes_tasks  创建任务、进度、暂停/继续/停止               │
│   routes_misc   health / settings / backups / logs          │
│   错误处理      401 NOT_LOGGED_IN / 429 风控 / 4xx·5xx → JSON │
└───────┬──────────────────────────────────────┬─────────────┘
        │ 读：调 bili 读接口（过限速器）        │ 写：只调 TaskManager.create()
        │                                      │
        ▼                                      ▼
┌───────────────┐                   ┌──────────────────────────┐
│ store.py      │                   │ services/task_engine.py  │
│ 数据目录       │◄─────────────────►│  TaskManager             │
│ session.dat   │   任务/日志读写    │  · 单写者线程 + queue     │
│ settings.json │                   │  · 状态机、暂停/继续/停止 │
│ tasks.json    │                   │  · 冷却、断点续跑         │
│ logs/         │                   └──────────┬───────────────┘
│ backups/      │                              │ 按 task.type 分派
└───────────────┘                              ▼
                                   ┌──────────────────────────────────┐
                                   │ services/executors.py            │
                                   │  unfollow / dynamic_delete /     │
                                   │  favorite_clear / clean_invalid /│
                                   │  toview_clear / classify_llm     │
                                   └──┬───────────┬──────────┬────────┘
                                      │           │          │
                        ┌─────────────▼──┐ ┌──────▼───────┐ ┌▼─────────────┐
                        │ ratelimit.py   │ │ backup.py    │ │ classify.py   │
                        │ 间隔/抖动/批休息│ │ CSV + JSONL  │ │ 规则 + 可选 LLM│
                        │ 冷却计次       │ │ 失败即中止    │ │               │
                        └───────┬────────┘ └──────────────┘ └──────────────┘
                                │ wait()
                                ▼
┌────────────────────────────────────────────────────────────┐
│ bili/  接口层（requests.Session，桌面 UA，按接口设 Referer） │
│   client.py   统一 get/post、wbi 签名、cookie 注入、错误映射│
│   wbi.py      nav 取 img_key/sub_key + mixinKeyEncTab + md5 │
│   auth.py     qrcode_generate / qrcode_poll / fetch_me      │
│   follows.py  翻页取关注、分组、取关（act=2）、查关系        │
│   dynamics.py 游标翻页取动态、删动态                        │
│   favorites.py 文件夹、条目翻页、批量取消收藏、清失效        │
│   toview.py   稍后再看列表、一键清空                         │
└─────────────────────────────┬──────────────────────────────┘
                              │ HTTPS
                              ▼
                         B 站 web 接口
```

依赖方向是单向的：`web → api → services → bili → requests`，`store` 被各层读取但不反向依赖它们。`bili/` 不知道任务、备份、前端的存在。`mock/` 只替换 `bili` 客户端，其他层代码不动。

## 4. 模块职责

| 模块 | 职责 | 不负责 | 状态 |
|---|---|---|---|
| `__main__.py` | 解析参数、找工作端口的空闲端口、起 uvicorn、开浏览器 | 业务逻辑 | 已实现 |
| `const.py` | 分类名、任务类型/状态、默认设置、风控码/登录码归类 | 运行时状态 | 已实现 |
| `store.py` | 数据目录、DPAPI 登录态、设置、任务列表、操作日志、备份目录 | 业务判断 | 已实现 |
| `types.py` | 任务 dict 的构造与时间戳 | 持久化 | 已实现 |
| `errors.py` | 异常层级与错误码 | 重试策略 | 已实现 |
| `bili/client.py` | 会话、UA/Referer、wbi、统一错误映射 | 限速、任务、备份 | 已实现 |
| `bili/*.py` | 各资源的读接口与单条写接口，分页用生成器 | 批量编排 | 已实现 |
| `services/ratelimit.py` | 请求间隔、抖动、批休息、风控冷却计次 | 决定任务成败 | 已实现 |
| `services/backup.py` | 删除前导出 CSV + JSONL + meta | 恢复 | 已实现 |
| `services/classify.py` | 21 类规则分类、可选 LLM 分类（失败降级规则） | 删改账号数据 | 已实现 |
| `services/task_engine.py` | 任务队列、状态机、暂停/继续/停止、冷却、进度落盘 | 具体怎么删（交给执行器） | 已实现 |
| `services/executors.py` | 按任务类型取数据、逐条/分批调用 bili、更新进度 | HTTP 层 | 已实现 |
| `api/*` | 路由、参数校验、错误响应、静态文件挂载 | 直接发写请求 | 已实现 |
| `web/*` | 四个 Tab、登录页、设置页、日志页、任务浮层 | 业务规则 | 已实现 |
| `mock/*` | 假客户端同接口替身、大量中文假数据、可注入假风控 | 被生产代码依赖 | 已实现 |

状态里的"已实现"指模拟数据下跑通、有测试覆盖。`bili/*` 在作者自己的真实账号上做过一次只读 + 小规模写冒烟（v0.1.3 发布前），但 B 站接口可能变，仍然建议用自己的账号按 [`docs/SMOKE-TEST.md`](SMOKE-TEST.md) 先跑一遍。

## 5. 一次取关任务的完整路径

拿"批量取关"举例，因为它的链条最长：

```text
1. 前端勾选 300 个 mid，POST /api/tasks
   {"type": "unfollow", "params": {"mids": [...], "reason": "manual", "ack_risk": true}}
2. TaskManager.create()
   · 校验任务类型和参数：unfollow 必须带 params.ack_risk（没有就拒绝，前端要用户勾
     "我已了解取关后无法恢复原关注顺序"）；清空类必须带 confirm_phrase
   · 写 tasks.json：status=queued，total 先按请求里的 mids 数量算
3. 写线程取出任务 → running
   · 执行器先拉一遍关注列表，按 `protect_mutual` 把互关从待处理目标里剔除（跳过数量写进任务 message）
   · BackupService.export("unfollow", 待取关对象, user)
     → backups/<mid>/<时间戳>/unfollow.csv + unfollow.jsonl + meta.json
     → 目录路径写进 task.backup_path；备份失败 → failed，结束
   · 再用 succeeded_keys() 读任务流水，跳过已经成功的对象（续跑场景）
4. 逐条循环（串行）：
   · limiter.wait()        2.5s + 随机 0.8s；每 20 条多歇 3 秒；每 300 条强制歇 10 分钟
   · bili.follows.unfollow(mid)
   · 成功 → done += 1；失败 → failed += 1（记日志，继续下一条；连续失败 10 次则任务中止）
   · 每处理一条往 task_ops/<task_id>.jsonl 追加一行 {key, ok, code, msg, ts}
   · 定期把 done/failed/updated_at 落盘 tasks.json
5. 全部处理完 → status=done（done + failed = total）
   撞风控 → cooldown；用户点停止 → stopped
```

读列表（比如翻关注列表）走 `routes_data` 直接调 `bili`，不经过 `TaskManager`，但一样要过限速器。所以任务冷却时，翻列表也会一起等——这是有意的：冷却期间少碰接口。读请求自己撞上风控时，API 返回 429，前端提示过一会儿再试，不会重试到把账号点着。

## 6. 任务状态机

状态取自 `const.TASK_STATUS`：

```text
                 create()
                    │
                    ▼
              ┌──────────┐   写线程取出
              │  queued  │──────────────┐
              └──────────┘              │
                                        ▼
        pause()                    ┌──────────┐   stop()   ┌──────────┐
              ┌───────────────────►│ running  │───────────►│ stopped  │
              │                    └────┬─────┘            └──────────┘
              │                         │
        ┌─────┴─────┐                   │ 全部处理完
        │  paused   │◄── resume() ──────┤
        └───────────┘                   ▼
                                    ┌──────┐
                                    │ done │
                                    └──────┘
                                        ▲
                    风控码（-352/-412    │ 备份失败 / 登录失效
                    /-509/-799）         │ / 不可恢复的错误
                    ┌──────────┐         │
                    │ cooldown │         ▼
                    └────┬─────┘    ┌────────┐
                         │          │ failed │
          到点重试同一批 │          └────────┘
          连续 2 次不过   │
                         ▼
                    ┌──────────┐
                    │ stopped  │  ← 提示"今天别继续了"
                    └──────────┘
```

| 从 | 到 | 触发 | 说明 |
|---|---|---|---|
| `queued` | `running` | 写线程取出队列头部 | 同一时刻最多一个任务在这个状态 |
| `running` | `paused` | 用户点暂停 | 当前这条请求做完才停，不会拦腰截断请求 |
| `paused` | `running` | 用户点继续 | 从下一个对象接着处理 |
| `running` | `cooldown` | 收到 `-352/-412/-509/-799` | 写 `cooldown_until = now + 600s`，任务挂起等 |
| `cooldown` | `running` | 到达 `cooldown_until` | 重试同一批；冷却计数 +1，成功后清零 |
| `cooldown` | `stopped` | 连续冷却 ≥ `max_cooldowns`（默认 2） | 主动放弃，提示换时间再来 |
| `running` | `done` | 待处理对象全部走完 | `done + failed = total`，不是"全成功" |
| `running` | `stopped` | 用户点停止 | 已执行的写操作不回滚 |
| `running` | `failed` | 备份失败、登录失效、执行器无法继续的错误 | 停止执行，保留 `message` |
| `paused` | `stopped` | 用户点停止 | 同 `running → stopped` |
| `running` / `cooldown` | `queued` | 程序重启后 `TaskManager.start()` | 自动转成排队，从断点继续；`paused` 不受影响 |

几个约定：

- `done` 只表示"跑完了"，失败条数看 `failed` 字段和日志。别在前端把 `done` 翻译成"全部成功"。
- `stopped` 和 `failed` 都是终态，不会再自己动。重启后想接着跑，得用户手动点继续。
- **停止优先**：执行器跑的过程里用户点了停止，最终状态一定是 `stopped`，哪怕执行器同时抛了别的异常。
- 所有状态变化都会更新 `updated_at`；`cooldown_until` 只在冷却时有值，其他状态是 `null`。
- 任务列表落盘在 `tasks.json`，进程重启后读回来。终态任务保留着，方便回看删了什么。

## 7. 错误码策略

B 站接口非 0 `code` 一律抛异常，由 `bili/client.py` 统一映射，执行器按异常类型决定"继续 / 冷却 / 停"。错误码归类写在 `const.py`：

```python
RISK_CODES = {-352, -412, -509, -799}
AUTH_CODES = {-101, -111}
```

| code | 常见含义 | 映射 | 任务怎么处理 | 用户看到 |
|---|---|---|---|---|
| `-101` | 账号未登录 | `AuthRequiredError`（`NOT_LOGGED_IN`） | 任务直接 `failed`，不重试 | HTTP 401；界面提示重新扫码 |
| `-111` | CSRF 校验失败（`bili_jct` 不对或缺失） | `AuthRequiredError` | 同上，重新登录会拿到新的 `bili_jct` | 同 `-101` |
| `-352` | 风控校验失败 | `RiskControlError` | 冷却 600 秒后重试同一批；连续 2 次 → `stopped` | 任务条显示"冷却中"和倒计时 |
| `-412` | 请求被拦截（常是 IP 级） | `RiskControlError` | 冷却；换 cookie 没用，冷却后还不行就停 | 冷却提示，建议换网络或明天再来 |
| `-509` | 超出限制 / 被限流 | `RiskControlError` | 冷却 600 秒后重试 | 冷却倒计时 |
| `-799` | 请求过于频繁 | `RiskControlError` | 冷却 600 秒后重试 | 冷却倒计时 |
| 其他非 0 | 业务错误：视频已失效、无权限、对象不存在 | `BiliApiError` | 这一条 `failed += 1`，继续处理下一条；连续失败 10 次（`MAX_FAIL_STREAK`）则任务 `failed`，避免空转 | 任务结束后看失败数和日志 |
| 网络超时 / 连接失败 | 断网、DNS、被防火墙拦 | `BiliApiError(-999)` / 内部异常 | 同普通业务错误：单条失败计数、继续，连续 10 次失败任务中止 | 日志 |

原则：

- **风控不向上抛 500。** 任务内部遇到风控就进 `cooldown`，任务接口照常返回任务对象；读接口遇到风控返回 `429 RATE_LIMITED`。只有登录失效会返回 401。
- **单条业务错误不拖垮整个任务。** 收藏夹里失效视频、已注销的 UP 主很常见，逐条记失败、继续跑完，比一个错误停下更实用。
- **登录失效不偷偷重试。** 重试只会把一次失败变成一堆失败，让用户重新扫码更省事。
- **不认识的错误码不猜。** 当普通业务错误处理、记日志；如果一整批都是同一个码，执行器按连续失败处理并结束任务。

## 8. 备份机制

规则来自 [`docs/adr/0003-backup.md`](adr/0003-backup.md)：删除之前必须备份，备份失败就不删。

- **时机**：删除类任务（取关、删动态、清收藏、清失效、清稍后再看）在发出第一个写请求之前。LLM 分类不改账号数据，但执行器也会把参与分类的关注对象导一份，当作"当时长什么样"的记录。
- **产物**：同一批数据导出三个文件，放在 `backups/<账号 mid>/<YYYYmmdd-HHMMSS>/`
  - `.csv`：人看。标题 / UP 主 / 链接 / 时间这类列，用 Excel 能直接打开。
  - `.jsonl`：机器读。接口返回的原始字段，一行一个对象，方便以后写脚本处理。
  - `meta.json`：任务 id、任务类型、条数、创建时间、账号 mid 和昵称、文件清单，给 `/api/backups` 回显用。
- **失败处理**：`BackupService.export()` 抛 `BackupError` → 任务 `failed`，一个写请求都不发。没有"跳过备份继续删"的开关。
- **保留策略**：`settings.backup_keep`，默认 `0` 表示全部保留。设成 N 只留最近 N 次，清理在 `BackupService._prune()` 里做，删之前会校验目标路径确实在 `backups/` 下面。
- **边界**：备份是"记录"，不是"快照恢复"。B 站侧删了就是删了，工具不提供还原按钮，也不会假装能还原。
- **提醒**：备份目录里有你的收藏标题和关注列表，别整个目录传到网盘或 git 仓库。

## 9. 断点续跑

方案落在三个文件上：

```text
tasks.json                      任务本身：状态、总数、done/failed、backup_path、params
task_ops/<task_id>.jsonl        对象级流水：{"key", "ok", "code", "msg", "ts"} 一行一个
logs/operations.jsonl           应用日志（info / warn / error），排障用
```

恢复流程：

1. 进程启动，`store.load_tasks()` 读回任务列表。状态是 `running` / `cooldown` 的改成 `queued` 重新排队，`paused` 保持不变，终态的不动。
2. 写线程取出 `queued`，状态转 `running`。
3. 执行器调 `succeeded_keys(task)` 读 `task_ops/<task_id>.jsonl` 里 `ok=true` 的 key 集合，跳过这些对象。
4. 从第一个没处理过的对象继续，每处理一个就 `append_op()` 追加一行、定期落盘进度。

这套机制已经落在 `TaskManager` 和执行器里（`_recover_locked` / `succeeded_keys` / `append_op`），`--mock` 模式下能跑通；真实账号上跑过取关 6 个、删动态 1 条、清稍后再看 4 条的成功路径，但"任务跑到一半强制重启"的恢复场景没在真实账号上故意验证过，边界以 `services/task_engine.py` 和 `services/executors.py` 的实现为准。

两个诚实的限制：

- **不是事务。** 如果进程正好在"请求已经发出去、流水还没落盘"的间隙崩溃，重启后可能重试一条其实已经成功的对象。取关、取消收藏这类操作重复执行一般无害，最坏多一条失败记录。
- **依赖接口的稳定。** 如果 B 站改了字段名，旧备份和旧流水可能对不上。断点续跑只保证"同一次任务内"的连续性，跨版本不承诺。

## 10. 目录结构

仓库：

```text
bili-cleaner/
├─ pyproject.toml         依赖、入口点、pytest 配置
├─ LICENSE                MIT
├─ README.md
├─ CONTRIBUTING.md
├─ SECURITY.md
├─ GLOSSARY.md            术语表（写代码/写文档前先对一下用词）
├─ docs/
│  ├─ API-CONTRACT.md     接口契约：字段名、路由、模块签名
│  ├─ ARCHITECTURE.md     本文
│  ├─ SMOKE-TEST.md       真实账号冒烟测试手册
│  ├─ screenshots/        README / 文档用截图
│  └─ adr/                0001 技术栈 / 0002 风控 / 0003 备份
├─ src/bilicleaner/
│  ├─ __main__.py
│  ├─ const.py  errors.py  store.py  types.py
│  ├─ bili/               接口层
│  ├─ services/           限速、备份、分类、任务引擎、执行器
│  ├─ api/                FastAPI 路由与 app 组装
│  ├─ web/                index.html + app.js + style.css
│  └─ mock/               假客户端与假数据
├─ tests/                 pytest，全部 mock，不连 B 站
├─ scripts/               dev.ps1 / build_exe.ps1 / pyinstaller_entry.py / smoke_real.ps1
├─ .github/workflows/     ci.yml：Windows 上装依赖跑 pytest
└─ dist/                  打包产物（BiliCleaner.exe，不进 git）
```

运行时数据目录（默认 `%APPDATA%\BiliCleaner`，可用 `--data-dir` 或 `BILICLEANER_DATA` 覆盖，非 Windows 退化到 `~/.bilicleaner`）：

```text
BiliCleaner/
├─ session.dat             登录 Cookie，DPAPI 加密
├─ settings.json           设置（LLM api_key 目前明文，见 SECURITY.md）
├─ tasks.json              任务列表与进度
├─ categories_<mid>.json   分类结果缓存（<mid> 是当前登录账号）
├─ logs/
│  └─ operations.jsonl     应用日志（info / warn / error），排障用
├─ task_ops/
│  └─ <task_id>.jsonl      对象级流水，断点续跑的依据
└─ backups/
   └─ <账号 mid>/
      └─ <YYYYmmdd-HHMMSS>/
         ├─ <任务类型>.csv
         ├─ <任务类型>.jsonl
         └─ meta.json      任务 id、类型、条数、创建时间、文件清单
```

`store.py` 里已经保证：写 JSON 用"临时文件 + `os.replace`"，避免进程被杀时留下半截文件；数据目录在启动时创建；`session.dat` 有 `dpapi:v1:` 前缀，非 Windows 退化成 `plain:v1:` + base64（只用于开发，会打警告）。

## 11. 不能破的几条线

1. **只有一个写者。** 同一时刻最多一个 `running` 的写任务，写请求只能从 `TaskManager` 的写线程发出。API 层（含 `routes_data`）不许直接调 `bili` 的写方法。
2. **先备份后删除。** 删除类任务在备份成功前不发写请求。备份失败 = 任务失败。
3. **不绕风控。** 不加代理池、不加并发、不伪装 UA 节奏。要改限速，只能往更保守的方向改。
4. **状态落盘。** 任何影响用户判断的状态（任务进度、备份路径、冷却截止时间）都得写进文件，不能只活在内存里。
5. **出错宁可停。** 不认识的错误码、对不上的数据、无法判断的关系类型，按失败处理并写日志，不要猜一个分支继续跑。

## 12. mock 与测试

- `python -m bilicleaner --mock` 时，`api/app.py` 用 `MockBiliClient` 注入，所有读接口返回固定假数据（几千条关注、动态、收藏、稍后再看），写操作返回成功或按开关注入假风控，用来联调和截图。
- `MockBiliClient` 必须和 `BiliClient` 同接口——这是 mock 能替身的前提，接口变了同步改。
- 测试分四层：`test_bili`（mock requests 响应）、`test_services`（限速/备份/分类/任务引擎）、`test_api`（`TestClient` + mock 模式）、`test_store`/`test_mock`。现在 184 条全绿（bili 76 + services 38 + api 14 + mock/store 56），CI 在 Windows 的 Python 3.12/3.13 上跑 `pytest`。
- 测试用例里不允许出现真实账号数据；需要样例就自己造，写死几个假 mid 和中文昵称就够了。

## 13. 验证状态（v0.1.3）

已经验证过的：

| 项目 | 结果 |
|---|---|
| 单元 / 集成测试 | 185 passed：bili 76 + services 38 + api 15 + mock/store 56 |
| mock 端到端 | `/api` 全部路由返回 200；六种任务类型全部跑到 `done`；备份 CSV / JSONL / meta 三件套都正常生成 |
| 打包 | PyInstaller 单文件 `dist\BiliCleaner.exe`，23.8 MB；exe 的 mock 模式能起服务，静态资源正常加载 |
| 前端 | 桌面和移动端截图见 `docs/screenshots/`；纯静态、无构建、无 CDN |
| 真实账号冒烟 | 2026-11-02：只读 7/7 通过；真实写任务取关 6、删动态 1、清稍后再看 4，全部 done，备份与 task_ops 日志正常 |

**没有**验证过的：

- 大规模真实操作：真实账号上只做过小规模冒烟（几十个对象级别），没拿几千条压过风控阈值；冷却后的自动恢复、长时间断点续跑也没在真号上故意验证过。
- 接口漂移：B 站改接口时 `bili/*` 可能失效，需要人工按 [`docs/SMOKE-TEST.md`](SMOKE-TEST.md) 复测。
- macOS / Linux（存储层会退化成 base64，只适合开发调试）。
