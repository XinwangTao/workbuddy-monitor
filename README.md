# WorkBuddy Monitor

监控 [WorkBuddy](https://www.workbuddy.ai) 模型用量限流状态，自动管理待办任务队列 —— 限流时排队，额度恢复后自动发送，并提供图形界面管理一切。

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Windows-lightblue)

## ✨ 功能

- 🔍 **限流消息解析** — 粘贴/剪贴板读取 WorkBuddy 限流提示，自动提取模型名与重置时间
- 📝 **任务队列** — 限流期间把任务加入队列，状态实时可见（等待中 / 已发送）
- ⏰ **自动发送** — 后台监控线程到点自动处理队列，弹窗提醒
- 🖥 **图形界面** — Tkinter GUI，双击编辑、按钮自适应布局、模型下拉记忆
- 💰 **余额查询（可选）** — 配置 API 后可查余额/用量
- 🌙 **夜间免费时段自动跑任务** — 从 WorkBuddy 底层（模型选择器徽标 + 页面 DOM）动态发现免费模型与免费时段，免费模型在闲时（官方口径 23:00–次日 08:00）自动发送队列任务，绝不消耗积分
- ⏰ **定时任务列表** — 给任务指定发送时间（`YYYY-MM-DD HH:MM:SS` 或「下一免费时段」），到点自动发送；也可留空让其等免费时段/限流重置自动发
- 🔁 **自动迭代（智能优化循环）** — 定义目标后，在免费时段内自动跑任务；每轮结束后根据「当前状态」（上次回复 + 错误检测）智能生成下一步优化提示词，反复迭代、持续打磨项目，且全程只在免费时段内推进（可设最大轮数，0=无限）
- 🚀 **一键启动** — 双击 `launch_workbuddy.vbs`，自动检查并启动 WorkBuddy 与监控 GUI；已运行时避免重复启动，无终端黑窗

## 🚀 快速开始

### 环境要求

- Windows 10/11
- Python 3.8+（含 tkinter，官方安装包默认带）

### 安装

```powershell
git clone https://github.com/1716775457damn/workbuddy-monitor.git
cd workbuddy-monitor
```

### 运行

```powershell
# 图形界面（推荐）
pythonw workbuddy_gui.py

# 命令行模式
python workbuddy_monitor.py --add "写周报" --model "Hy4 preview"
python workbuddy_monitor.py --add "整理资料" --model "Hy4 preview" --at free        # 下一免费时段发送
python workbuddy_monitor.py --add "跑长文档" --model "Hy4 preview" --at "2026-09-23 02:00:00"
python workbuddy_monitor.py --add "急事" --model "GPT-5" --at "2026-09-22 14:00:00" --allow-credit  # 允许扣积分
python workbuddy_monitor.py --discover   # 从 WorkBuddy 底层发现免费模型/时段并打印
python workbuddy_monitor.py --catalog    # 打印已发现的免费模型目录
python workbuddy_monitor.py --list
python workbuddy_monitor.py --once   # 单次检查
python workbuddy_monitor.py --doctor # 启动自检：CDP/免费保护/免费模型与时段/活跃迭代
python workbuddy_monitor.py --report # 汇总各迭代活动轮次、产出、暂停原因与待发任务
# 自动迭代：在免费时段内反复跑任务、智能生成下一步优化提示词
python workbuddy_monitor.py --iterate "用Python写一个并发爬虫" --strategy staged --stages "搭框架,加异常处理,写测试,写文档" --max 0
python workbuddy_monitor.py --iterate "优化我的博客项目" --strategy improve --max 12
python workbuddy_monitor.py --list-iter          # 查看迭代活动
python workbuddy_monitor.py --stop-iter 123456   # 停止指定活动
python workbuddy_monitor.py --pause-iter 123456  # 暂停 / --resume-iter 恢复
```

### 一键启动（推荐）


双击项目目录里的 `launch_workbuddy.vbs`：启动器会自动定位本项目、查找 Python、检测 WorkBuddy/CDP，必要时启动 WorkBuddy（CDP 端口优先 9223），然后打开监控界面并自动开始监控。重复双击不会重复启动已运行的 WorkBuddy，但会再开一个监控窗口。启动过程记录在 `launch.log`。

如果 WorkBuddy 已经以普通方式运行但没有 CDP，启动器不会强行关闭或重启它；余额同步、免费模型发现等 CDP 功能可能不可用。关闭 WorkBuddy 后再双击启动器即可用 CDP 模式启动。若 Python 不在 PATH，请安装 Python（含 tkinter），或在 `start_workbuddy.ps1` 的 `Find-Pythonw` 函数中添加 `pythonw.exe` 路径。

也可手动运行：`powershell -ExecutionPolicy Bypass -File .\start_workbuddy.ps1`。只检查、不启动应用：追加 `-CheckOnly`。

### 桌面快捷方式（最省事）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File create_shortcut.ps1
```

一次性在**桌面**和**开始菜单**各建一个「WorkBuddy + 用量监控」图标（沿用 WorkBuddy 自己的图标），以后双击就能同时拉起 WorkBuddy（带 CDP）和监控界面。项目目录搬家后重跑一次这个脚本即可。

## 🌙 夜间免费时段 & 定时任务

### 免费模型 / 免费时段从哪来（底层发现）

程序**不再硬编码**免费时段，而是从 WorkBuddy 底层动态获取：

- **免费模型**：启动监控 / 点 GUI「🔍 发现免费」时，通过 CDP 枚举模型选择器，解析每个模型的**计费倍率**判定是否免费。

  判定口径（以 WorkBuddy 5.6.2 实测为准，模型选择器 DOM 是**无空格拼接**的，例如 `Hy3限时免费0.00x`）：

  | 解析结果 | 判定 | 说明 |
  | --- | --- | --- |
  | 倍率 = `0.00x` | **全天免费** | 如 `Hy3`，不看时段窗口，白天也能发 |
  | 倍率 = `0.29x` / `0.79x` … | **计费** | 徽标写「夜间免费」也算计费！`Hy4 preview` 0.29x、`GLM-5.2`「夜间折扣」0.79x、`Deepseek-V4-Pro`「夜间折扣」0.51x 都要扣积分 |
  | 未解析到倍率 | 保守：只在夜间时段窗口内可用 | 认不出就宁可排队，绝不误判免费 |

  > ⚠️ 关键坑：模型名自带版本号（`MiniMax-M3`、`Kimi-K2.8-Preview`、`GLM-5.3`），早年代码会把名字里的 `M3` 当成倍率 `30.25x` 之类，导致免费/计费判定完全反过来。现在会先把模型名从文本里剥掉，只解析**名字之后**的尾部倍率。
- **免费时段**：优先从 WorkBuddy 页面 DOM 扫描「23:00-08:00」之类的闲时文本；扫描不到则回退到官方口径 **23:00–次日 08:00**（腾讯 WorkBuddy 公告：老用户夜间闲时继续免费，其余时间正常消耗积分）。
- 发现结果缓存进 `state.json`（`model_catalog` / `free_info`），GUI 顶部实时展示。

> 依赖 `websocket-client` 才能走 CDP：`pip install websocket-client`。WorkBuddy 需以开启 CDP（默认端口 9222）的方式运行。发现不到时自动回退，不影响其他功能。

### 两种用法

1. **夜间免费时段跑任务**：添加任务时选一个免费模型、定时留空。监控会在该模型的免费时段内自动发送（如把「整理资料 / 跑长文档」这类不着急的活排到夜里）。
2. **指定时间运行任务列表**：在「定时」框填 `2026-09-23 02:00:00`（或点 🌙 自动填下一免费时段）。到点即发送；若勾选了「允许消耗积分」则无视免费时段（谨慎，会扣积分）。

发送保护：`free_only`（默认开）下，任何非免费时段 / 非免费模型 / 未到点的任务都会被扣留，绝不消耗积分；余额守卫仍会在发送后余额异常减少时立即暂停并弹窗。

## 🔁 自动迭代（智能优化循环）

给程序一个目标，它会在**免费时段内**反复执行任务，并且**每轮结束后根据上轮结果智能生成下一步的优化提示词**——自动打磨、持续迭代整个项目。全部在免费额度内完成，绝不消耗积分。

### 怎么用

- **命令行**：`python workbuddy_monitor.py --iterate "用Python写一个并发爬虫" --strategy staged --stages "搭框架,加异常处理,写测试,写文档" --max 0`
- **代码自迭代（真正改动项目）**：加 `--repo <项目目录> --test-cmd "<验证命令>"，提示词会携带「读文件→改文件→跑测试→汇报」指令，让免费的 WorkBuddy 在每轮直接落地代码改动：
  `python workbuddy_monitor.py --iterate "持续打磨本项目" --strategy staged --stages "提升发现准确率,加固迭代引擎,补测试" --max 0 --repo "C:/path/to/repo" --test-cmd "python test_core.py && python -m py_compile *.py"`
- **图形界面**：点底部「🔁 迭代」按钮 → 填目标/初始提示词、选策略、设最大轮数、填「代码目录」与「验证命令」→ 「▶ 开始迭代」。活动列表里可随时暂停/恢复/停止，并实时查看每轮产出。

> 别忘了先**启动监控**（CLI 直接运行 `python workbuddy_monitor.py`，或界面点「启动监控」），迭代才会在免费时段里真正跑起来。

### 三种迭代策略

| 策略 | 含义 | 适合 |
|---|---|---|
| `staged` | 按你给的阶段列表循环（如「搭框架→加测试→写文档」），到头循环 | 你知道项目该按什么节奏推进 |
| `improve`（默认） | 内置渐进优化：首轮出可运行版，之后依次修错误→提质量→补测试→优化性能→补文档→收尾 | 想"持续变好"但不想管细节 |
| `reflect` | 把上一轮产出丢回 WorkBuddy，让它自己决定当前最值得做的改进并直接做 | 完全放手，让模型自主打磨 |

### 「智能」体现在哪

- **感知状态**：每轮结束后读取 WorkBuddy 最新的回复作为「当前状态」。读取时会先切回对话页（WorkBuddy 发送后会回到会话列表，按 `last_prompt` 前缀定位回刚才那条会话），只认助手气泡正文，剥掉「共消耗 / 模型名 / 时间」元数据，排除「深度思考」折叠块与首页无障碍文案。读不到 / 判断为噪声 / 仍在流式输出时一律返回 `None`（宁可等，也绝不用半截内容或 UI 噪声推进迭代）。
- **错误自愈**：若上轮产出含 `error`/`traceback`/`失败` 等关键词，下一轮提示词会明确要求「先定位并修复再继续」，而非盲目推进。
- **自动选模型 + 切模型**：优先用你指定且确实免费的模型；否则自动挑已发现的免费模型；发送前会经 CDP `select_model` 把 WorkBuddy 切到该免费模型，确保真的走免费通道。都不行则回退到免费时段机制。
- **代码自迭代**：指定 `--repo` 后，提示词会要求接收方 WorkBuddy「读取源文件→直接修改→运行验证命令→汇报 diff 与结果」，让每轮迭代真正落到代码上、可验证（不只是一个建议）。
- **安全闸门（fail-closed）**：迭代任务一律 `allow_credit=False`。**免费额度用尽或无法确认免费时一律暂停，绝不改走积分通道、绝不发送下一条对话。** 具体说，满足任一条件即判定「不可免费发送」并暂停活动、写入 `pause_reason`：
  - 免费模型目录不存在 / 已过期（超过 `auto_send.free_catalog_max_age`，默认 6 小时）/ 缺少发现时间戳；
  - 任务模型不在目录内，或在目录里被标记为非免费；
  - 该模型的免费时段未到（含模型专属时段）；
  - 模型被限流且未到重置时间；
  - 余额守卫已暂停（曾检测到积分消耗）。
  - `allow_credit` 只影响"是否允许扣积分"的旧语义已废弃：只要 `free_only=true`（默认），任何任务都不能绕过免费门控。
  - 发现不可信时监控会按节流（默认 1 小时）自动重新发现，恢复后自动继续；`--doctor` 可随时查看当前门控状态。

### 健壮性（防死锁 / 防空转）

- **回复捕获回退**：优先从 WorkBuddy CDP 读回复；读不到时改读 `auto_send.reply_cache_file`（默认 `campaign_reply.txt`，可让接收方把最终回复写入此文件），避免永久卡住。
- **在途回复超时自愈**：任务发出后若超过 `auto_send.await_timeout`（默认 1800 秒）仍未捕获到回复（CDP/网络异常），引擎会把本轮记为失败并续跑——连续多次失败后自动暂停，**不会无限空转卡死**。
- **无代码改动停滞检测**：指定 `--repo` 且为 git 仓库时，若连续 2 轮只说话、未改动任何 `.py`（git 无 diff），判定无进展并自动暂停。
- **停滞检测**：连续 2 轮产出几乎相同（无进展）→ 自动暂停，避免无限空转白白烧掉免费额度（恢复后可继续或调整目标）。

### 可观测性（昨晚到底有没有在跑？）

- **启动自检**：启动监控时自动写入一条 `[自检]` 日志，逐项记录 WorkBuddy/CDP 是否在线、免费保护开关、免费时段与来源、免费模型目录及可信度（`fresh`/`stale`/`unmarked`/`missing`）、活跃迭代。
- **心跳日志**：监控每 10 分钟写一条 `监控心跳`，包含当前是否在免费时段、余额守卫状态、活跃迭代数与待发任务数——事后可直接判断"昨晚 23:00-08:00 监控是否在岗、有没有在推进"。
- **CLI 汇总**：`python workbuddy_monitor.py --report` 直接输出各活动轮次、最近产出、暂停原因与待发任务被卡的原因，不用翻日志。
- **启动确认**：一键启动器会等 GUI 进程真正存活（最多 10 秒），若启动即退出会弹窗提示原因（缺依赖/端口占用），不会"点了没反应"；随后自动跑一次 `--doctor` 写入 `launch.log`。
- **界面状态**：底部监控标签除「监控中」外，还会显示当前是否在免费时段、距下一免费时段还有多久、以及是否被余额守卫暂停。

### 停止条件

- `--max N` / 界面「最大轮数」= 到 N 轮自动结束（设 `0` = 无限，只在免费时段内推进，白天免费时段结束就暂停等下一夜）。
- 连续 3 轮检测到失败 → 自动暂停。
- 随时在界面或 `python workbuddy_monitor.py --stop-iter <ID>` 手动停止。

活动状态保存在 `state.json` 的 `campaigns` 字段，下一夜会自动从断点继续。

## 🖼 界面预览

- 限流状态区：显示被限流模型、重置时间、剩余倒计时
- 任务列表：模型 / 内容 / 创建时间 / 状态，双击编辑
- 监控控制：启动 / 停止后台监控、余额查询

## 📖 工作原理

1. 从剪贴板或文件读取限流消息，如：

   > 当前您在Hy4 preview模型的使用量已超出频率限制，可在2026-09-09 19:30:24 重置可用。

2. 正则解析出模型 `Hy4 preview` 和重置时间 `19:30:24`，写入本地状态
3. 后台线程每 30 秒轮询，到点把该模型的 `pending` 任务标记为已发送并提醒
4. 所有数据存在本地 `state.json`，不会上传任何服务器

## ⚙️ 配置（config.json，首次运行自动生成）

```json
{
  "watch_file": "clipboard",
  "send_mode": "notify",
  "send_command": "",
  "api": {
    "url": "",
    "headers": {},
    "balance_field": "balance",
    "usage_field": "usage"
  },
  "poll_interval": 30,
  "notify_title": "WorkBuddy 监控"
}
```

| 字段 | 说明 |
|---|---|
| `watch_file` | 监控源：`clipboard` 或文件路径 |
| `send_mode` | 发送方式：`notify`（弹窗）/ `command`（执行命令）/ `api`（POST） |
| `send_command` | `send_mode=command` 时执行，`{task}` 占位符 |
| `api.url` | 余额查询接口（GET） |
| `poll_interval` | 轮询间隔秒数 |
| `auto_send.free_window` | 免费时段 `{start,end}`（默认 `23:00`/`08:00`），发现成功后被覆盖 |
| `auto_send.discover_free_models` | 从模型选择器动态发现免费模型（默认 `true`） |
| `auto_send.discover_free_window` | 从页面 DOM 动态发现免费时段，失败回退 `23:00-08:00`（默认 `true`） |
| `auto_send.free_only` | 只走免费额度，绝不消耗积分（默认 `true`） |
| `auto_send.balance_guard` | 发送后余额减少立即暂停并弹窗报警（默认 `true`） |
| `auto_send.free_catalog_max_age` | 免费模型目录保鲜期（秒，默认 21600=6 小时）；过期一律暂停发送 |

## 🧪 运行测试

无需真实 WorkBuddy 即可验证全部核心逻辑（CDP 用伪造模块注入，状态写入临时目录，不污染仓库）：

```bash
python test_core.py          # 核心逻辑离线测试（265 项：fail-closed 门控、倍率解析、回复捕获、
                             # 输入框焦点校验、autopilot 模型自定方向、回复清洗、并发写保护等）

## 迭代策略

| 策略 | 说明 | 适用 |
|------|------|------|
| `staged` | 按你给的阶段列表循环（如「搭框架→加测试→写文档」），到头循环 | 你知道项目该按什么节奏推进 |
| `improve` / `reflect` | 单一提示词反复迭代 / 每轮带反思 | 简单打磨、回归巡检 |
| `autopilot` | **模型自定方向**：每轮回复末尾输出 `[[NEXT]]下一步任务[[/NEXT]]`，程序解析后作为下一轮提示词；连续 3 轮漏写自动暂停 | 让免费模型自己规划迭代路径 |

## 发送可靠性（实测教训沉淀）

- **输入框聚焦**：激活窗口 ≠ 焦点在编辑器。发送前用 CDP `focus_editor()` 直接把光标放回输入框；
- **新建会话隔离**：发送前点「新建任务」回空会话，避免消息粘进历史会话并**沿用其计费模型**（实测 Hy3 被会话覆盖成 Deepseek-V4.1-Flash 白扣 9.17 积分）；
- **新会话重选模型**：模型是会话级设置，新建后必须重新选择任务指定模型；
- **双重粘贴校验**：粘贴后必须看到内容、回车后必须清空，任一失败任务回到 pending 重试，绝不静默丢消息；
- **窗口激活多重降级**：ALT 键解除前台锁定 → AttachThreadInput 共享输入状态 → 全失败则取消发送。

## 快速开始

双击桌面快捷方式「WorkBuddy + 用量监控」，或：

```powershell
.\start_workbuddy.ps1    # 自检 CDP → 启动 WorkBuddy（CDP 9223）→ 启动监控 GUI
```
python test_e2e_real.py      # 实测：无 CDP 时回退/免费门控/白天不发送
python test_e2e_cdp.py       # 实测：注入 Fake CDP 的「夜间 hy4 免费」自动迭代闭环
python test_e2e_hy3_day.py   # 实测：白天 hy3 免费自动迭代闭环（按模型专属时段自动选模型）
python test_gui_smoke.py     # GUI 构建冒烟（15 项，需带 tkinter 的 Python；无 tkinter 时自动跳过）
```

一键跑全部（与 CI 同款）：

```bash
python test_core.py && python test_e2e_real.py && python test_e2e_cdp.py && python test_e2e_hy3_day.py \
  && python test_gui_smoke.py \
  && python -m py_compile workbuddy_monitor.py workbuddy_cdp.py workbuddy_gui.py && echo "ALL OK"
```

**免费判定**部分是安全关键——宁可漏判（任务继续排队），也绝不把付费模型误判成免费，否则会真扣积分。
测试里专门覆盖了这类误判边界：`x0.5`（半价）不能被当成 `x0`，`避免限流` / `免疫` 不能因为含「免」被当成免费，`MiniMax-M30.25x` 不能被解析成倍率 30.25。

**实测通过的真实验证**（2026-09-24，免费模型 Hy3，账户余额 6605.47）：

| 验证项 | 结果 |
| --- | --- |
| 免费门控 | `free_gate_reason("Hy3") == ""` 放行；`Hy4 preview`/`GLM-5.2`/`Deepseek-V4-Pro` 一律拦下 |
| 真实发送 | CDP 切到 Hy3 → 粘贴 → 回车，2 轮全部发送成功 |
| 积分消耗 | 6605.47 → 6605.47，**零扣费** |
| 回复捕获 | 从 `.cr-agent__content` 读到真实回复，自动剥掉「共消耗 / 模型名 / 时间」元数据，排除首页无障碍文案与「深度思考」折叠块；并且**只认「本轮提示词之后」那条助手消息**，匹配不到就判定读不到回复（绝不拿别的会话/上一轮的内容冒充本轮产出） |
| 会话定位 | 发送后回到会话列表，能按 `last_prompt` 前缀定位回刚才那条会话 |
| 迭代闭环 | 第 1 轮回复「链路正常」→ 推进 → 生成第 2 轮（引用上轮结果）→ 再发 → 达到 max=2 自动结束 |

> 会话定位很重要：WorkBuddy 发送消息后会回到**会话列表页**，读不到 `.cr-agent__content` 就会把别的会话内容当成本轮产出。若拿不到回复，宁可判定「未完成」也绝不用 UI 噪声推进迭代。

## 📁 文件结构

```
├── workbuddy_monitor.py   # 核心逻辑（解析/队列/监控/CLI）
├── workbuddy_gui.py       # Tkinter 图形界面
├── workbuddy_cdp.py       # CDP 驱动（读取模型/回复/余额）
├── launch_workbuddy.vbs   # 一键启动器（WorkBuddy + GUI），双击即用
├── start_workbuddy.ps1    # 启动器实现（挑 pythonw / 起 WorkBuddy CDP / 等 GUI 存活 / 自检）
├── create_shortcut.ps1    # 在桌面+开始菜单创建「WorkBuddy + 用量监控」快捷方式
├── test_core.py           # 核心逻辑离线测试（无需 WorkBuddy）
├── test_e2e_real.py       # 端到端实测：无 CDP 回退 / 免费门控 / 白天不发送
├── test_e2e_cdp.py        # 端到端实测：注入 Fake CDP 的夜间 hy4 免费迭代闭环
├── test_e2e_hy3_day.py    # 端到端实测：白天 hy3 免费迭代闭环（模型专属时段）
├── test_gui_smoke.py      # GUI 构建冒烟测试（需 tkinter）
└── README.md
```

运行时自动生成（已 gitignore）：`config.json`、`state.json`、`monitor.log`

## 🤝 贡献

欢迎 PR！无论是 bug 修复、新功能还是文档改进。

## 📄 License

[MIT](LICENSE)
