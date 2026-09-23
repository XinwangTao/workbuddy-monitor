#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 用量/余额监控 + 任务队列自动发送程序
================================================
功能：
1. 监测 WorkBuddy 限流消息，自动解析模型名称和重置时间
   （示例："当前您在Hy4 preview模型的使用量已超出频率限制，可在2026-09-09 19:30:24 重置可用。
            您可切换其他模型或消耗积分继续使用该模型"）
2. 任务列表：限流期间提交的任务进入队列，等待余额/额度恢复
3. 到达重置时间后自动执行（发送）队列中的任务
4. 可选：通过 HTTP API 查询余额/用量（在 config.json 中配置）

用法：
    python workbuddy_monitor.py            # 前台运行
    python workbuddy_monitor.py --once     # 单次检查后退出（供计划任务调用）
    python workbuddy_monitor.py --add "任务内容" --model "Hy4 preview"
    python workbuddy_monitor.py --list     # 查看任务队列
    python workbuddy_monitor.py --status   # 查看限流状态
"""

import argparse
import sys as _sys
if _sys.stdout and hasattr(_sys.stdout, 'reconfigure'):
    _sys.stdout.reconfigure(encoding='utf-8')
    _sys.stderr.reconfigure(encoding='utf-8')
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径与配置
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "monitor.log"

DEFAULT_CONFIG = {
    # 限流检测：要监控的消息来源（文件路径或 "clipboard"）
    "watch_file": "",
    # 自动发送方式: "notify"  = Windows 通知提醒
    #              "command" = 执行 config.send_command（占位符 {task}）
    #              "api"     = POST 到 config.api.url
    "send_mode": "notify",
    "send_command": "",
    # 用官方 API（CDP 句柄）读取精确积分余额，默认启用
    "api_balance": True,
    # 可选的余额/用量查询 API
    "api": {
        "url": "",
        "headers": {},
        "balance_field": "balance",   # JSON 响应中表示余额的字段
        "usage_field": "usage",       # JSON 响应中表示用量的字段
    },
    # 轮询间隔（秒）
    "poll_interval": 30,
    # Windows 通知标题
    "notify_title": "WorkBuddy 监控",
    # 到点自动发送到 WorkBuddy 窗口（复制到剪贴板 + 粘贴 + 回车）
    "auto_send": {
        "enabled": True,
        "window_title": "WorkBuddy",
        "free_only": True,  # 只在限流重置后(免费额度)自动发送，绝不走积分通道
        "balance_guard": True,  # 积分余额守卫：发送后余额减少立即停止并报警
        "free_window": {  # 免费时段：只在此时段内自动发送（不消耗积分）
            "enabled": True,
            "start": "23:00",
            "end": "08:00",
        },
        # 从 WorkBuddy 底层（模型选择器 / 页面 DOM）动态发现免费模型与免费时段，
        # 发现不到时回退到上面的 free_window 配置（官方口径 23:00-08:00）。
        "discover_free_models": True,   # 枚举模型选择器，标注哪些模型免费
        "discover_free_window": True,   # 从页面 DOM 发现闲时夜间免费时段
    },
}

# 弹窗（置顶 MessageBox，比气泡通知更醒目，用于余额报警等紧急场景）
def show_alert_popup(title: str, body: str):
    import ctypes
    # MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST = 0x30 | 0x10000 | 0x40000
    ctypes.windll.user32.MessageBoxW(0, body, title, 0x30 | 0x10000 | 0x40000)


def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = DEFAULT_CONFIG.copy()
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    # 补齐缺失键
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


def save_config(cfg: dict):
    """原子写回配置文件"""
    import os
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


def load_state() -> dict:
    """读取状态；文件损坏时自动备份到 state.json.bak 并回退空状态，保证程序可启动"""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("state root is not a dict")
            data.setdefault("tasks", [])
            data.setdefault("rate_limits", {})
            data.setdefault("campaigns", [])
            return data
        except Exception as e:
            import shutil
            log(f"state.json 损坏({e})，已备份为 .bak 并重置")
            try:
                shutil.copy2(STATE_FILE, STATE_FILE.with_suffix(".bak"))
            except Exception:
                pass
    return {"tasks": [], "rate_limits": {}}


def save_state(state: dict):
    """原子写入：先写临时文件再替换，防止写到一半崩溃损坏 state.json"""
    import os
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def log(msg: str):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        # 日志轮转：超过 1MB 归档为 monitor.old.log，防止无限增长
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            import shutil
            shutil.move(str(LOG_FILE), str(LOG_FILE.with_suffix(".old.log")))
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 限流消息解析
# ---------------------------------------------------------------------------
# 匹配 "Hy4 preview模型" / "Hy4 preview 模型"
MODEL_RE = re.compile(r"您在\s*(.+?)\s*模型的使用量已超出频率限制")
# 变体文案：部分版本不写"您在…使用量已超出…"，例如 "模型 Hy4 preview 已限流"
MODEL_RE2 = re.compile(r"(?:模型\s*[:：]?\s*)?([A-Za-z0-9][\w.\- ]{0,40}?)\s*(?:模型)?\s*(?:已|被)?\s*限流")
# 秒数可省略：真实消息里也可能写成 "2026-09-09 19:30"（缺秒必须能解析，否则检测不到限流）
TIME_RE = re.compile(r"可在\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)\s*重置")
# 备用：只要时间
TIME_RE2 = re.compile(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)")


def _parse_dt(s: str):
    """解析 'YYYY-MM-DD HH:MM[:SS]'，缺秒时补 :00；非法返回 None"""
    s = (s or "").strip()
    if len(s) == 16:
        s += ":00"
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def parse_rate_limit_message(text: str):
    """解析限流消息，返回 (model, reset_time: datetime|None, ok)"""
    model = None
    reset = None
    # 主文案优先；匹配不到再试变体文案（如 "模型 X 已限流"）
    m = MODEL_RE.search(text) or MODEL_RE2.search(text)
    if m:
        model = m.group(1).strip()
    t = TIME_RE.search(text) or TIME_RE2.search(text)
    if t:
        reset = _parse_dt(t.group(1))
    return model, reset, (model is not None or reset is not None)


# ---------------------------------------------------------------------------
# 免费模型 / 免费时段 动态发现（从 WorkBuddy 底层 = 模型选择器 / 页面 DOM）
# ---------------------------------------------------------------------------
# 官方口径（腾讯 WorkBuddy 联合团队公告）：老用户夜间 23:00-次日 08:00 继续免费，
# 其余时间消耗积分；免费仅限对话（纯文本），画图/视频走多模态模型扣积分。
# 免费判定（正则，不区分大小写）：语义明确的关键词。
# 用 (?<!\d)0\s*积分 而非 "0积分" 子串，避免 "10积分" 被误判为免费；
# "免积分"/"不计费"/"限免" 等也归入免费语义。
FREE_PATTERNS = [
    r"免费", r"限免", r"free",
    r"(?<!\d)0\s*积分",   # 0积分 / 0 积分（排除 10积分 等正成本）
    r"免\s*积分",          # 免积分
    r"不计费",             # 不计费
    r"夜间免费", r"闲时免费",
]
# 免费判定（徽标精确匹配）：短词做子串匹配会误判——
#   "x0.5"(半价) 含 "x0"、"避免限流" 含 "免"、"免疫" 含 "免"，
#   误判为免费会导致在非免费通道发送而真扣积分，因此这类短词只允许徽标全等匹配。
FREE_MODEL_BADGE_EXACT = ("x0", "×0", "免", "0", "0 积分", "free", "免费", "限免")
_POLICY_FREE_WINDOW = {"start": "23:00", "end": "08:00", "source": "policy"}


def _parse_multiplier(text: str):
    """从徽标/文本解析积分倍率，如 'x2' '倍率2' 'x0.5' '2倍'。返回 float 或 None。
    必须带显式倍率标记(x/×/*/倍)或徽标本身就是一个数字，避免把模型名里的
    数字(如 'Hy4')误判为倍率而错误触发"非免费"。"""
    if not text:
        return None
    s = text.strip()
    # 形式1：显式倍率标记 x/×/*/倍 后跟数字，如 x2 / ×0.5 / *2 / 倍率2 / 2倍
    m = re.search(r"(?:[xX×*]|倍率?)\s*(\d+(?:\.\d+)?)\s*倍?", s)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    # 形式1b：数字后跟"倍"，如 2倍 / 0.5倍
    m = re.search(r"(\d+(?:\.\d+)?)\s*倍", s)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    # 形式2：整个徽标就是一个数字（如 "0" "2" "0.5"），视为倍率
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    return None


def _parse_window_from_text(text: str):
    """从文本解析免费时段 HH:MM-HH:MM，返回 (start,end) 或 None。
    兼容 '23:00-08:00' / '23:00 到 08:00' / '23:00-次日 8:00' 等写法。"""
    if not text:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})\s*[-~到至]\s*[\u4e00-\u9fff]{0,4}?\s*(\d{1,2}):(\d{2})", text)
    if m:
        return (f"{int(m.group(1)):02d}:{int(m.group(2)):02d}",
                f"{int(m.group(3)):02d}:{int(m.group(4)):02d}")
    return None


def classify_model(item: dict):
    """把一个 list_models() 返回的模型项归类为结构化记录。
    返回 {name, free, free_confidence, free_reason, badge, multiplier, free_window, raw}。

    准确率原则（宁可漏判绝不误判）：
      - 正向信号：免费关键词 / 免费徽标 / 倍率为 0
      - 负向信号：倍率>0 或 文本含正积分成本(如 "10积分") → 强制非免费
    二者冲突时以负向信号为准，避免走积分通道真扣积分。"""
    name = (item.get("name") or "").strip()
    badge = (item.get("badge") or "").strip()
    text = (item.get("text") or "").strip()
    desc = (item.get("desc") or "").strip()
    combined = f"{name} {badge} {text} {desc}"
    multiplier = _parse_multiplier(badge) or _parse_multiplier(text)
    # 负向信号：检测到积分成本（倍率>0，或形如 "10积分" 的正成本）
    cost_m = re.search(r"(\d+(?:\.\d+)?)\s*积分", combined)
    positive_cost = ((multiplier is not None and multiplier > 0)
                     or (cost_m is not None and float(cost_m.group(1)) > 0))
    # 正向信号
    badge_norm = badge.strip().lower()
    free_kw = any(re.search(p, combined, re.I) for p in FREE_PATTERNS)
    free_badge = badge_norm in FREE_MODEL_BADGE_EXACT
    zero_mult = (multiplier is not None and multiplier == 0)
    reasons = []
    if free_kw:
        reasons.append("免费关键词")
    if free_badge:
        reasons.append("免费徽标")
    if zero_mult:
        reasons.append("倍率为0")
    positive = free_kw or free_badge or zero_mult
    if positive_cost:
        positive = False
        reasons = [f"检测到积分成本(倍率={multiplier}, 文本成本={cost_m.group(1) if cost_m else '?'}积分)→非免费"]
    free = bool(positive)
    free_window = _parse_window_from_text(text) or _parse_window_from_text(desc)
    if free_window:
        free_window = {"start": free_window[0], "end": free_window[1]}
    return {
        "name": name, "free": free,
        "free_confidence": 1.0 if free else 0.0,
        "free_reason": ("; ".join(reasons) if reasons else "非免费"),
        "badge": badge, "multiplier": multiplier,
        "free_window": free_window, "raw": combined[:140],
    }


def discover_free_models(cfg: dict, state: dict = None, force: bool = False):
    """从 WorkBuddy 模型选择器（底层 DOM）枚举模型并标注免费状态。
    结果写入 state['model_catalog']。返回目录列表或 None（CDP 不可用/未配置发现）。"""
    auto = cfg.get("auto_send", {})
    if not auto.get("discover_free_models", True) and not force:
        return (state or {}).get("model_catalog")
    try:
        import workbuddy_cdp
        raw = workbuddy_cdp.list_models()
    except Exception as e:
        log(f"发现免费模型失败(CDP): {e}")
        return None
    if not raw:
        return None
    cat = [classify_model(it) for it in raw if (it.get("name") or "").strip()]
    if not cat:
        return None
    fresh = load_state()
    fresh["model_catalog"] = cat
    fresh.setdefault("free_info", {})["models_discovered_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_state(fresh)
    if state is not None:
        state["model_catalog"] = cat
    n_free = sum(1 for c in cat if c["free"])
    log(f"已发现 {len(cat)} 个模型，其中 {n_free} 个免费："
        f"{[c['name'] for c in cat if c['free']]}")
    return cat


def _save_free_window(state: dict, win: dict):
    fresh = load_state()
    fi = fresh.setdefault("free_info", {})
    fi["window"] = {"start": win["start"], "end": win["end"], "source": win.get("source", "config")}
    if win.get("ctx"):
        fi["window"]["ctx"] = win["ctx"]
    fi["discovered_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_state(fresh)
    if state is not None:
        state.setdefault("free_info", {}).update(fi)


def discover_free_window(cfg: dict, state: dict = None, force: bool = False):
    """从 WorkBuddy 页面 DOM 发现全局免费时段（闲时夜间窗口）。
    发现不到则回退到配置/策略默认 23:00-08:00。写入 state['free_info']['window']。"""
    auto = cfg.get("auto_send", {})
    w = auto.get("free_window", {})
    fallback = {"start": str(w.get("start", "23:00")),
                "end": str(w.get("end", "08:00")), "source": "config"}
    if auto.get("discover_free_window", True) or force:
        try:
            import workbuddy_cdp
            dom = workbuddy_cdp.discover_free_window_dom()
            if dom:
                win = {"start": dom["start"], "end": dom["end"],
                       "source": "discovered", "ctx": dom.get("ctx", "")}
                _save_free_window(state, win)
                log(f"已从 WorkBuddy 页面发现免费时段: {win['start']}-{win['end']} (ctx={win.get('ctx')})")
                return win
        except Exception as e:
            log(f"发现免费时段失败(CDP): {e}")
    _save_free_window(state, fallback)
    log(f"使用免费时段（配置/策略默认）: {fallback['start']}-{fallback['end']}")
    return fallback


def effective_free_window(state: dict, model: str = None) -> dict:
    """返回用于判定的免费时段 {start, end, source}：
    优先模型专属窗口(model_catalog) → 全局 free_info.window → 策略默认 23:00-08:00。"""
    if model:
        for m in (state or {}).get("model_catalog", []) or []:
            if m.get("name") == model and m.get("free_window"):
                return m["free_window"]
    fi = (state or {}).get("free_info", {}) or {}
    win = fi.get("window")
    if win and win.get("start") and win.get("end"):
        return {"start": win["start"], "end": win["end"], "source": win.get("source", "config")}
    return dict(_POLICY_FREE_WINDOW)


def in_window(win: dict, now=None) -> bool:
    """win: {start, end} 支持跨午夜。未配置返回 True。"""
    if not win:
        return True
    try:
        sh, sm = map(int, str(win.get("start", "23:00")).split(":"))
        eh, em = map(int, str(win.get("end", "08:00")).split(":"))
    except (ValueError, AttributeError):
        return True
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    s, e = sh * 60 + sm, eh * 60 + em
    if s == e:
        return True
    if s < e:
        return s <= cur < e
    return cur >= s or cur < e  # 跨午夜窗口


def is_model_free_now(model: str, state: dict, cfg: dict, now=None) -> bool:
    """model 当前是否处于免费可用状态（已发现为免费 且 在免费时段内）。
    无发现数据时回退原行为：全局免费时段内一律视为可免积分发送。"""
    cat = (state or {}).get("model_catalog", []) or []
    if cat:  # 有发现数据：只对标记为免费的模型在免费时段内放行
        if not model:
            return in_free_window(cfg, now, state)
        m = next((x for x in cat if x.get("name") == model), None)
        if m and m.get("free"):
            return in_free_window(cfg, now, state, model)
        return False  # 模型未标记为免费（或未发现该模型）：free_only 下禁止，避免扣积分
    # 无发现数据：回退到原行为——全局免费时段内视为可免积分发送
    return in_free_window(cfg, now, state)


def task_sendable_now(task: dict, state: dict, cfg: dict, now=None) -> bool:
    """判断一个 pending 任务此刻是否可以发送（free_only 前提下绝不消耗积分）。
    满足以下任一即可发：① 限流重置时间已到 ② 处于免费模型的免费时段 ③ 已到用户指定时间。"""
    now = now or datetime.now()
    if balance_guard_paused(state):
        return False
    auto = cfg.get("auto_send", {})
    free_only = auto.get("free_only", True)
    # ① 显式定时：未到时间不发
    sched = task.get("scheduled_time")
    if sched:
        try:
            sched_dt = datetime.strptime(sched, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            sched_dt = None
        if sched_dt and now < sched_dt:
            return False
    # ② 限流闸门：模型仍被限流且未到重置时间，不发
    model = task.get("model")
    rl = state.get("rate_limits", {})
    if model and isinstance(rl, dict):
        info = rl.get(model) or (rl.get("default") if model else None)
        if isinstance(info, dict) and info.get("reset"):
            try:
                reset_dt = datetime.strptime(info["reset"], "%Y-%m-%d %H:%M:%S")
                if now < reset_dt:
                    return False
            except ValueError:
                pass
    # ③ 免费额度保护：free_only 且任务未显式允许消耗积分时，必须处于免费状态
    if free_only and not task.get("allow_credit"):
        if not is_model_free_now(model, state, cfg, now):
            return False
    return True


# ---------------------------------------------------------------------------
# 任务队列
# ---------------------------------------------------------------------------
def add_task(state: dict, content: str, model: str = "", scheduled_time: str = None,
             allow_credit: bool = False):
    """添加任务。注意：始终从磁盘读最新状态再合并写回，
    避免用调用方内存里的旧状态覆盖磁盘上其他线程/进程的更改。

    scheduled_time: "YYYY-MM-DD HH:MM:SS" 或 None（None=立即入队/等免费时段）
    allow_credit:   是否允许在免费时段外消耗积分发送（默认 False，保护积分）
    """
    if scheduled_time:
        try:
            datetime.strptime(scheduled_time, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            scheduled_time = None  # 格式非法则退化为立即
    task = {
        "id": int(time.time() * 1000),
        "content": content,
        "model": model,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scheduled_time": scheduled_time,
        "allow_credit": bool(allow_credit),
        "status": "pending",  # pending / sent / failed
    }
    fresh = load_state()
    fresh.setdefault("tasks", []).append(task)
    save_state(fresh)
    state["tasks"] = fresh["tasks"]  # 同步回调用方视图
    sched_hint = f" 定时={scheduled_time}" if scheduled_time else ""
    log(f"已添加任务 #{task['id']}: {content[:50]}{sched_hint}")
    return task


def update_task(task_id, **fields):
    """按 id 更新单个任务字段：从磁盘读最新状态、合并、原子写回。
    多线程/多进程安全的任务状态变更统一走这里。"""
    fresh = load_state()
    for t in fresh.get("tasks", []):
        if t.get("id") == task_id:
            t.update(fields)
            save_state(fresh)
            return True
    return False


def pending_tasks(state: dict, model: str = ""):
    return [t for t in state.get("tasks", [])
            if t.get("status") == "pending" and (not model or t.get("model") == model)]

# ---------------------------------------------------------------------------
# 自动迭代引擎
# ---------------------------------------------------------------------------
# 用户定义一个"迭代目标"，引擎在免费时段内自动执行任务；
# 每次完成后根据"当前状态"（上次 WorkBuddy 回复 + 错误检测）智能生成下一步优化提示词，
# 再次入队、再次执行，如此反复，且只在免费时段推进。可设最大轮数（0=无限）。
ITER_STRATEGIES = ("staged", "improve", "reflect")
_IMPROVE_ANGLES = [
    "实现项目核心功能，产出可运行的第一版",
    "检查并修复潜在错误、异常与边界情况",
    "提升代码质量与可读性（命名、结构、注释）",
    "补充单元测试与关键场景覆盖",
    "优化性能与资源占用",
    "完善文档与使用说明",
    "做一轮整体回顾，打磨收尾并验证目标达成",
]
_FAIL_KW = ("错误", "报错", "失败", "异常", "traceback", "error", "exception",
            "✗", "✘", "cannot", "undefined", "syntax", "failed", "unable")


def _looks_failed(text):
    if not text:
        return False
    low = (text or "").lower()
    return any(k in low for k in _FAIL_KW)


def _truncate(s, n=800):
    s = (s or "").strip()
    return s[:n] + ("…" if len(s) > n else "")


def ensure_campaigns(state):
    state.setdefault("campaigns", [])
    for c in state["campaigns"]:
        c.setdefault("history", [])
        c.setdefault("stage_index", 0)
        c.setdefault("current_iteration", 0)
        c.setdefault("error_count", 0)
        c.setdefault("awaiting_result", False)
        c.setdefault("last_task_id", None)
        c.setdefault("last_prompt", "")
        c.setdefault("last_result", "")
        c.setdefault("stages", [])
    return state["campaigns"]


def active_campaigns(state):
    return [c for c in ensure_campaigns(state) if c.get("status") == "running"]


def pick_free_model(state):
    """从已发现目录里挑一个免费模型；没有返回 None。"""
    cat = (state or {}).get("model_catalog") or []
    free = [c["name"] for c in cat if c.get("free")]
    return free[0] if free else None


def create_campaign(state, goal, strategy="improve", model="", stages=None,
                    max_iterations=0, repo_path="", test_cmd=""):
    """创建自动迭代活动。返回新 campaign dict。
    repo_path/test_cmd 用于"代码自迭代"：活动会生成携带读文件/改文件/跑测试
    指令的提示词，让接收方 WorkBuddy 真正改动项目并产生可验证的 diff。"""
    strategy = strategy if strategy in ITER_STRATEGIES else "improve"
    camps = ensure_campaigns(state)
    camp = {
        "id": int(time.time() * 1000),
        "goal": goal.strip(),
        "strategy": strategy,
        "model": (model or "").strip(),
        "stages": [s.strip() for s in (stages or []) if s.strip()],
        "max_iterations": int(max_iterations or 0),
        "repo_path": (repo_path or "").strip(),
        "test_cmd": (test_cmd or "").strip(),
        "current_iteration": 0,
        "stage_index": 0,
        "status": "running",
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_prompt": "",
        "last_result": "",
        "last_task_id": None,
        "awaiting_result": False,
        "error_count": 0,
        "stagnation": False,
        "history": [],
    }
    camps.append(camp)
    save_state(state)
    log(f"已创建迭代活动 #{camp['id']} 目标={goal[:40]} 策略={strategy} "
        f"最大轮数={'∞' if not camp['max_iterations'] else camp['max_iterations']}")
    return camp


def campaign_by_id(state, cid):
    for c in ensure_campaigns(state):
        if c["id"] == cid:
            return c
    return None


def set_campaign_status(state, cid, status):
    c = campaign_by_id(state, cid)
    if c:
        c["status"] = status
        if status == "running":
            # 恢复时清空在途状态，下一轮重新生成任务
            c["awaiting_result"] = False
            c["last_task_id"] = None
        save_state(state)
        log(f"迭代活动 #{cid} 状态 -> {status}")
        return True
    return False


def _effective_model(camp, state):
    """选择本次迭代使用的免费模型：优先活动指定且确实免费的模型，否则自动挑免费模型，再退化为活动指定/空。"""
    pref = (camp.get("model") or "").strip()
    free = pick_free_model(state)
    cat = (state or {}).get("model_catalog") or []
    if pref and any(c["name"] == pref and c.get("free") for c in cat):
        return pref
    if free:
        return free
    return pref or ""


def _similar(a, b, threshold=0.9):
    """两段文本相似度（difflib ratio），用于停滞检测。"""
    import difflib
    return difflib.SequenceMatcher(None, a or "", b or "").ratio() >= threshold


def generate_next_prompt(camp, state):
    """根据当前状态生成下一步优化提示词。返回 (prompt, label) 或 None（活动应结束）。"""
    it = camp["current_iteration"] + 1  # 本轮是第几轮
    last = camp.get("last_result", "")
    failed = _looks_failed(last)
    goal = camp.get("goal", "")
    ctx = f"项目目标：{goal}\n" if goal else ""
    base = (f"{ctx}这是第 {it} 轮自主迭代。请直接动手改进并给出结果"
            f"（纯文本/代码，不要触发消耗积分的多模态功能）。\n")
    if failed:
        base += (f"⚠ 上一步执行似乎出现了问题，请先定位并修复，再继续推进。\n"
                 f"上一步产出（节选）：\n{_truncate(last, 600)}\n")
    else:
        base += f"上一轮产出摘要（节选）供参考：\n{_truncate(last, 600)}\n"

    strat = camp.get("strategy", "improve")
    if strat == "staged":
        stages = camp.get("stages") or []
        if not stages:
            stages = _IMPROVE_ANGLES
        idx = camp["stage_index"] % len(stages)
        label = stages[idx]
        prompt = f"{base}\n本轮阶段任务（{label}）：\n{label}"
        return _apply_code_tail(prompt, camp, label), label
    if strat == "reflect":
        label = "模型自主反思优化"
        prompt = (f"{base}\n请审视你上一轮的产出与当前项目状态，自己决定当前最有价值的"
                  f"一个改进点，并直接实施它。只做最关键的一件事，做到位即可。")
        return _apply_code_tail(prompt, camp, label), label
    # improve（默认）
    angle = _IMPROVE_ANGLES[min(it - 1, len(_IMPROVE_ANGLES) - 1)]
    label = angle
    prompt = f"{base}\n本轮优化方向：{angle}\n请围绕该方向改进当前项目。"
    return _apply_code_tail(prompt, camp, label), label


def _apply_code_tail(prompt, camp, label):
    """若活动指向代码仓库，附加'读文件→改文件→跑测试→汇报'指令，使自迭代产生真实改动。"""
    repo = camp.get("repo_path")
    if not repo:
        return prompt
    test_cmd = camp.get("test_cmd") or "python -m py_compile *.py && echo OK"
    tail = (
        f"\n\n—— 执行要求（代码自迭代）——\n"
        f"本项目位于目录：{repo}\n"
        f"请按以上方向（{label}）真正改进代码，不要只给建议：\n"
        f"1) 先用工具读取相关源文件，理解现状；\n"
        f"2) 直接修改对应文件（新增/修复/重构），确保改动可运行、不破坏现有功能；\n"
        f"3) 运行验证命令确认无回归：\n    {test_cmd}\n"
        f"4) 简短汇报：改了哪些文件、改了什么、验证结果（通过/失败）。\n"
        f"只做与本轮方向相关的一件关键改动，做到位即可。"
    )
    return prompt + tail


def advance_campaign(camp, result, state):
    """收到一次迭代结果后推进：记录历史、轮次+1、错误计数、停止条件判定。"""
    camp["current_iteration"] += 1
    camp["history"].append({
        "iter": camp["current_iteration"],
        "prompt": camp.get("last_prompt", ""),
        "result": _truncate(result, 600),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    camp["last_result"] = result or camp.get("last_result", "")
    stages = camp.get("stages") or _IMPROVE_ANGLES
    camp["stage_index"] = (camp["stage_index"] + 1) % max(1, len(stages))
    camp["awaiting_result"] = False
    camp["last_task_id"] = None
    if _looks_failed(result):
        camp["error_count"] = camp.get("error_count", 0) + 1
    else:
        camp["error_count"] = 0
    # 停止条件
    if camp["max_iterations"] and camp["current_iteration"] >= camp["max_iterations"]:
        camp["status"] = "done"
        log(f"迭代活动 #{camp['id']} 达到最大轮数 {camp['max_iterations']}，已结束")
    elif camp["error_count"] >= 3:
        camp["status"] = "paused"
        log(f"迭代活动 #{camp['id']} 连续 3 轮检测到失败，已自动暂停（请检查后恢复）")
    else:
        # 停滞检测：连续 2 轮结果几乎相同（无进展）则暂停，避免无限空转烧免费额度
        hist = camp.get("history", [])
        if len(hist) >= 2:
            prev = hist[-2].get("result", "") or ""
            cur = hist[-1].get("result", "") or ""
            if prev and cur and _similar(prev, cur):
                camp["status"] = "paused"
                camp["stagnation"] = True
                log(f"迭代活动 #{camp['id']} 连续 2 轮输出几乎相同，已自动暂停"
                    f"（疑似无进展，请检查目标任务是否仍可优化）")
    save_state(state)


def ensure_campaign_task(camp, state, cfg, now):
    """若活动没有待发送任务，则生成下一步提示词并入队。返回 pending 任务或 None（活动结束）。"""
    pending = [t for t in state.get("tasks", [])
               if t.get("campaign_id") == camp["id"] and t.get("status") == "pending"]
    if pending:
        return pending[0]
    prompt, label = generate_next_prompt(camp, state)
    if prompt is None:
        camp["status"] = "done"
        save_state(state)
        return None
    model = _effective_model(camp, state)
    # 免费额度保护：迭代任务绝不消耗积分（allow_credit=False），只在免费时段/免费模型发送
    task = add_task(state, prompt, model=model, scheduled_time=None, allow_credit=False)
    # 必须同步更新内存里的任务对象：add_task 返回的对象就在 state["tasks"] 中，
    # 若只调用 update_task 落盘，随后 save_state(state) 会用内存中的旧副本覆盖掉
    # campaign_id，导致下一轮查不到在途任务而重复入队、无限堆积任务。
    task["campaign_id"] = camp["id"]
    task["model"] = model
    update_task(task["id"], campaign_id=camp["id"], model=model)
    camp["last_prompt"] = prompt
    camp["awaiting_result"] = True
    camp["last_task_id"] = task["id"]
    save_state(state)
    log(f"迭代活动 #{camp['id']} 已生成第 {camp['current_iteration'] + 1} 轮任务"
        f"（方向：{label}）模型={model or '自动(免费时段)'}")
    return task


def try_capture_campaign_result(camp, cfg, timeout=240, poll=15):
    """尝试读取 WorkBuddy 对 camp.last_task_id 的回复。返回文本或 None（未就绪/超时/不可用）。"""
    try:
        import workbuddy_cdp
        msg = workbuddy_cdp.last_assistant_message(timeout=timeout, poll=poll)
        if msg:
            return msg
    except Exception as e:
        log(f"迭代读取 WorkBuddy 回复失败(可忽略): {e}")
    return None


def run_iteration_step(state, cfg, now=None):
    """执行一轮自动迭代步进。调用方需保证当前处于免费时段（免费额度保护前提）。
    已发送任务会在此阻塞等待 WorkBuddy 回复（最长 timeout 秒，轮询），拿到结果后立即
    生成下一轮任务——这样在一个免费时段内可连续多轮迭代。"""
    now = now or datetime.now()
    for camp in active_campaigns(state):
        # 1) 若已发送任务在等回复，尝试捕获结果
        if camp.get("awaiting_result") and camp.get("last_task_id"):
            t = next((x for x in state.get("tasks", [])
                      if x.get("id") == camp["last_task_id"]), None)
            if not t:
                camp["awaiting_result"] = False
                camp["last_task_id"] = None
                save_state(state)
            elif t.get("status") == "sent":
                res = try_capture_campaign_result(camp, cfg)
                if res is not None:
                    advance_campaign(camp, res, state)
                    # 成功推进后，若活动仍活跃，立即生成下一轮任务（同一轮内连续迭代）
                    if camp.get("status") == "running":
                        ensure_campaign_task(camp, state, cfg, now)
                # 未捕获到（CDP 不可用或仍在流式输出）则保持 awaiting，下轮继续
                continue
            else:
                # 仍在 pending：等统一调度循环发送
                continue
        # 2) 需要下一轮任务
        ensure_campaign_task(camp, state, cfg, now)


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------
def _ps_quote(s: str) -> str:
    """转义 PowerShell 单引号字符串，防止任务内容中的引号破坏命令或注入"""
    return "'" + str(s).replace("'", "''") + "'"


def send_windows_notification(title: str, body: str):
    try:
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$n = New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon = [System.Drawing.SystemIcons]::Information; "
            "$n.Visible = $true; "
            f"$n.ShowBalloonTip(5000, {_ps_quote(title)}, {_ps_quote(body)}, "
            "[System.Windows.Forms.ToolTipIcon]::Info)"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=15,
        )
    except Exception as e:
        log(f"通知发送失败: {e}")


# ---------------------------------------------------------------------------
# 自动发送：剪贴板 + 激活 WorkBuddy 窗口 + Ctrl+V + Enter（纯 ctypes，无第三方依赖）
# ---------------------------------------------------------------------------
import ctypes
import ctypes.wintypes as _wt

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
# 64 位下必须声明正确的返回/参数类型，否则句柄被截断导致写入违规
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = (ctypes.c_uint, ctypes.c_size_t)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = (ctypes.c_uint, ctypes.c_void_p)
user32.GetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = (ctypes.c_uint,)


def set_clipboard_text(text: str) -> bool:
    """把文本写入系统剪贴板（CF_UNICODETEXT）"""
    if not user32.OpenClipboard(0):
        return False
    try:
        user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        h = kernel32.GlobalAlloc(0x2000, len(data))  # GMEM_MOVEABLE
        if not h:
            return False
        ptr = kernel32.GlobalLock(h)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(13, h):  # CF_UNICODETEXT
            return False
        return True
    finally:
        user32.CloseClipboard()


def _find_window_by_title(substr: str):
    """枚举顶层窗口，返回标题包含 substr 的第一个可见窗口句柄"""
    result = []
    @ctypes.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if substr.lower() in buf.value.lower():
                result.append(hwnd)
        return True
    user32.EnumWindows(cb, 0)
    return result[0] if result else None


def _activate_window(hwnd) -> bool:
    """还原并前置窗口"""
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    return bool(user32.GetForegroundWindow() == hwnd)


def _key(vk, up=False):
    user32.keybd_event(vk, 0, 2 if up else 0, 0)


def _ctrl_v():
    _key(0x11); _key(0x56)
    _key(0x56, True); _key(0x11, True)


def _enter():
    _key(0x0D)
    _key(0x0D, True)


def _get_clipboard_text() -> str:
    """读取当前剪贴板文本（用于发送后还原）"""
    if not user32.OpenClipboard(0):
        return ""
    try:
        h = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not h:
            return ""
        return ctypes.cast(h, ctypes.c_wchar_p).value or ""
    finally:
        user32.CloseClipboard()


def auto_send_to_workbuddy(content: str, window_title: str = "WorkBuddy") -> tuple:
    """复制到剪贴板 → 激活 WorkBuddy → 粘贴 → 回车发送。
    返回 (ok, message)。免费额度保护：本函数只在限流重置后被调用，
    且绝不会点击 WorkBuddy 中任何「消耗积分」确认弹窗。"""
    prev_clip = _get_clipboard_text()
    if not set_clipboard_text(content):
        return False, "写入剪贴板失败"
    hwnd = _find_window_by_title(window_title)
    if not hwnd:
        return False, f"未找到标题含「{window_title}」的窗口"
    if not _activate_window(hwnd):
        return False, "无法激活 WorkBuddy 窗口"
    time.sleep(0.5)
    # 发送前二次确认前台就是目标窗口，防止把内容打进无关应用
    if user32.GetForegroundWindow() != hwnd:
        return False, "前台窗口校验失败，取消发送"
    _ctrl_v()
    time.sleep(1.0)  # 等输入框渲染完粘贴内容
    _enter()
    time.sleep(0.3)
    # 还原用户剪贴板
    if prev_clip:
        set_clipboard_text(prev_clip)
    return True, "已自动粘贴并发送到 WorkBuddy"


def send_task(task: dict, cfg: dict):
    mode = cfg.get("send_mode", "notify")
    state = load_state()  # 发送前取最新状态（含 model_catalog / free_info）
    # 这两个变量在函数末尾的余额守卫中无条件引用，必须在此初始化，
    # 否则 command / api 模式会因未定义而抛 NameError，导致任务状态永远标不上 sent。
    auto = cfg.get("auto_send", {})
    balance_before = None  # 发送前余额基线（仅 notify 模式实际读取）
    log(f"发送任务 #{task['id']}: {task['content'][:50]} (mode={mode})")
    if mode == "command":
        cmd = cfg.get("send_command", "")
        if cmd:
            cmd = cmd.replace("{task}", task["content"])
            subprocess.run(cmd, shell=True, capture_output=True, timeout=60)
    elif mode == "api":
        import urllib.request
        api = cfg.get("api", {})
        if api.get("url"):
            data = json.dumps({"task": task["content"], "model": task.get("model", "")}).encode("utf-8")
            req = urllib.request.Request(api["url"], data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            for k, v in api.get("headers", {}).items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=30) as resp:
                log(f"API 响应: {resp.status}")
    else:
        if auto.get("enabled") and auto.get("free_only", True):
            # 免费额度保护：除非任务显式允许消耗积分，否则必须处于免费状态
            if not task.get("allow_credit"):
                now = datetime.now()
                if not is_model_free_now(task.get("model", ""), state, cfg, now):
                    win = effective_free_window(state, task.get("model", ""))
                    log(f"任务 #{task['id']} 当前非免费时段/非免费模型"
                        f"（窗口 {win['start']}-{win['end']}），暂不发送")
                    return
            # 余额守卫：发送前读余额作为基线
            if auto.get("balance_guard", True):
                balance_before = read_balance(cfg)
                if balance_before is not None:
                    log(f"发送前余额基线: {balance_before}")
            attempts = int(task.get("attempts", 0))
            # 发送前通过 CDP 选择任务指定的模型（句柄/API 方式，等价人工点击）
            want_model = (task.get("model") or "").strip()
            if want_model:
                try:
                    import workbuddy_cdp
                    ok_m, why_m = workbuddy_cdp.select_model(want_model)
                    if ok_m:
                        log(f"已切换模型到 {want_model}")
                    else:
                        log(f"模型切换失败({why_m})，按当前模型发送")
                except Exception as e:
                    log(f"模型切换异常: {e}")
            ok, msg = auto_send_to_workbuddy(task["content"],
                                             auto.get("window_title", "WorkBuddy"))
            log(f"自动发送结果(第{attempts+1}次): {msg}")
            if not ok:
                attempts += 1
                if attempts >= 3:
                    # 重试上限：标记 failed，停止无限重试风暴
                    update_task(task["id"], status="failed", attempts=attempts, last_error=msg)
                    send_windows_notification(cfg.get("notify_title", "WorkBuddy 监控"),
                                              f"自动发送连续{attempts}次失败，已停止重试。请在界面中查看/重新入队: {task['content'][:50]}")
                    return
                update_task(task["id"], attempts=attempts, last_error=msg)
                send_windows_notification(cfg.get("notify_title", "WorkBuddy 监控"),
                                          f"自动发送失败({msg})，内容已在剪贴板，请手动粘贴。任务保留在队列中将重试: {task['content'][:50]}")
                return
        else:
            send_windows_notification(cfg.get("notify_title", "WorkBuddy 监控"),
                                      f"任务已恢复可发送：{task['content'][:80]}")
    task["status"] = "sent"
    # 余额守卫：发送成功后校验余额是否减少，减少则报警并全局暂停
    if auto.get("balance_guard", True) and balance_before is not None:
        verify_no_credit_charge(cfg, balance_before, task["id"])
    # 重新读盘合并写回，减少与 UI/其他进程的丢更新窗口
    state = load_state()
    for t in state.get("tasks", []):
        if t["id"] == task["id"]:
            t["status"] = "sent"
            break
    save_state(state)
    log(f"任务 #{task['id']} 已发送")

# ---------------------------------------------------------------------------
# 监控主循环
# ---------------------------------------------------------------------------
def check_rate_limits(state: dict, cfg: dict):
    """从监控源读取新消息，更新 state['rate_limits']"""
    src = cfg.get("watch_file", "")
    text = ""
    if src == "clipboard":
        try:
            ps = "Get-Clipboard -Raw"
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, text=True, timeout=10,
                               encoding="utf-8")
            text = r.stdout or ""
        except Exception:
            return
    elif src and Path(src).exists():
        text = Path(src).read_text(encoding="utf-8", errors="ignore")
    else:
        return

    model, reset, ok = parse_rate_limit_message(text)
    if ok:
        # 陈旧记录防护：重置时间已过去超过 24 小时的消息是历史残留，
        # 不写入限流记录，避免旧剪贴板内容触发"立即自动发送"
        if reset and (datetime.now() - reset).total_seconds() > 86400:
            log(f"忽略陈旧限流消息（重置时间 {reset} 已超过 24 小时）")
            return
        key = model or "default"
        entry = state["rate_limits"].setdefault(key, {})
        entry["model"] = model or key
        if reset:
            entry["reset"] = reset.strftime("%Y-%m-%d %H:%M:%S")
        entry["raw"] = text.strip()[:300]
        save_state(state)
        log(f"检测到限流: model={model} reset={reset}")


def balance_check(cfg: dict):
    """查询可选的余额 API，返回 (balance, usage) 或 (None, None)"""
    api = cfg.get("api", {})
    if not api.get("url"):
        return None, None
    try:
        import urllib.request
        req = urllib.request.Request(api["url"], method="GET")
        for k, v in api.get("headers", {}).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        bal = _to_number(data.get(api.get("balance_field", "balance")))
        usage = _to_number(data.get(api.get("usage_field", "usage")))
        return bal, usage
    except Exception as e:
        log(f"余额查询失败: {e}")
        return None, None


def _to_number(v):
    """把各种类型的余额值安全转成 float，失败返回 None"""
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def read_balance_clipboard():
    """从剪贴板解析积分余额：复制 WorkBuddy 余额页面的数字即可。
    返回 float 或 None"""
    try:
        if not user32.OpenClipboard(0):
            return None
        try:
            h = user32.GetClipboardData(13)
            text = ctypes.cast(h, ctypes.c_wchar_p).value if h else ""
        finally:
            user32.CloseClipboard()
        if not text:
            return None
        # 匹配第一个数字（支持 "1,234.5" "剩余 1000 积分" 等）
        m = re.search(r"-?\d[\d,]*(?:\.\d+)?", text)
        return _to_number(m.group(0)) if m else None
    except Exception:
        return None


def read_balance(cfg: dict):
    """统一余额入口：优先 WorkBuddy 官方 API（CDP 取 token），
    未配置/失败时回退 HTTP API 与剪贴板解析。返回 float 或 None"""
    if cfg.get("api_balance", True):
        try:
            import workbuddy_cdp
            info = workbuddy_cdp.read_credits()
            if info and info.get("left") is not None:
                return float(info["left"])
        except Exception:
            pass
    bal, _ = balance_check(cfg)
    if bal is not None:
        return bal
    return read_balance_clipboard()


_BALANCE_STATE_KEY = "balance_guard"


def in_free_window(cfg: dict, now=None, state=None, model=None) -> bool:
    """判断当前时间是否在免费时段内（支持跨午夜，如 23:00-08:00）。
    state/model 提供时优先用发现到的专属窗口；否则用配置窗口。
    未配置时默认启用 23:00-08:00。"""
    auto = cfg.get("auto_send", {})
    w = auto.get("free_window", {})
    if w.get("enabled", True) is False:
        return True  # 用户明确关闭了时段限制
    win = effective_free_window(state or {}, model) if (state is not None or model) else None
    if win:
        return in_window(win, now)
    # 无 state 时退回配置窗口
    return in_window({"start": str(w.get("start", "23:00")),
                      "end": str(w.get("end", "08:00"))}, now)


def next_free_window(cfg: dict, state=None, now=None):
    """返回下一个免费时段开始的 datetime（用于倒计时显示）。"""
    if state is not None:
        win = effective_free_window(state, None)
    else:
        win = {"start": str(cfg.get("auto_send", {}).get("free_window", {}).get("start", "23:00"))}
    try:
        sh, sm = map(int, str(win.get("start", "23:00")).split(":"))
    except (ValueError, AttributeError):
        return None
    now = now or datetime.now()
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    if now >= start:
        start += timedelta(days=1)
    return start


def balance_guard_paused(state: dict) -> bool:
    """余额守卫是否已触发暂停"""
    return bool(state.get(_BALANCE_STATE_KEY, {}).get("paused", False))


def set_balance_guard_paused(state: dict, paused: bool, reason: str = ""):
    """暂停/恢复自动发送（余额守卫触发时调用），写入磁盘"""
    fresh = load_state()
    g = fresh.setdefault(_BALANCE_STATE_KEY, {})
    g["paused"] = paused
    g["reason"] = reason
    g["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_state(fresh)
    if state is not None:
        state[_BALANCE_STATE_KEY] = g


def verify_no_credit_charge(cfg: dict, before: float, task_id) -> bool:
    """发送后余额校验：余额减少 = 消耗了积分 → 暂停自动发送并弹窗报警。
    返回 True=安全(余额未减少或无法读取)，False=已触发暂停。"""
    if before is None:
        return True  # 发送前没读到余额，无法判断（不误报）
    after = read_balance(cfg)
    if after is None:
        log("余额守卫：发送后未能读取余额，跳过本次校验")
        return True
    if after < before - 1e-9:
        reason = f"积分余额 {before} -> {after}"
        log(f"⚠ 余额守卫触发: {reason}，任务 #{task_id}，暂停自动发送")
        set_balance_guard_paused(None, True, reason)
        show_alert_popup(
            "⚠ 积分余额减少 - 自动发送已暂停",
            f"检测到发送任务后积分余额减少：\n\n{reason}\n\n"
            "自动发送已立即暂停以保护您的积分。\n"
            "请检查 WorkBuddy 是否弹出了积分确认，\n"
            "确认安全后可在监控界面重新启用。")
        return False
    log(f"余额校验通过: {before} -> {after}")
    return True


def run_loop(cfg: dict, once: bool = False):
    interval = max(5, int(cfg.get("poll_interval", 30)))
    log(f"开始监控 (interval={interval}s, mode={cfg.get('send_mode')})")
    # 启动时尝试从 WorkBuddy 底层发现免费模型与免费时段（失败仅记录，不影响后续）
    try:
        discover_free_models(cfg, None)
        discover_free_window(cfg, None)
    except Exception as e:
        log(f"初始免费发现失败(可忽略): {e}")
    while True:
        try:
            # 每轮从磁盘读最新状态，避免长期持有旧视图覆盖别人的写入
            state = load_state()
            check_rate_limits(state, cfg)

            now = datetime.now()
            # 余额守卫暂停中：跳过自动发送，但仍更新限流检测
            if balance_guard_paused(state):
                if once:
                    break
                time.sleep(interval)
                continue

            # 统一调度：遍历所有 pending 任务，到达发送条件即发送。
            # 条件由 task_sendable_now 判定：
            #   ① 限流重置时间已到   ② 免费模型处于免费时段   ③ 用户指定时间已到
            for t in pending_tasks(state):
                if task_sendable_now(t, state, cfg, now):
                    # 发送前二次确认仍是 pending（可能被其他进程/线程发送）
                    still = next((x for x in load_state().get("tasks", [])
                                  if x.get("id") == t.get("id") and x.get("status") == "pending"), None)
                    if still:
                        send_task(t, cfg)

            # 自动迭代：仅在免费时段内推进（免费额度保护的前提）
            if in_free_window(cfg, now, state):
                run_iteration_step(state, cfg, now)

            # 清理已过重置且无待发任务的限流记录（避免无限堆积）
            state = load_state()
            rl = state.get("rate_limits", {})
            if isinstance(rl, dict):
                changed = False
                for key, info in list(rl.items()):
                    reset_str = info.get("reset", "")
                    if not reset_str:
                        continue
                    try:
                        reset = datetime.strptime(reset_str, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                    if now >= reset and not pending_tasks(state, model=info.get("model", key)):
                        rl.pop(key, None)
                        changed = True
                if changed:
                    save_state(state)

            # 查询余额（可选）
            bal, usage = balance_check(cfg)
            if bal is not None:
                log(f"余额={bal} 用量={usage}")
        except KeyboardInterrupt:
            log("用户中断，退出")
            break
        except Exception as e:
            # 任何一轮的意外错误都不应杀死监控主循环
            log(f"监控轮询异常(已跳过本轮): {e}")

        if once:
            break
        time.sleep(interval)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="WorkBuddy 用量/余额监控")
    p.add_argument("--add", metavar="TEXT", help="添加任务")
    p.add_argument("--model", default="", help="任务关联的模型（可选）")
    p.add_argument("--at", metavar="TIME", default=None,
                   help="定时发送时间: 'YYYY-MM-DD HH:MM:SS' 或 'free'(下一免费时段)。空=立即入队/等免费时段")
    p.add_argument("--allow-credit", action="store_true",
                   help="允许在免费时段外消耗积分发送（谨慎，默认保护积分不消耗）")
    p.add_argument("--list", action="store_true", help="列出任务")
    p.add_argument("--status", action="store_true", help="查看限流状态")
    p.add_argument("--catalog", action="store_true", help="打印已发现的免费模型目录")
    p.add_argument("--discover", action="store_true", help="从 WorkBuddy 底层发现免费模型/时段并打印")
    # 自动迭代：在免费时段内反复执行任务，每轮根据上轮结果智能生成下一步优化提示词
    p.add_argument("--iterate", metavar="GOAL", help="开启自动迭代：填入目标/初始提示词")
    p.add_argument("--strategy", choices=ITER_STRATEGIES, default="improve",
                   help="迭代策略: staged(自定义阶段循环)/improve(内置渐进优化)/reflect(模型自主反思)")
    p.add_argument("--stages", metavar="A,B,C", default="", help="staged 策略的阶段列表（逗号分隔）")
    p.add_argument("--repo", metavar="PATH", default="",
                   help="代码自迭代：目标项目目录；指定后提示词会携带'读文件/改文件/跑测试'指令")
    p.add_argument("--test-cmd", metavar="CMD", default="",
                   help="代码自迭代：每次改动后运行的验证命令（默认 py_compile *.py）")
    p.add_argument("--max", type=int, default=0, metavar="N",
                   help="最大迭代轮数（0=无限，仅在免费时段内推进）")
    p.add_argument("--list-iter", action="store_true", help="列出自动迭代活动")
    p.add_argument("--stop-iter", type=int, metavar="ID", help="停止指定迭代活动")
    p.add_argument("--pause-iter", type=int, metavar="ID", help="暂停指定迭代活动")
    p.add_argument("--resume-iter", type=int, metavar="ID", help="恢复指定迭代活动")
    p.add_argument("--clear", action="store_true", help="清空已发送任务")
    p.add_argument("--parse", metavar="TEXT", help="解析一条限流消息（测试）")
    p.add_argument("--once", action="store_true", help="单次检查后退出")
    args = p.parse_args()

    cfg = load_config()

    if args.parse is not None:
        model, reset, ok = parse_rate_limit_message(args.parse)
        print(f"解析成功: {ok}\n模型: {model}\n重置时间: {reset}")
        return

    if args.discover:
        state = load_state()
        cat = discover_free_models(cfg, state, force=True)
        win = discover_free_window(cfg, state, force=True)
        if cat is None:
            print("（无法从 WorkBuddy 底层发现模型：WorkBuddy 未运行或 CDP 不可用）")
        else:
            print(f"已发现 {len(cat)} 个模型：")
            for c in cat:
                tag = "免费" if c["free"] else "积分"
                fw = c.get("free_window")
                extra = f" 时段{fw['start']}-{fw['end']}" if fw else ""
                print(f"  - {c['name']}  [{tag}]  badge={c['badge'] or '-'}{extra}")
        print(f"免费时段: {win['start']}-{win['end']} (来源: {win.get('source')})")
        return

    if args.catalog:
        state = load_state()
        cat = state.get("model_catalog") or []
        if not cat:
            print("(尚无模型目录，先运行 --discover 或启动监控自动发现)")
        else:
            for c in cat:
                print(f"  - {c['name']}  {'免费' if c['free'] else '积分'}  badge={c['badge'] or '-'}")
        return

    if args.add:
        state = load_state()
        sched = None
        if args.at:
            if args.at.strip().lower() == "free":
                nxt = next_free_window(cfg, state)
                sched = nxt.strftime("%Y-%m-%d %H:%M:%S") if nxt else None
            else:
                sched = args.at.strip()
        add_task(state, args.add, args.model, scheduled_time=sched,
                 allow_credit=args.allow_credit)
        if sched:
            print(f"已添加定时任务，将于 {sched} 发送")
        else:
            print("已添加任务（立即/等免费时段）")
        return

    if args.list:
        state = load_state()
        tasks = state.get("tasks", [])
        if not tasks:
            print("(队列为空)")
        for t in tasks:
            mark = "[done]" if t["status"] == "sent" else "[wait]"
            sched = t.get("scheduled_time") or "-"
            credit = " [允许积分]" if t.get("allow_credit") else ""
            print(f"{mark} #{t['id']} [{t['status']}] model={t.get('model','-')} "
                  f"定时={sched}{credit} {t['content']}")
        return

    if args.status:
        state = load_state()
        rl = state.get("rate_limits", {})
        if not rl:
            print("(无限流记录)")
        if not isinstance(rl, dict):
            rl = {}
        for k, v in rl.items():
            print(f"{k}: model={v.get('model')} reset={v.get('reset')}")
        fi = state.get("free_info", {}).get("window")
        if fi:
            print(f"免费时段: {fi.get('start')}-{fi.get('end')} (来源: {fi.get('source')})")
        return

    if args.clear:
        state = load_state()
        state["tasks"] = [t for t in state.get("tasks", []) if t["status"] != "sent"]
        save_state(state)
        print("已清理已发送任务")
        return

    if args.list_iter:
        state = load_state()
        camps = ensure_campaigns(state)
        if not camps:
            print("(无迭代活动，用 --iterate \"目标\" 创建)")
        for c in camps:
            maxs = "∞" if not c.get("max_iterations") else c["max_iterations"]
            print(f"#{c['id']} [{c['status']}] 轮次={c['current_iteration']}/{maxs} "
                  f"策略={c['strategy']} 模型={c.get('model') or '自动'}")
            print(f"   目标: {c.get('goal', '')[:60]}")
            if c.get("last_result"):
                print(f"   最近产出: {_truncate(c['last_result'], 80)}")
        return

    if args.stop_iter is not None:
        ok = set_campaign_status(load_state(), args.stop_iter, "done")
        print("已停止" if ok else "未找到该活动")
        return
    if args.pause_iter is not None:
        ok = set_campaign_status(load_state(), args.pause_iter, "paused")
        print("已暂停" if ok else "未找到该活动")
        return
    if args.resume_iter is not None:
        ok = set_campaign_status(load_state(), args.resume_iter, "running")
        print("已恢复" if ok else "未找到该活动")
        return

    if args.iterate:
        state = load_state()
        stages = [s for s in args.stages.split(",")] if args.stages else None
        camp = create_campaign(state, args.iterate, strategy=args.strategy,
                               model=args.model, stages=stages,
                               max_iterations=args.max,
                               repo_path=args.repo, test_cmd=args.test_cmd)
        print(f"已创建迭代活动 #{camp['id']}，目标={args.iterate!r}")
        print(f"策略={args.strategy} 最大轮数={'∞' if not args.max else args.max}")
        if camp.get("repo_path"):
            print(f"代码自迭代目录: {camp['repo_path']}  验证命令: {camp['test_cmd'] or '(默认 py_compile)'}")
        print("启动监控（python workbuddy_monitor.py 或界面点启动监控）后，将在免费时段内自动迭代。")
        return

    run_loop(cfg, once=args.once)


if __name__ == "__main__":
    main()