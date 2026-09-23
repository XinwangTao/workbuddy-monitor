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
- 🚀 **一键启动** — VBS 启动器同时拉起 WorkBuddy 与监控，无终端黑窗

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
# 自动迭代：在免费时段内反复跑任务、智能生成下一步优化提示词
python workbuddy_monitor.py --iterate "用Python写一个并发爬虫" --strategy staged --stages "搭框架,加异常处理,写测试,写文档" --max 0
python workbuddy_monitor.py --iterate "优化我的博客项目" --strategy improve --max 12
python workbuddy_monitor.py --list-iter          # 查看迭代活动
python workbuddy_monitor.py --stop-iter 123456   # 停止指定活动
python workbuddy_monitor.py --pause-iter 123456  # 暂停 / --resume-iter 恢复
```

### 随 WorkBuddy 一起启动

编辑 `launch_workbuddy.vbs`，把其中两处路径改成你机器上的实际路径（WorkBuddy 安装路径、本仓库路径），然后双击运行，或为其创建快捷方式。

## 🌙 夜间免费时段 & 定时任务

### 免费模型 / 免费时段从哪来（底层发现）

程序**不再硬编码**免费时段，而是从 WorkBuddy 底层动态获取：

- **免费模型**：启动监控 / 点 GUI「🔍 发现免费」时，通过 CDP 枚举模型选择器，解析每个模型徽标（如「免费」「x0」「倍率」）判定是否免费。
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

- **感知状态**：每轮结束后读取 WorkBuddy 最新的回复作为「当前状态」。
- **错误自愈**：若上轮产出含 `error`/`traceback`/`失败` 等关键词，下一轮提示词会明确要求「先定位并修复再继续」，而非盲目推进。
- **自动选模型 + 切模型**：优先用你指定且确实免费的模型；否则自动挑已发现的免费模型；发送前会经 CDP `select_model` 把 WorkBuddy 切到该免费模型，确保真的走免费通道。都不行则回退到免费时段机制。
- **代码自迭代**：指定 `--repo` 后，提示词会要求接收方 WorkBuddy「读取源文件→直接修改→运行验证命令→汇报 diff 与结果」，让每轮迭代真正落到代码上、可验证（不只是一个建议）。
- **安全闸门**：迭代任务一律 `allow_credit=False`（绝不消耗积分）；余额守卫照常生效；连续 3 轮失败自动暂停，避免无意义空转。

### 停止条件

- `--max N` / 界面「最大轮数」= 到 N 轮自动结束（设 `0` = 无限，只在免费时段内推进，白天免费时段结束就暂停等下一夜）。
- 连续 3 轮检测到失败 → 自动暂停。
- **停滞检测**：连续 2 轮产出几乎相同（无进展）→ 自动暂停，避免无限空转白白烧掉免费额度（恢复后可继续或调整目标）。
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

## 🧪 运行测试

无需真实 WorkBuddy 即可验证全部核心逻辑（CDP 用伪造模块注入，状态写入临时目录，不污染仓库）：

```bash
python test_core.py        # 118 项：免费判定/调度/迭代引擎/发送通道/限流解析
python test_gui_smoke.py   # 15 项：界面构建、定时填充、迭代窗口（需带 tkinter 的 Python）
```

**免费判定**部分是安全关键——宁可漏判（任务继续排队），也绝不把付费模型误判成免费，否则会真扣积分。
测试里专门覆盖了这类误判边界：`x0.5`（半价）不能被当成 `x0`，`避免限流` / `免疫` 不能因为含「免」被当成免费。

## 📁 文件结构

```
├── workbuddy_monitor.py   # 核心逻辑（解析/队列/监控/CLI）
├── workbuddy_gui.py       # Tkinter 图形界面
├── workbuddy_cdp.py       # CDP 驱动（读取模型/回复/余额）
├── launch_workbuddy.vbs   # 一键启动器（WorkBuddy + GUI）
├── test_core.py           # 核心逻辑离线测试（无需 WorkBuddy）
├── test_gui_smoke.py      # GUI 构建冒烟测试
└── README.md
```

运行时自动生成（已 gitignore）：`config.json`、`state.json`、`monitor.log`

## 🤝 贡献

欢迎 PR！无论是 bug 修复、新功能还是文档改进。

## 📄 License

[MIT](LICENSE)
