# 参与 BiliCleaner

先说清楚情况：这是个个人项目，维护带宽有限。能自己动手改的 PR，比只提需求的 issue 更容易被处理。

## 提 issue 之前

- 先用 `--mock` 模式复现一下。如果 mock 模式下也有问题，那多半是界面或逻辑的锅，好定位；如果只有真实账号下才有，写清楚是哪一步。
- **不要贴任何登录态、Cookie、备份内容、日志原文。** 需要放日志就手动脱敏，把 `SESSDATA`、`bili_jct`、`DedeUserID`、昵称和 mid 换成 `xxx`。
- 搜一下现有 issue，别重复开。

报 bug 需要这些信息：

| 项 | 说明 |
|---|---|
| 版本 | `python -m bilicleaner --version` 的输出，或者 commit hash |
| 环境 | Windows 版本、Python 版本；如果是源码运行，说明是不是 venv |
| 复现步骤 | 从启动开始，越具体越好 |
| 期望行为 / 实际行为 | 各一句 |
| 相关日志 | `logs\operations.jsonl`（应用日志）和 `task_ops\<任务id>.jsonl`（对象级流水）的最后几十行，**脱敏后**贴 |
| 截图 | 有界面异常的给一张；别拍到二维码、昵称、Cookie |

提功能请求时写清楚你要解决的场景。像"加个一键清空粉丝"这种需求先别急，工具只处理当前账号自己的数据，粉丝和黑名单在 roadmap 里但优先级不高。

## 提 PR

- 一个 PR 只做一件事。顺手重构一大片会被我拆开看，很慢。
- 改接口先改 [`docs/API-CONTRACT.md`](docs/API-CONTRACT.md)，再改实现。契约是并行开发时唯一能让几拨人对上的东西。
- 架构决策变了（比如想引 SQLite、想改风控策略），先开 issue 讨论，别直接改 ADR 和代码。现有的三个 ADR 记录的是"为什么是现在这样"，不是随便能推翻的。
- 新增第三方依赖要说明理由。现在的依赖清单很克制：`fastapi`、`uvicorn`、`requests`、`pillow`、`qrcode`，就这些。能不用库解决的就别加。
- 提 PR 前想办法跑一遍你改的那条流程。`pytest` 不覆盖真实 B 站行为，手动点一遍最靠谱。

## 代码风格

没有强制 lint 配置，也没上 pre-commit，目前靠自觉。几条底线：

**Python**

- Python 3.10+。新模块文件头统一 `from __future__ import annotations`；`const.py`、`errors.py` 是早期文件没有，不用特意去补。
- 4 空格缩进，行宽控制在 100 左右，别为了对齐把一行拖到屏幕外。
- 注释和 docstring 用中文，标识符用英文。docstring 一句话说清这个函数干什么就行，不用写参数表。
- 公共函数写类型注解；内部小函数随意。整个项目的风格是"能看懂优先"，不是"类型覆盖 100%"。
- 异常用 `errors.py` 里的类型，别到处 `raise Exception("...")`。B 站返回非 0 code 的映射在 `bili/client.py` 统一处理。
- **写操作必须走 `TaskManager`。** API 层不许直接调 `bili` 的写方法——这条是硬规矩，绕过它等于绕过限速和备份。
- `store.py` 是唯一碰数据目录和文件的地方。别的模块想读设置、写任务，调 store 的方法，别自己拼路径。
- 不要 `print` 调试。运行时信息走 `store.append_log()` 或 TaskManager 的日志接口；临时调试记得提交前删掉。

**前端**

- 纯静态：`index.html` + `app.js` + `style.css`，**不引 npm、不引 CDN、不加构建步骤**。这是 ADR 0001 定下的，改之前先想清楚。
- 请求全走相对路径 `/api/...`，不要把 `http://127.0.0.1:8770` 写死。
- 用户可控内容（昵称、签名、动态正文、收藏标题）一律走 `textContent` 或转义后再插 DOM，别直接 `innerHTML`。
- 界面上的中文文案别用"赋能""一站式"这类词，写人话。

**数据与文件**

- 文件路径里不要塞未经校验的用户可控字符串。
- 写 JSON 用临时文件 + 替换（`store._write_json` 已经这么做），不要直接覆盖写。

## 跑测试

```powershell
python -m pip install -e ".[dev]"
pytest
```

测试**不连 B 站**：接口层用 mock 的 requests 响应，服务层用 `MockBiliClient`，API 层用 `TestClient`。现在 184 条全绿。想在本地手动验证，起 mock 服务点一遍：

```powershell
.\scripts\dev.ps1        # 建 venv、装依赖、以 mock 模式启动
# 或者手动：
python -m bilicleaner --mock --no-browser --port 8771
```

打开 `http://127.0.0.1:8771/api/health`，返回 200 和 `{"ok": true, ...}` 就算起来了。

真实账号的验证是另一回事：`scripts\smoke_real.ps1` 默认只读，会真的连 B 站、需要你自己扫码登录，**不要**在 PR、CI 或自动化流程里跑它。写测试要显式加 `-IncludeWriteTest` 并二次确认，只适合人工做。细节见 [docs/SMOKE-TEST.md](docs/SMOKE-TEST.md)。

## 不要提交这些

`.gitignore` 已经挡了一部分，但别指望它兜底：

- 个人数据：`session.dat`、`settings.json`、`tasks.json`、`categories_*.json`、`backups/`、`task_ops/`、`operations.jsonl`、`*.log`
- 任何真实账号的 mid、昵称、Cookie、二维码截图
- 本地环境文件（`.venv/`、`.idea/`、`.vscode/` 里的个人配置）
- 大文件、二进制、打包产物（`dist/`、`build/`、`*.exe`）

测试需要数据就自己造。`mock/fixtures.py` 里的假昵称就是现成模板，照着加几条假数据就行，别从真实账号里拷。

## PR 前检查清单

提交前对着过一遍，能省掉一轮来回：

- [ ] `pytest` 全绿，没有 skip 掉自己写挂的用例
- [ ] 没有在测试里发真实 B 站请求，也没有把 `smoke_real.ps1` 塞进 CI
- [ ] 改了接口的话，`docs/API-CONTRACT.md` 已同步
- [ ] 新增依赖已在 PR 描述里说明理由
- [ ] `python -m bilicleaner --mock --no-browser` 能启动，`/api/health` 返回 200
- [ ] 改了前端的话，浏览器控制台没有报错，没有横向滚动条
- [ ] 写操作仍然只经过 `TaskManager`，没有在路由里直接调 `bili` 写方法
- [ ] 没有提交个人数据、日志、截图里的敏感信息
- [ ] 文档和代码没对不上（README 的功能状态表、ARCHITECTURE 的状态机）
- [ ] 自己手动跑了一遍改动的流程

## Commit 信息

中英文都行，说清楚"为什么改"，而不是"改了哪些文件"。比如：

```text
fix: 冷却到期后重试会重复计入 done

写线程重试时把同一批对象又当成新对象处理，导致 done 超过 total。
按 task_ops/<task_id>.jsonl 里已成功的 key 过滤后再重试。
```

## License

提交 PR 即表示同意以 MIT 协议发布你的贡献。
