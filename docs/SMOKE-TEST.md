# 真实账号冒烟测试

这份文档对应 `scripts/smoke_real.ps1`：在**你自己的真实 B 站账号**上，对本地 BiliCleaner 服务做一次小范围冒烟，确认接口层、任务引擎、登录态、限速与备份都能正常工作。

它和开发阶段的测试是两回事：

| | 开发/CI 测试 | 真实账号冒烟 |
|---|---|---|
| 数据 | mock（`--mock`） | 真实 B 站账号 |
| 会不会发写请求 | 不会 | 默认不会，只有 `-IncludeWriteTest` 且你输入 `y` 才会 |
| 目的 | 逻辑回归 | 验证真实接口没变、风控策略可用、登录态能持久化 |

> 子代理/CI 阶段不要跑真实冒烟。真实冒烟由人来做，**建议用测试号，或者至少用你愿意承担风险的小号**。

---

## 1. 前置条件

- Windows 10/11，PowerShell 5.1 或 PowerShell 7（`pwsh`）均可。
- 服务是**真实模式**启动的，不能带 `--mock`：
  ```powershell
  # 在项目根目录（源码方式）
  python -m bilicleaner --no-browser --port 8770

  # 或者 exe / pipx 安装方式
  bilicleaner --no-browser --port 8770
  ```
- 浏览器打开 <http://127.0.0.1:8770>，用 B 站 App 扫码登录。登录态会用 Windows DPAPI 加密后存在 `%APPDATA%\BiliCleaner\session.dat`，下次启动不用重新扫码。
- 只读冒烟要求当前账号**已登录**；未登录会直接退出（退出码 3）。
- 脚本只访问 `-BaseUrl` 指向的本地地址，默认拒绝非本机地址，避免把登录 Cookie 发到别处。

---

## 2. 只读冒烟（默认行为）

```powershell
cd <项目根目录>
.\scripts\smoke_real.ps1
```

指定端口：

```powershell
.\scripts\smoke_real.ps1 -BaseUrl http://127.0.0.1:8771
```

脚本会依次请求下面这些**只读**接口，打印每项状态码/条数/耗时，最后汇总：

| 检查项 | 含义 | 正常表现 | 备注 |
|---|---|---|---|
| `GET /api/health` | 服务存活 | 200，`ok=true`，`mock=false` | 几十毫秒；`mock=true` 时脚本会拒绝运行 |
| `GET /api/me` | 登录态 | 200，`logged_in=true`，带 `user.mid/name` | 登录后首次可能秒级（要请求一次 nav） |
| `GET /api/follows?offset=0&limit=50` | 关注第一页 | 200，`total` 与 B 站关注页一致，本页 50 条 | **首次会全量翻页**：2000 关注≈40+ 次请求，按默认限速约 2–3 分钟；之后缓存 120 秒 |
| `GET /api/follows/groups` | 关注分组 | 200，分组数与 B 站一致（含默认分组/特别关注） | 秒级 |
| `GET /api/dynamics?offset=&limit=30` | 动态第一页 | 200，本页约 20–30 条，`has_more=true` | 首次同样要全量翻页（300 条≈15 页） |
| `GET /api/favorites/folders` | 收藏夹列表 | 200，数量与 B 站一致（含默认收藏夹） | 秒级 |
| `GET /api/toview?offset=0&limit=50` | 稍后再看第一页 | 200，`total` 与 App 一致，本页 50 条 | 秒级 |

预期输出示例（数字是示例，实际以你的账号为准）：

```text
BiliCleaner 真实账号冒烟测试
服务地址：http://127.0.0.1:8770
默认只读；写操作需要 -IncludeWriteTest，且只会操作一个显式指定的对象。

== 基础连通性
[ OK ] /api/health                        32 ms  version=0.1.0 mock=False
[ OK ] /api/me                          1204 ms  mid=123456789 name=示例用户

== 只读接口（第一页 / 列表）
[ OK ] /api/follows                    118042 ms  total=2000，本页 50 条；首条：xxx（mid=xxx）
[ OK ] /api/follows/groups              2610 ms  分组 8 个：特别关注、默认分组、学习、科技
[ OK ] /api/dynamics                    41108 ms  本页 30 条，has_more=True，类型：转发 / 图文 / 视频；首条：图文「…」
[ OK ] /api/favorites/folders           2588 ms  收藏夹 2 个：默认收藏夹(1798)、学习资料(24)
[ OK ] /api/toview                       2596 ms  total=875，本页 50 条；首条：…

== 只读冒烟结果
通过 7/7 项，总耗时 166.3 秒

只读冒烟完成，未修改任何数据。
写测试请加 -IncludeWriteTest 并显式指定单个对象，风险与回滚见 docs/SMOKE-TEST.md。
```

几点说明：

- **首次只读冒烟很慢是正常的**。服务端会在任务/接口里遵守限速（默认每条请求间隔 2.5s ± 0.8s 抖动，每 20 条休息 3s），关注/动态的首次全量拉取要几分钟；之后有 120 秒缓存，重跑会快很多。
- 如果 `total` 和你在 B 站 App 里看到的不完全一致，通常是缓存/刚操作过导致的延迟，等 2 分钟再跑一次。
- 某个接口 401：登录态失效，重新扫码。
- 某个接口 429 / 返回 `RISK_CONTROL`：触发了 B 站风控，等几小时再试，别连续跑。
- 某个接口 502：B 站接口报错（或返回码非 0），查 `%APPDATA%\BiliCleaner\logs\operations.jsonl`。

---

## 3. 写测试（可选，真实写操作，不可撤销）

默认**不会**发任何写请求。只有显式加 `-IncludeWriteTest`，并且执行前在终端输入 `y` 才真正执行；一次只允许操作**一个**对象。

### 3.1 取关一个 UP 主

```powershell
.\scripts\smoke_real.ps1 -IncludeWriteTest -UnfollowMid 12345678
```

`-UnfollowMid` 就是 UP 主 UID。获取方式：

- 打开 UP 主空间页，URL 形如 `https://space.bilibili.com/12345678`，最后那段数字就是；
- 或者调用本机服务搜索：`Invoke-RestMethod "http://127.0.0.1:8770/api/follows?q=昵称"`，返回里的 `mid`；
- 或者看你之前导出的备份 CSV（`UID` 列）。

脚本会先全量扫描关注列表找到这个 mid，打印昵称/签名/认证/分组/互关状态/关注时间/空间页，然后问你是否确认。**互关对象**会额外警告：设置里 `protect_mutual=true` 时任务会跳过它，不会真的取关。

### 3.2 取消收藏一个条目

```powershell
.\scripts\smoke_real.ps1 -IncludeWriteTest -FavFolderId 916673043 -UnfavResource "117528844047788:2"
```

- `-FavFolderId`：收藏夹的 `media_id`。打开 `Invoke-RestMethod http://127.0.0.1:8770/api/favorites/folders`，返回里的 `media_id`；
- `-UnfavResource`：条目的 `id:type`。打开 `Invoke-RestMethod "http://127.0.0.1:8770/api/favorites/916673043/items?limit=20"`，把某条的 `id` 和 `type` 用冒号拼起来；备份 JSONL 里也有这两个字段。

脚本会打印标题/UP 主/bvid/链接/收藏时间（失效内容会额外提示），确认后才执行。

> 建议：先往收藏夹里收藏一个**你自己的测试视频**，再用这个测试视频做取消收藏冒烟。不要拿舍不得的视频试。

### 3.3 写测试背后发生了什么

1. 脚本先做完全部只读检查；有失败项会直接退出，不会继续写。
2. 打印目标对象 → 等待你输入 `y`（其他输入 = 取消，退出码 4，不发任何写请求）。
3. 通过 `POST /api/tasks` 创建任务，由后台**单写者线程**串行执行：
   - 先强制备份（CSV 给人看 + JSONL 全字段）到 `%APPDATA%\BiliCleaner\backups\<账号mid>\<时间戳>\`；**备份失败任务直接失败，不会继续删**；
   - 再发 1 条写请求（取关走 `act=2`；取消收藏走 `batch-del`，单条资源）；
   - 每个对象的操作结果写一行操作日志，供断点续跑与排障。
4. 脚本轮询任务状态直到 `done / failed / stopped`，打印结果与备份路径；超时会提示你到 Web UI 查看（退出码 5）。
5. 写请求同样走限速；遇到风控码（`-352 / -412 / -509 / -799`）任务进入 `cooldown`（默认 600 秒，最多 2 次），脚本会提示冷却并继续等；连续风控则任务 `stopped`。

---

## 4. 风险与回滚

B 站侧的取关/取消收藏**不可撤销**。脚本能保证的只有：对象是显式指定的单个、执行前二次确认、执行前有备份、失败时不会扩大范围。

| 操作 | 影响 | 回滚方式 | 回滚后的代价 |
|---|---|---|---|
| 取关 | 解除关注关系 | 打开 `https://space.bilibili.com/<mid>` 点“关注” | 关注顺序、自定义分组不会恢复；关注时间 `mtime` 变成当前时间；短时间频繁取关/关注可能触发 B 站安全验证或风控 |
| 取消收藏 | 从收藏夹移除条目 | 打开视频页点“收藏”，手动选回原收藏夹 | 收藏时间重置为当前时间；若视频已失效（`invalid=true`）则无需也无法恢复 |

备份位置：

```
%APPDATA%\BiliCleaner\backups\<账号mid>\<时间戳>\
  <task_type>.csv     # 人读：标题/UP主/链接/时间等
  <task_type>.jsonl   # 完整字段，供以后写脚本用
```

任务接口返回的 `backup_path` 就是这次任务的实际目录。确认不再需要回滚之前，别删备份。

如果写测试后想确认状态：`GET /api/tasks` 看任务记录，或重新跑一遍只读冒烟看总数是否变化。

---

## 5. 注意事项（请务必看完）

- **不要在高峰期跑**：晚上 8–11 点、周末、新番更新后是 B 站风控最敏感的时候；工作日上午最稳。
- **不要用主号/大号试探写操作**：先用测试号或小号。取关是真实生效的。
- **不要连续重复跑写测试**：一小时内能做 1–2 次单条写就已经足够验证链路；连续批量操作才需要任务引擎，而那是 UI 上让用户自己确认的事。
- **不要对 `--mock` 服务跑真实冒烟**：脚本检测到 `mock=true` 会拒绝（`-AllowMock` 只用于验证脚本本身能跑通）。
- **不要把 `-BaseUrl` 指到公网**：脚本默认只允许 `127.0.0.1`/`localhost`；登录 Cookie 只应发往本机服务。
- 本脚本**不测试**批量清空类操作（清空收藏夹 / 清理失效内容 / 清空稍后再看 / 批量删动态）。这些是真正的不可逆批量删除，即使有备份也不建议拿来“冒烟”，让用户在 Web UI 里自己确认。
- 所有日志都在本地，不上传；操作日志在 `%APPDATA%\BiliCleaner\logs\operations.jsonl`。

---

## 6. 退出码与排错

| 退出码 | 含义 | 怎么办 |
|---|---|---|
| 0 | 全部检查通过 | 正常 |
| 1 | 有只读检查失败 | 看失败项详情：401 → 重新扫码；429 → 等风控过去；502 → 看服务端日志 |
| 2 | 服务未启动 / 端口不对 / mock 模式 / 地址不是本机 | 按提示启动真实模式服务，或检查 `-BaseUrl` |
| 3 | 未登录 | 浏览器打开服务地址扫码登录后重跑 |
| 4 | 写测试被取消，或写任务失败/参数不合法 | 看输出里的任务 `message` 与备份路径；参数错误按提示修正 |
| 5 | 等待任务超时 | 任务可能仍在跑或在冷却：打开 Web UI 任务列表查看，别重复提交 |

常见问题：

- **连接失败**：确认服务进程还在、端口没被占；默认端口 8770，被占时 `__main__` 会自动顺延，注意看启动日志里的实际地址。
- **`/api/me` 显示未登录但浏览器里已经登录**：扫码是在浏览器里对本地服务做的；如果你重启过服务又清了数据目录，需要重新扫码。
- **任务一直 `cooldown`**：风控冷却中（默认 10 分钟），脚本会提示；冷却完仍失败就今天先别跑了。
- **任务 `failed`**：先看 `/api/tasks/<id>` 的 `message`，再看 `logs/operations.jsonl` 和任务的 `task_ops/<task_id>.jsonl`（记录每个对象成功/失败）。
- **找不到 `-UnfollowMid` 指定的对象**：可能已经取关，或 mid 写错；脚本会扫描关注列表前 4000 条（20 页 × 200），确认未找到会放弃且不发写请求。
- **找不到 `-UnfavResource` 指定的资源**：确认它还在该收藏夹里（可能已被删除/移到别的夹子）。

---

## 7. 手动替代（不想跑脚本时）

只读检查也可以直接用 PowerShell 单发请求：

```powershell
Invoke-RestMethod http://127.0.0.1:8770/api/health
Invoke-RestMethod http://127.0.0.1:8770/api/me
Invoke-RestMethod "http://127.0.0.1:8770/api/follows?offset=0&limit=50"
Invoke-RestMethod "http://127.0.0.1:8770/api/dynamics?offset=&limit=30"
```

写动作（取关/取消收藏）建议走 Web UI 或本脚本，不要在命令行手工拼 `POST /api/tasks`——脚本里的二次确认与对象展示就是为了避免手滑。

---

## 8. 冒烟记录模板

跑完建议在 issue / 发布检查单里记一笔：

```text
日期：
版本 / commit：
模式：真实账号（mock=false）
PowerShell：5.1 / 7.x
只读冒烟：7/7 通过，总耗时 __ 秒
写测试：未做 / 取关 mid=__ / 取消收藏 id:type=__
结果：done __/__，备份路径 __
发现的异常：
```
