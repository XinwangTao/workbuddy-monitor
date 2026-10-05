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
import time
import threading
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


# ---------------------------------------------------------------------------
# 状态文件并发保护
# ---------------------------------------------------------------------------
# GUI 界面线程、CLI、监控线程都可能同时读写 state.json；"读-改-写"若不串行化，
# 后写者会用旧快照覆盖前者的更新（曾经实测：4 线程各加 20 个任务，最终只剩 20 个）。
# 用一个旁路锁文件 + 平台原生锁来串行化整个 read-modify-write 临界区。
_STATE_LOCK = BASE_DIR / ".state.lock"
try:  # Windows
    import msvcrt as _msvcrt

    def _lock_file(fh):
        _msvcrt.locking(fh.fileno(), _msvcrt.LK_LOCK, 1)

    def _unlock_file(fh):
        try:
            _msvcrt.locking(fh.fileno(), _msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
except ImportError:  # POSIX
    import fcntl as _fcntl

    def _lock_file(fh):
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)

    def _unlock_file(fh):
        try:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
        except OSError:
            pass


import contextlib


_STATE_LOCK_DEPTH = threading.local()


def _lock_depth() -> int:
    return getattr(_STATE_LOCK_DEPTH, "n", 0)


@contextlib.contextmanager
def state_lock():
    """串行化 state.json 的 read-modify-write（可重入）。

    同一线程内嵌套获取只计深度、只加一次文件锁：msvcrt/flock 的锁是进程级的，
    重复加锁会自锁等待（实测阻塞 ~10s），因此用线程本地计数器实现可重入锁。
    跨线程/跨进程仍然互斥。"""
    if getattr(_STATE_LOCK_DEPTH, "n", 0) > 0:
        _STATE_LOCK_DEPTH.n += 1
        try:
            yield
        finally:
            _STATE_LOCK_DEPTH.n -= 1
        return
    fh = None
    try:
        _STATE_LOCK.parent.mkdir(parents=True, exist_ok=True)
        fh = open(_STATE_LOCK, "a+")
        _lock_file(fh)  # 阻塞至获取；失败时退化为无锁，保证功能不被锁拖死
    except Exception:
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        fh = None
    _STATE_LOCK_DEPTH.n = 1
    try:
        yield
    finally:
        _STATE_LOCK_DEPTH.n = 0
        if fh is not None:
            try:
                _unlock_file(fh)
            except Exception:
                pass
            try:
                fh.close()
            except Exception:
                pass


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
    """原子写入：先写临时文件再替换，防止写到一半崩溃损坏 state.json。
    加锁避免并发 save 互相覆盖（内容层面仍需调用方持有 state_lock）。"""
    import os
    tmp = STATE_FILE.with_suffix(".tmp")
    with state_lock():
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)


def log(msg: str):
    """写日志：控制台 + monitor.log。
    UTF-8 with BOM：Windows 记事本/部分编辑器默认按 ANSI 解码，无 BOM 的中文会变乱码，
    首行写一次 BOM 保证双击打开可读。"""
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        print(line)
    except Exception:
        pass
    try:
        # 日志轮转：超过 1MB 归档为 monitor.old.log，防止无限增长
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            import shutil
            shutil.move(str(LOG_FILE), str(LOG_FILE.with_suffix(".old.log")))
        need_bom = (not LOG_FILE.exists()) or LOG_FILE.stat().st_size == 0
        with open(LOG_FILE, "a", encoding="utf-8-sig" if need_bom else "utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_heartbeat(state: dict, cfg: dict, now=None):
    """周期性心跳：让"昨晚监控是否在岗"可被事后审计，而不是只能靠推断。"""
    now = now or datetime.now()
    auto = cfg.get("auto_send", {}) or {}
    win = effective_free_window(state, None, cfg)
    in_win = in_window(win, now)
    paused = balance_guard_paused(state)
    active = [c for c in ensure_campaigns(state) if c.get("status") == "running"]
    pend = len(pending_tasks(state))
    parts = [
        "监控心跳",
        f"免费时段 {win['start']}-{win['end']}({'窗口内' if in_win else '窗口外'})",
        f"余额守卫={'暂停' if paused else '正常'}",
        f"活跃迭代={len(active)}",
        f"待发任务={pend}",
    ]
    if active:
        c = active[0]
        parts.append(f"活动#{c['id']} 已{c.get('current_iteration', 0)}轮"
                     f"{'(等待回复)' if c.get('awaiting_result') else ''}")
    log(" | ".join(parts))

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
#
# ⚠ 重要：「夜间免费」/「闲时免费」在 WorkBuddy 里并不等于不计费。
# 实测（2026-09-24 目录）：
#   Hy3            徽标「限时免费」 倍率 0.00x → 真免费
#   Hy4 preview    徽标「夜间免费」 倍率 0.29x → 夜间折扣价，仍按 0.29x 扣积分
# 因此「夜间免费/闲时免费」只是"夜间有折扣"的营销文案，不能单独作为免费证据；
# 必须同时满足倍率==0 或明确 "0积分" 才算免费。负向成本信号仍拥有最终否决权。
FREE_PATTERNS = [
    r"限免", r"free",
    r"(?<!\d)0\s*积分",   # 0积分 / 0 积分（排除 10积分 等正成本）
    r"免\s*积分",          # 免积分
    r"不计费",             # 不计费
    r"免费使用", r"完全免费",
]
# 「免费」这类宽泛词：仅当没有正向倍率/成本信号时才作为弱证据（见 classify_model）
WEAK_FREE_PATTERNS = [r"免费"]
# 描述"夜间折扣/闲时优惠"的词：明确表示仍计费，只是打折
DISCOUNT_HINTS = (r"夜间折扣", r"闲时优惠", r"折扣")
# 免费判定（徽标精确匹配）：短词做子串匹配会误判——
#   "x0.5"(半价) 含 "x0"、"避免限流" 含 "免"、"免疫" 含 "免"，
#   误判为免费会导致在非免费通道发送而真扣积分，因此这类短词只允许徽标全等匹配。
FREE_MODEL_BADGE_EXACT = ("x0", "×0", "免", "0", "0 积分", "free", "免费", "限免")
_POLICY_FREE_WINDOW = {"start": "23:00", "end": "08:00", "source": "policy"}


def _parse_multiplier(text: str):
    """从徽标/文本解析积分倍率。返回 float 或 None。

    WorkBuddy 实测格式（模型选择器 item 的 textContent 是**无空格拼接**的）：
      'Hy3限时免费0.00x'               → 0.0    （真免费）
      'Hy4 preview夜间免费0.29x'       → 0.29   （夜间折扣价，仍计费！）
      'Deepseek-V4-Pro 夜间折扣...0.51x' → 0.51
      'Kimi-K3...1.62x'                → 1.62
    支持四种写法：<数字>x / x<数字> / <数字>倍 / 倍率<数字>。
    必须带显式倍率标记（x/×/*/倍），否则会把模型名里的数字（如 'Hy4'、'GLM-5.3'、
    'Deepseek-V4.1'）误当成倍率，导致安全判定出错。"""
    if not text:
        return None
    s = str(text).strip()
    # 形式1：前缀标记 x2 / ×0.5 / *2 / 倍率2 / 倍 2
    m = re.search(r"(?:[xX×*]|倍率?|倍)\s*(\d+(?:\.\d+)?)", s)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    # 形式2：后缀写法 2x / 0.5倍 / 0.29X（WorkBuddy 真实格式，倍率在文本末尾）
    # 必须取**最后一个**匹配：模型名自带版本号时会把它们连在一起，
    # 如 'MiniMax-M30.25x'（名字 M3 + 倍率 0.25x）、'Kimi-K31.62x'。
    # 若取第一个会把 'M3' 当成倍率 30.25，导致免费/计费判定完全错误。
    hits = re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:[xX×]|倍)", s)
    if hits:
        try:
            return float(hits[-1])
        except ValueError:
            return None
    # 形式3：整个徽标本身就是一个数字（如 "0" "2" "0.5"），视为倍率
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
    # 倍率解析优先级：
    #   1) cost —— CDP 已剥掉模型名的计费后缀（最可靠，见 workbuddy_cdp._JS_LIST_MODELS）
    #   2) badge —— 徽标本身（如 "x2"）
    #   3) text —— 整行兜底（模型名含版本号时可能误判，故放最后）
    # 注意 "0.29x" / "0.29倍" 这类折后倍率是**真实计费倍率**，必须解析出来。
    cost = (item.get("cost") or "").strip()
    # cost 未提供（旧版 CDP/测试注入）时，本地从 text 里剥掉模型名再解析：
    # 'MiniMax-M30.25x' -> '0.25x'。否则 'M3' 会被误当成倍率 30.25。
    if not cost and name and name in text:
        cost = text.split(name, 1)[1].strip()
    multiplier = (_parse_multiplier(cost) or _parse_multiplier(badge)
                  or _parse_multiplier(cost) or _parse_multiplier(text))
    # 负向信号：检测到积分成本（倍率>0，或形如 "10积分" 的正成本）
    cost_m = re.search(r"(\d+(?:\.\d+)?)\s*积分", combined)
    positive_cost = ((multiplier is not None and multiplier > 0)
                     or (cost_m is not None and float(cost_m.group(1)) > 0))
    # 正向信号：强免费关键词（语义明确，不依赖倍率）
    badge_norm = badge.strip().lower()
    free_kw = any(re.search(p, combined, re.I) for p in FREE_PATTERNS)
    free_badge = badge_norm in FREE_MODEL_BADGE_EXACT
    zero_mult = (multiplier is not None and multiplier == 0)
    # 弱免费词："免费"字样 + 无任何计费信号；有倍率时必须倍率为 0
    weak_free = (not positive_cost) and any(
        re.search(p, combined, re.I) for p in WEAK_FREE_PATTERNS) and (
            multiplier is None or multiplier == 0)
    reasons = []
    if free_kw:
        reasons.append("免费关键词")
    if free_badge:
        reasons.append("免费徽标")
    if weak_free:
        reasons.append("免费字样(无计费倍率)")
    if zero_mult:
        reasons.append("倍率为0")
    positive = bool(free_kw or free_badge or zero_mult or weak_free)
    if positive_cost:
        # 安全优先：只要有正成本信号，一律判非免费并在原因里写明依据
        positive = False
        basis = f"倍率={multiplier}" if multiplier is not None else (
            f"文本成本={cost_m.group(1)}积分" if cost_m else "?")
        hint = next((h for h in DISCOUNT_HINTS if re.search(h, combined, re.I)), None)
        extra = f"（徽标'{badge}'属折扣文案，非免费）" if hint else ""
        reasons = [f"检测到积分成本({basis})→非免费{extra}"]
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
        # 未开启自动发现：仍以磁盘目录为准，但调用方无法确认其时效性，
        # 由门控依据时间戳判断可信度。
        return (state or {}).get("model_catalog")
    # 先确认 WorkBuddy/CDP 可用：不可用时返回 None（不要拿磁盘旧目录冒充"发现成功"，
    # 否则自愈会假成功、过期目录也会被当成可信放行）。
    try:
        import workbuddy_cdp
        if hasattr(workbuddy_cdp, "is_workbuddy_running") and not workbuddy_cdp.is_workbuddy_running():
            return None
    except Exception as e:
        log(f"发现免费模型失败(CDP 探测): {e}")
        return None
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
    # 记录时段发现时间戳，供门控判断"时段信息"新鲜度
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


def effective_free_window(state: dict, model: str = None, cfg: dict = None) -> dict:
    """返回用于判定的免费时段 {start, end, source}。

    优先级：
      1. 模型专属窗口（model_catalog 里该模型的 free_window）
      2. 从页面真实发现到的全局时段（free_info.window，source='discovered'）
      3. 调用方显式传入的配置 config.auto_send.free_window（测试注入/用户手改）
      4. 策略默认 23:00-08:00

    source='config' 的 free_info 条目只是配置值的缓存副本，没有信息增量，
    因此排在第 3 级之后，避免旧缓存覆盖用户后来的配置修改。"""
    if model:
        for m in (state or {}).get("model_catalog", []) or []:
            if m.get("name") == model and m.get("free_window"):
                return dict(m["free_window"])
    fi = (state or {}).get("free_info", {}) or {}
    win = fi.get("window")
    if win and win.get("start") and win.get("end") and win.get("source") == "discovered":
        return {"start": str(win["start"]), "end": str(win["end"]), "source": "discovered"}
    if isinstance(cfg, dict):
        w = (cfg.get("auto_send", {}) or {}).get("free_window", {}) or {}
        if w.get("start") and w.get("end"):
            return {"start": str(w["start"]), "end": str(w["end"]), "source": "config"}
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


# 免费目录数据的"保鲜期"：超过该时长未重新发现的模型/时段信息视为过期，
# 一律按 fail-closed 处理（暂停发送，等待重新发现），避免用过期的免费判断烧积分。
FREE_CATALOG_MAX_AGE = 6 * 3600


def _catalog_discovered_at(state: dict):
    """返回上次成功发现免费模型目录的时间；没有可用时间戳返回 None。"""
    fi = (state or {}).get("free_info", {}) or {}
    ts = fi.get("models_discovered_at")
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _catalog_age_seconds(state: dict) -> float:
    """model_catalog 距上次发现的秒数；无时间戳返回 inf（未知即过期）。"""
    dt = _catalog_discovered_at(state)
    if dt is None:
        return float("inf")
    return max(0.0, (datetime.now() - dt).total_seconds())


def catalog_is_fresh(state: dict, cfg: dict = None) -> bool:
    """免费模型目录是否仍在保鲜期内（是否可信）。"""
    max_age = FREE_CATALOG_MAX_AGE
    if isinstance(cfg, dict):
        try:
            max_age = int((cfg.get("auto_send", {}) or {}).get(
                "free_catalog_max_age", FREE_CATALOG_MAX_AGE))
        except (ValueError, TypeError):
            max_age = FREE_CATALOG_MAX_AGE
    return _catalog_age_seconds(state) <= max_age


FREE_REFRESH_INTERVAL = 3600  # 目录可信度自愈的最小间隔（秒），避免每轮刷 CDP
_last_free_refresh = {"at": 0.0}


def refresh_free_discovery(cfg: dict, state: dict, force: bool = False, min_interval: int = FREE_REFRESH_INTERVAL) -> bool:
    """当目录缺失/过期/无时间戳时，重新从 WorkBuddy 底层发现免费模型与时段。

    返回 True 表示目录现在可信。带节流：默认 1 小时内最多真正调一次 CDP，
    避免监控循环每 30 秒就去敲门把 WorkBuddy 打挂。"""
    trust = _catalog_trust(state, cfg)
    if trust == "fresh" and not force:
        return True
    now_ts = time.time()
    if not force and (now_ts - _last_free_refresh["at"]) < min_interval:
        return False
    _last_free_refresh["at"] = now_ts
    try:
        cat = discover_free_models(cfg, state)
    except Exception as e:
        log(f"免费目录自愈发现失败: {e}")
        return False
    if not cat:
        return False
    # 目录刷新成功后，顺带刷新时段发现（DOM 读不到时保留已有/配置时段）
    try:
        discover_free_window(cfg, state)
    except Exception as e:
        log(f"免费时段自愈发现失败(可忽略): {e}")
    return catalog_is_fresh(state, cfg)


def free_gate_reason(model: str, state: dict, cfg: dict, now=None) -> str:
    """返回空字符串=可以免费发送，否则返回不可发送的具体原因（可读、可写日志）。

    fail-closed 原则：任何"无法确认免费"的情况都必须返回非空原因。
    覆盖：目录缺失 / 目录过期 / 模型未发现 / 模型非免费 / 不在免费时段 /
          余额守卫暂停 / 模型被限流未到重置。"""
    now = now or datetime.now()
    reason = _gate_static_reason(model, state, cfg, now)
    return reason


def _gate_static_reason(model: str, state: dict, cfg: dict, now) -> str:
    if balance_guard_paused(state):
        g = (state.get(_BALANCE_STATE_KEY) or {})
        return f"余额守卫暂停中({g.get('reason') or '检测到积分消耗'})"
    # ① 限流闸门：模型仍被限流且未到重置时间，不发
    m = (model or "").strip()
    rl = state.get("rate_limits", {})
    if m and isinstance(rl, dict):
        info = rl.get(m) or (rl.get("default") if m else None)
        if isinstance(info, dict) and info.get("reset"):
            try:
                reset_dt = datetime.strptime(info["reset"], "%Y-%m-%d %H:%M:%S")
                if now < reset_dt:
                    return f"模型 {m} 限流中，重置时间 {reset_dt:%Y-%m-%d %H:%M:%S}"
            except ValueError:
                pass  # 时间戳非法：不据此阻断，交给后续免费判定
    # ② 免费资格
    auto = (cfg or {}).get("auto_send", {})
    if auto.get("free_window", {}).get("enabled", True) is False:
        return ""  # 用户显式关闭时段限制
    trust = _catalog_trust(state, cfg)
    if trust == "missing":
        return "尚无免费模型目录（未发现或发现失败），fail-closed 暂停发送"
    if trust == "stale":
        dt = _catalog_discovered_at(state)
        age_h = _catalog_age_seconds(state) / 3600.0
        return (f"免费模型目录已过期（发现于 {dt:%m-%d %H:%M}，约 {age_h:.1f} 小时前，"
                f"超过保鲜期），fail-closed 暂停发送")
    if trust == "unmarked":
        return ("免费模型目录缺少发现时间戳，无法确认时效性，fail-closed 暂停发送"
                "（请运行一次发现：界面「🔍 发现免费」或启动监控自动发现）")
    cat = state.get("model_catalog") or []
    hit = next((x for x in cat if x.get("name") == m), None)
    if hit is None:
        return f"模型 {m or '(未指定)'} 不在已发现目录中，无法确认免费资格，暂停发送"
    if not hit.get("free"):
        return (f"模型 {m} 非免费模型（{hit.get('free_reason') or '未标记免费'}），"
                f"free_only 下禁止发送，避免消耗积分")
    # 倍率明确为 0（"限时免费"类活动模型，DOM 标注 0.00x）：全天免费，不看时段窗口。
    # 夜间折扣类模型（0.29x 等）已在上面被判为非免费，不会走到这里。
    # 注意：multiplier 为 None 表示"未解析到倍率"，不属于已知免费，
    # 必须继续走时段窗口判定（宁可多等，也不能误判成全天免费）。
    if hit.get("multiplier") == 0:
        return ""
    win = effective_free_window(state, m, cfg)
    if not in_window(win, now):
        return (f"模型 {m} 的免费时段为 {win['start']}-{win['end']}，当前不在免费时段")
    return ""


def _catalog_trust(state: dict, cfg: dict = None) -> str:
    """给模型目录打可信度标签：'fresh' / 'stale' / 'missing' / 'unmarked'。

    unmarked = 目录里有模型但没有 models_discovered_at 时间戳（如旧版本状态文件、
    手工构造的测试状态）。这种情况不能直接当可信，但也不该永久锁死：
    调用方应触发一次重新发现，只有重新发现成功才能转为 fresh。"""
    cat = (state or {}).get("model_catalog") or []
    if not cat:
        return "missing"
    if _catalog_discovered_at(state) is None:
        return "unmarked"
    return "fresh" if catalog_is_fresh(state, cfg) else "stale"


def is_model_free_now(model: str, state: dict, cfg: dict, now=None) -> bool:
    """model 当前是否处于免费可用状态（已发现为免费 且 在免费时段内）。
    strict 模式（默认）：目录缺失/过期、模型未发现、模型非免费、不在时段内 → False。
    这是安全关键函数：宁可误留（不发）也不可误发（扣积分）。"""
    return free_gate_reason(model, state, cfg, now) == ""


def task_sendable_now(task: dict, state: dict, cfg: dict, now=None) -> bool:
    """判断一个 pending 任务此刻是否可以发送。

    免费额度保护（free_only，默认开启）是硬性前提：
    只有当前模型能被确认为"免费"时才允许发送。允许消耗积分的任务只有在
    auto_send.allow_credit_tasks（默认 False，需人工显式打开）+ free_only=False
    同时成立时才会被放行；只要 free_only 为真，任何任务都不允许走积分通道。
    这确保"自动迭代/任务队列"永远不会在免费额度用尽后偷偷扣积分。"""
    now = now or datetime.now()
    auto = cfg.get("auto_send", {}) or {}
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
    # ② 免费资格闸门（含余额守卫暂停与限流未重置的判定）
    reason = free_gate_reason(task.get("model", ""), state, cfg, now)
    if reason:
        if free_only:
            return False
        # free_only=false（用户明确关闭免费保护）时，仅允许显式 allow_credit 任务通行，
        # 且必须不在"模型被限流"或"余额守卫暂停"这类硬阻断状态中。
        if not task.get("allow_credit"):
            return False
        if balance_guard_paused(state):
            return False
        m = (task.get("model") or "").strip()
        rl = state.get("rate_limits", {})
        if m and isinstance(rl, dict):
            info = rl.get(m) or rl.get("default")
            if isinstance(info, dict) and info.get("reset"):
                try:
                    reset_dt = datetime.strptime(info["reset"], "%Y-%m-%d %H:%M:%S")
                    if now < reset_dt:
                        return False
                except ValueError:
                    pass
    return True


def send_block_reason(task: dict, state: dict, cfg: dict, now=None) -> str:
    """返回阻止发送的原因；空字符串表示可发。供界面/日志展示，便于排查。"""
    now = now or datetime.now()
    sched = task.get("scheduled_time")
    if sched:
        try:
            sched_dt = datetime.strptime(sched, "%Y-%m-%d %H:%M:%S")
            if now < sched_dt:
                return f"未到定时时间 {sched}"
        except ValueError:
            pass
    return free_gate_reason(task.get("model", ""), state, cfg, now)


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
    with state_lock():
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
    with state_lock():
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
ITER_STRATEGIES = ("staged", "improve", "reflect", "autopilot")
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


# ---------------------------------------------------------------------------
# 自动迭代引擎·健壮性常量与辅助（阶段2：停滞检测 / 错误自愈 / 回复捕获）
# ---------------------------------------------------------------------------
REPLY_CACHE_FILE = "campaign_reply.txt"   # 回复捕获回退文件：CDP 读不到时由接收方写入
AWAIT_RESULT_TIMEOUT = 1800               # 在途回复最长等待秒数；超时按失败自愈，防死锁


def _await_result_timeout(cfg):
    try:
        return int((cfg.get("auto_send", {}) or {}).get("await_timeout") or AWAIT_RESULT_TIMEOUT)
    except (ValueError, TypeError):
        return AWAIT_RESULT_TIMEOUT


def _await_stale(camp, cfg, now):
    """在途回复是否已超过等待上限（用于防死锁自愈）。无时间戳则视为未超时。"""
    ts = camp.get("awaiting_result_at")
    if not ts:
        return False
    try:
        dt = _parse_dt(ts)
    except Exception:
        return False
    if dt is None:
        return False
    return (now - dt).total_seconds() > _await_result_timeout(cfg)


def _reply_cache_path(cfg):
    p = (cfg.get("auto_send", {}) or {}).get("reply_cache_file") or REPLY_CACHE_FILE
    return Path(p)


def _read_reply_cache_file(camp, cfg):
    """回退源：读取回复缓存文件（CDP DOM 读不到时，可让接收方把最终回复写入此文件）。"""
    try:
        fp = _reply_cache_path(cfg)
        if fp.is_file():
            txt = fp.read_text(encoding="utf-8", errors="ignore").strip()
            if txt:
                return txt
    except Exception:
        pass
    return None


def _repo_has_code_change(repo):
    """repo 指向 git 仓库时，返回本轮是否有 .py 代码改动；非 git 仓库/异常返回 None。
    用于停滞检测增强：若连续多轮只说话不改代码，判定为无进展。"""
    try:
        rp = Path(repo)
        if not rp.is_dir() or not (rp / ".git").exists():
            return None
        out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                             cwd=str(rp), capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return None
        changed = [ln for ln in out.stdout.splitlines()
                   if ln.strip() and ln.strip()[2:].endswith(".py")]
        if changed:
            return True
        out2 = subprocess.run(["git", "diff", "--stat", "HEAD", "--", "*.py"],
                             cwd=str(rp), capture_output=True, text=True, timeout=15)
        if out2.returncode == 0 and out2.stdout.strip():
            return True
        return False
    except Exception:
        return None


def ensure_campaigns(state):
    state.setdefault("campaigns", [])
    for c in state["campaigns"]:
        c.setdefault("history", [])
        c.setdefault("stage_index", 0)
        c.setdefault("current_iteration", 0)
        c.setdefault("error_count", 0)
        c.setdefault("awaiting_result", False)
        c.setdefault("awaiting_result_at", "")
        c.setdefault("last_task_id", None)
        c.setdefault("no_code_change_count", 0)
        c.setdefault("last_prompt", "")
        c.setdefault("last_result", "")
        c.setdefault("stages", [])
        c.setdefault("pause_reason", "")
        c.setdefault("paused_at", "")
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
    # 与磁盘对齐后再追加：campaigns 可能已被 GUI/其他进程改动，
    # 直接用调用方的旧列表写回会丢活动，也会造成同 id 重复条目。
    fresh = load_state()
    fresh_camps = ensure_campaigns(fresh)
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
        "awaiting_result_at": "",
        "error_count": 0,
        "stagnation": False,
        "no_code_change_count": 0,
        "next_step": "",          # autopilot：模型上一轮自己给出的下一步方向
        "next_step_missing": 0,   # autopilot：连续多少轮模型没写 [[NEXT]]
        "history": [],
        "pause_reason": "",
        "paused_at": "",
    }
    fresh_camps.append(camp)
    save_state(fresh)
    # 同步回调用方视图：保证内存与磁盘指向同一批对象，避免 campaign_by_id 命中旧副本
    if state is not fresh:
        state["campaigns"] = fresh["campaigns"]
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
            # 恢复时清空在途状态与暂停原因，下一轮重新生成任务
            c["awaiting_result"] = False
            c["last_task_id"] = None
            c["pause_reason"] = ""
            c["paused_at"] = ""
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
    if strat == "autopilot":
        # autopilot：下一步方向直接来自模型上一轮用 [[NEXT]]..[[/NEXT]] 输出
        label = camp.get("next_step") or ""
        if not label:
            prompt = _build_autopilot_prompt(camp, it, last, failed)
            return _apply_code_tail(prompt, camp, NEXT_STEP_FALLBACK), "自主决策(首轮)"
        prompt = (f"项目目标：{goal}\n" if goal else "") + (
            f"这是第 {it} 轮自主迭代。")
        prompt += ("\n上一轮产出（节选）：\n" + _truncate(last, 600) + "\n"
                   if last else "")
        prompt += ("⚠ 上一轮似乎没有顺利完成，请先定位并修复。\n" if failed else "")
        prompt += f"\n本轮任务（由你上一轮自己决定）：\n{label}\n请直接动手完成它。"
        return _apply_code_tail(prompt, camp, label), label
    # improve（默认）
    angle = _IMPROVE_ANGLES[min(it - 1, len(_IMPROVE_ANGLES) - 1)]
    label = angle
    prompt = f"{base}\n本轮优化方向：{angle}\n请围绕该方向改进当前项目。"
    return _apply_code_tail(prompt, camp, label), label


# ===========================================================================
# 自动迭代引擎·「autopilot」策略：让 WorkBuddy 自己决定下一步方向
#
# 旧策略（staged/improve/reflect）的下一步提示词都由本程序套模板生成，
# 「方向」这件事始终握在程序手里；reflect 也只是"模型自己挑一个改进点"，
# 下一轮模板仍然覆盖了它的判断。
#
# autopilot 的做法：每轮提示词明确要求模型在**同一轮回复的末尾**用固定标记
# 输出「下一步方向」，程序只要解析出这段标记，就直接把它作为下一轮提示词的正文。
# 于是：下一步做什么、做到什么程度、用什么顺序，全部由 WorkBuddy 自己决定，
# 程序只负责调度（发送 / 捕获回复 / 推进 / 免费门控），不再主观注入方向。
# ===========================================================================
NEXT_STEP_OPEN = "[[NEXT]]"     # 模型输出下一步方向的开始标记
NEXT_STEP_CLOSE = "[[/NEXT]]"   # 结束标记
NEXT_STEP_FALLBACK = "继续推进项目目标，针对上轮最明显的不足之处做一件关键改进并汇报结果。"


def _build_autopilot_prompt(camp, it: int, last: str, failed: bool) -> str:
    """构造 autopilot 策略的本轮提示词。

    关键点：
    - 要求模型自己评估现状并决策下一步方向，而不是由程序指定；
    - 必须用 [[NEXT]]..[[/NEXT]] 把「下一步方向」原样输出，程序据此生成下一轮；
    - 明确禁止超出免费通道的动作（不要触发多模态/外部网络等可能计费的功能）。"""
    goal = camp.get("goal", "") or ""
    parts = [
        (f"项目目标：{goal}\n" if goal else ""),
        f"这是第 {it} 轮自主迭代，你现在拥有完全自主权：**下一步做什么由你自己决定**。",
        "",
        "—— 你的任务 ——",
        "1) 先评估当前进展：对照上面给的目标，判断上一轮产出是否达标、还缺什么、",
        "   哪一件事对推进目标最有价值；",
        "2) 然后**直接动手**完成你自己选定的这一件事（读文件 / 改文件 / 做实验都行），",
        "   最后汇报你实际做了什么、结果如何；",
        "3) 在回复的最末尾，用下面这对标记**原样输出下一步的方向**（不要转义、不要加序号）：",
        NEXT_STEP_OPEN,
        NEXT_STEP_CLOSE,
        "   标记内写一句明确的行动指令，说明下一轮该做什么，例如：",
        f"   {NEXT_STEP_OPEN}补齐 parse_rate_limit 的边界测试并跑通全量回归{NEXT_STEP_CLOSE}",
        "",
        "—— 约束 ——",
        "- 只做纯文本/代码层面的改动，不要触发消耗积分的多模态功能；",
        "- 只做一件最关键的事，做到位即可，不要贪多；",
        "- 汇报要简短：做了什么、结果如何（通过/失败）。",
    ]
    if last:
        if failed:
            parts.insert(4, "⚠ 上一轮似乎没有顺利完成，请先定位并修复问题再继续。\n"
                            f"上一轮产出（节选）：\n{_truncate(last, 600)}\n")
        else:
            parts.insert(4, f"上一轮产出（节选）：\n{_truncate(last, 600)}\n")
    return "\n".join(parts)


def extract_next_step(text: str):
    """从模型回复中解析 [[NEXT]]..[[/NEXT]]。

    返回 (下一步方向文本 或 None, 原因)。解析不到/内容过短时返回 (None, reason)。
    只有当提取出的方向足够明确（>=8 个字符）才认为有效，
    否则退回内置兜底方向——绝不因为模型漏写标记就让迭代停摆。"""
    if not text:
        return None, "empty"
    m = re.search(re.escape(NEXT_STEP_OPEN) + r"(.*?)\s*" + re.escape(NEXT_STEP_CLOSE),
                  text, re.S)
    if not m:
        return None, "no-marker"
    raw = m.group(1).strip().strip("`").strip()
    raw = re.sub(r"^[#>\-\s]+", "", raw)
    if len(raw) < 8:
        return None, "too-short"
    return raw, ""


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
    """收到一次迭代结果后推进：记录历史、轮次+1、错误计数、停止条件判定。

    并发安全：内部按 id 把本活动的字段合并回磁盘最新状态，
    而不是整体 save_state(调用方视图) —— 否则会覆盖掉其他进程/GUI 新建的活动
    （实测：用只含 1 个活动的旧内存视图写回，会丢掉后来创建的第二个活动）。"""
    camp["current_iteration"] += 1
    camp["history"].append({
        "iter": camp["current_iteration"],
        "prompt": camp.get("last_prompt", ""),
        "result": _truncate(result, 600),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    camp["last_result"] = result or camp.get("last_result", "")
    if camp.get("strategy") == "autopilot":
        # 从模型回复里取它自己决定的下一步方向；漏写则累计，
        # 连续漏写达到上限就暂停，避免无限空转烧免费额度。
        step, why = extract_next_step(result)
        if step:
            camp["next_step"] = step
            camp["next_step_missing"] = 0
            log(f"迭代活动 #{camp['id']} 模型自定下一步方向: {step[:80]}")
        else:
            camp["next_step_missing"] = camp.get("next_step_missing", 0) + 1
            log(f"迭代活动 #{camp['id']} 本轮未给出下一步方向({why})，"
                f"连续 {camp['next_step_missing']} 次；将使用兜底方向续跑")
    stages = camp.get("stages") or _IMPROVE_ANGLES
    camp["stage_index"] = (camp["stage_index"] + 1) % max(1, len(stages))
    camp["awaiting_result"] = False
    camp["last_task_id"] = None
    if _looks_failed(result):
        camp["error_count"] = camp.get("error_count", 0) + 1
    else:
        camp["error_count"] = 0
    # 停滞检测增强：repo 模式检测"多轮只说话不改代码"
    repo = camp.get("repo_path")
    if repo and not _looks_failed(result):
        changed = _repo_has_code_change(repo)
        if changed is False:
            camp["no_code_change_count"] = camp.get("no_code_change_count", 0) + 1
        elif changed is True:
            camp["no_code_change_count"] = 0
    # 停止条件。多个条件可能同时命中，必须保留**最根本的那个**原因：
    # 若直接 setdefault 会让先命中的条件占据 pause_reason，真正的问题被掩盖，
    # 用户按提示去排查却找不到病因。因此先收集原因，只写第一个。
    reasons = []
    if camp["max_iterations"] and camp["current_iteration"] >= camp["max_iterations"]:
        camp["status"] = "done"
        log(f"迭代活动 #{camp['id']} 达到最大轮数 {camp['max_iterations']}，已结束")
    else:
        hist = camp.get("history", [])
        if camp["error_count"] >= 3:
            reasons.append("连续 3 轮检测到失败")
        if len(hist) >= 2:
            prev = hist[-2].get("result", "") or ""
            cur = hist[-1].get("result", "") or ""
            if prev and cur and _similar(prev, cur):
                reasons.append("连续 2 轮输出几乎相同（无进展）")
        if camp.get("next_step_missing", 0) >= 3:
            reasons.append("连续 3 轮未给出下一步方向（模型未输出 [[NEXT]] 标记）")
        if camp.get("no_code_change_count", 0) >= 2:
            reasons.append(f"连续 {camp['no_code_change_count']} 轮无代码改动")
        if reasons:
            camp["status"] = "paused"
            # 首个原因即根因；同时把所有命中的条件写进日志，便于一次看清
            camp["pause_reason"] = reasons[0]
            log(f"迭代活动 #{camp['id']} 已自动暂停：{reasons[0]}"
                + (f"（同时命中：{'、'.join(reasons[1:])}）" if len(reasons) > 1 else ""))
            if "输出几乎相同" in reasons[0] or "无代码改动" in reasons[0]:
                camp["stagnation"] = True
    # 锁内按 id 合并写回，避免整体覆盖导致其他活动丢失
    with state_lock():
        fresh = load_state()
        for i, c in enumerate(ensure_campaigns(fresh)):
            if c.get("id") == camp.get("id"):
                fresh["campaigns"][i] = camp
                break
        else:
            fresh.setdefault("campaigns", []).append(camp)
        save_state(fresh)
        if state is not None:
            state["campaigns"] = fresh["campaigns"]


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
    camp["awaiting_result_at"] = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    camp["last_task_id"] = task["id"]
    save_state(state)
    log(f"迭代活动 #{camp['id']} 已生成第 {camp['current_iteration'] + 1} 轮任务"
        f"（方向：{label}）模型={model or '自动(免费时段)'}")
    return task


def try_capture_campaign_result(camp, cfg, timeout=240, poll=15):
    """尝试读取 WorkBuddy 对 camp.last_task_id 的回复。返回文本或 None（未就绪/超时/不可用）。

    捕获顺序：① WorkBuddy CDP DOM ② 回复缓存文件（CDP 读不到时由接收方写入）。

    hint 用 camp.last_prompt：WorkBuddy 发送后会回到会话列表，必须能定位回
    刚才那条会话，否则会读到别的会话的回复，把无关内容当成本轮迭代产出。"""
    hint = (camp.get("last_prompt") or "").strip()
    try:
        import workbuddy_cdp
        msg = workbuddy_cdp.last_assistant_message(
            timeout=timeout, poll=poll, prompt_hint=hint)
        if msg:
            return msg
    except Exception as e:
        log(f"迭代读取 WorkBuddy 回复失败(CDP, 尝试回退源): {e}")
    # 回退源：回复缓存文件
    cached = _read_reply_cache_file(camp, cfg)
    if cached:
        log("迭代回复改由缓存文件捕获（CDP DOM 不可用）")
        return cached
    return None


def iteration_free_block(camp, state, cfg, now=None) -> str:
    """活动能否在"零积分消耗"前提下继续推进。空字符串=可以，否则为暂停原因。

    只认当前已发现且保鲜的免费目录里的免费模型；模型未知/目录过期/被限流/
    不在免费时段/余额守卫暂停 → 一律返回原因（调用方据此暂停活动，不发下一轮）。"""
    now = now or datetime.now()
    model = _effective_model(camp, state)
    if not model:
        return "未能确定可用的免费模型（目录为空或全部非免费）"
    return free_gate_reason(model, state, cfg, now)


def run_iteration_step(state, cfg, now=None):
    """执行一轮自动迭代步进。调用方需保证当前处于免费时段（免费额度保护前提）。
    已发送任务会在此阻塞等待 WorkBuddy 回复（最长 timeout 秒，轮询），拿到结果后立即
    生成下一轮任务——这样在一个免费时段内可连续多轮迭代。

    fail-closed：任何一轮只要无法确认下一步仍走免费通道，就暂停活动、不再生成新任务。"""
    now = now or datetime.now()
    for camp in active_campaigns(state):
        # 0) 免费资格门控：无法确认免费 → 暂停活动，绝不生成/发送下一轮
        block = iteration_free_block(camp, state, cfg, now)
        if block:
            _pause_campaign_for_reason(camp, state, block)
            continue

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
                elif _await_stale(camp, cfg, now):
                    # 防死锁自愈：在途回复超过等待上限仍未捕获到（CDP/网络异常或回复捕获失败），
                    # 以"无回复"作为失败推进，触发错误计数→连续多次后自动暂停，避免无限空转。
                    log(f"迭代活动 #{camp['id']} 在途回复超时，按失败自愈并续跑")
                    advance_campaign(
                        camp,
                        "【引擎自愈】在途任务超过等待上限仍未捕获到回复"
                        "（可能 CDP/网络异常或回复捕获失败），本轮记为失败。",
                        state)
                    if camp.get("status") == "running":
                        ensure_campaign_task(camp, state, cfg, now)
                # 未超时也未捕获（CDP 不可用或仍在流式输出）则保持 awaiting，下轮继续
                continue
            else:
                # 仍在 pending：等统一调度循环发送
                continue
        # 2) 需要下一轮任务
        ensure_campaign_task(camp, state, cfg, now)


def _pause_campaign_for_reason(camp, state, reason: str):
    """因安全/额度原因暂停活动：只有当原因变化时才写日志与状态，避免每轮重复刷屏。"""
    prev = camp.get("pause_reason", "")
    if camp.get("status") == "running" or prev != reason:
        camp["status"] = "paused"
        camp["pause_reason"] = reason
        camp["paused_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log(f"迭代活动 #{camp['id']} 已暂停：{reason}")
    save_state(state)


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
# _activate_window 加固用：显式原型，避免 64 位指针截断
user32.SetForegroundWindow.restype = ctypes.c_int
user32.SetForegroundWindow.argtypes = (ctypes.c_void_p,)
user32.GetWindowThreadProcessId.restype = ctypes.c_uint
user32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint))
user32.AttachThreadInput.restype = ctypes.c_int
user32.AttachThreadInput.argtypes = (ctypes.c_uint, ctypes.c_uint, ctypes.c_int)
kernel32.GetCurrentThreadId.restype = ctypes.c_uint
kernel32.GetCurrentThreadId.argtypes = ()
user32.BringWindowToTop.restype = ctypes.c_int
user32.BringWindowToTop.argtypes = (ctypes.c_void_p,)


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
    """还原并前置窗口（对 Windows 前台锁定多重加固）。

    Windows 限制后台进程调用 SetForegroundWindow 抢焦点。多层降级：
    1. ALT 键事件让系统认为有新输入，解除前台锁定超时；
    2. 失败则把调用线程同时附加到「当前前台窗口线程」和「目标窗口线程」，
       共享输入状态后再切换（经典绕过，最稳）；
    3. 全部失败返回 False，调用方据此取消发送，绝不盲发。"""
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # ALT 键 up/down 各一次，绕过 SetForegroundWindow 限制
    user32.keybd_event(0x12, 0, 0, 0)   # ALT down
    user32.keybd_event(0x12, 0, 2, 0)   # ALT up
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    if user32.GetForegroundWindow() == hwnd:
        return True
    time.sleep(0.4)
    if user32.GetForegroundWindow() == hwnd:
        return True
    # 降级：同时附加到前台线程 + 目标线程，共享输入状态
    cur_tid = kernel32.GetCurrentThreadId()
    fg_hwnd = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
    tgt_tid = user32.GetWindowThreadProcessId(hwnd, None)
    attached = []
    for t in (fg_tid, tgt_tid):
        if t and t != cur_tid and user32.AttachThreadInput(cur_tid, t, True):
            attached.append(t)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        for t in attached:
            user32.AttachThreadInput(cur_tid, t, False)
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


def _editor_has_text(needle: str) -> bool:
    """CDP 检查 WorkBuddy 输入框里是否真的出现了待发送内容（前 40 字匹配即可）。

    没有 CDP 时返回 True（不阻塞发送，由调用方按旧行为处理）。
    这一步是必要兜底：实测出现过「窗口已激活但焦点不在输入框」导致
    Ctrl+V 落空、随后 Enter 把空消息发出去的情况——那种静默丢消息
    比发送失败危险得多，因为按设计 cpyd 之后任务会被标记 sent。"""
    if not needle:
        return True
    try:
        import workbuddy_cdp
        if not workbuddy_cdp.is_workbuddy_running():
            return True
        head = needle.strip()[:40]
        return bool(workbuddy_cdp.editor_contains(head))
    except Exception:
        return True


def auto_send_to_workbuddy(content: str, window_title: str = "WorkBuddy",
                           model: str = "") -> tuple:
    """复制到剪贴板 → 激活 WorkBuddy → 粘贴 → 回车发送。

    返回 (ok, message)。发送后会校验「输入框已清空」或「对话里出现本轮内容」，
    两者至少满足其一才认为发送成功；否则返回 False，让上层把任务标回 pending
    重试，绝不静默丢消息。"""
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
    # 关键修复 1：激活窗口 ≠ 焦点在输入框。真实出现过「窗口在前台但
    # Ctrl+V 落空」的情况。用 CDP 直接把光标放回编辑器，再粘贴。
    # 关键修复 2：发送前必须回「新建任务」页。真实事故：WorkBuddy 停在
    # 历史会话时消息会粘进那个会话并沿用其计费模型（Hy3 被覆盖成
    # Deepseek-V4.1-Flash 白扣 9.17 积分）。
    try:
        import workbuddy_cdp
        if workbuddy_cdp.is_workbuddy_running():
            try:
                if workbuddy_cdp.start_new_conversation():
                    log("已回新建任务页，消息不会污染历史会话")
                else:
                    log("警告：未能确认新建任务页（可能无 CDP 或按钮未命中），继续发送")
            except Exception as e2:
                log(f"新建会话步骤异常（忽略）: {e2}")
            workbuddy_cdp.focus_editor()
            # 新会话的模型是会话级设置、不继承历史会话，必须重选。
            # 真实事故：新会话默认 Deepseek-V4.1-Flash（0.11x），白扣 9.17 积分。
            if model:
                ok_m, why_m = workbuddy_cdp.select_model(model)
                if not ok_m:
                    return False, f"新会话中模型重选失败: {why_m}"
                log(f"新会话中已重新选择模型 {model}")
        else:
            log("CDP 不可用，跳过新建会话步骤")
    except Exception as e:
        log(f"新建会话步骤异常（忽略，继续原流程）: {e}")
    _ctrl_v()
    time.sleep(1.0)  # 等输入框渲染完粘贴内容
    # 关键兜底：确认粘贴真的落进了输入框，避免把空消息发出去
    if not _editor_has_text(content):
        # 重试一次：重新激活窗口 → 重新聚焦编辑器 → 再粘贴（焦点偶发丢失）
        _activate_window(hwnd)
        time.sleep(0.4)
        try:
            import workbuddy_cdp
            workbuddy_cdp.focus_editor()
        except Exception:
            pass
        _ctrl_v()
        time.sleep(1.0)
        if not _editor_has_text(content):
            return False, "粘贴未生效（内容未进入输入框），已取消发送"
    _enter()
    time.sleep(0.8)
    # 还原用户剪贴板
    if prev_clip:
        set_clipboard_text(prev_clip)
    # 发送后确认：输入框应被清空（消息已提交）
    if _editor_still_has_text(content):
        return False, "回车后输入框仍有内容（消息未提交），已保留任务待重试"
    # 真实投递校验：消息必须真的出现在对话里，否则只是“假成功”
    # （粘贴/提交可能静默失败，仅看输入框清空会误判为已发送）。
    if not _sent_message_present_in_conversation(content):
        return False, "回车后消息未出现在对话中（疑似未真正提交），已保留任务待重试"
    return True, "已自动粘贴并发送到 WorkBuddy"


def _sent_message_present_in_conversation(content: str, tries: int = 4) -> bool:
    """发送后确认消息确实进入了对话（存在含该内容的用户气泡）。

    仅看“输入框被清空”是必要不充分条件：粘贴可能根本没落地、或提交被静默吞掉，
    此时输入框本就是空的，原校验会误判为已发送。这里直接去对话里找这条用户消息。

    读不到对话（无 CDP / 已回列表且打不开）或离线测试桩缺少 CDP 能力时，
    保守返回 True，不误判失败、也不影响无 CDP 的回退路径。"""
    try:
        import workbuddy_cdp
    except Exception:
        return True
    if not hasattr(workbuddy_cdp, "read_conversation"):
        return True  # 离线测试桩无 CDP 能力，保守放行
    head = (content or "").strip()[:40]
    if not head:
        return True
    for _ in range(max(1, tries)):
        conv = None
        try:
            if workbuddy_cdp._in_conversation_page():
                conv = workbuddy_cdp.read_conversation()
            else:
                # 发送后可能回到会话列表：打开最新会话再读
                workbuddy_cdp._open_latest_conversation()
                time.sleep(0.8)
                conv = (workbuddy_cdp.read_conversation()
                        if workbuddy_cdp._in_conversation_page() else None)
        except Exception:
            conv = None
        if conv and any(m.get("role") == "user"
                       and head in (m.get("text") or "") for m in conv):
            return True
        # 兜底：按内容前缀定位到刚发的那条会话再确认一次
        try:
            for kw in (head[:24], head[:16], head[:8]):
                if workbuddy_cdp._open_conversation_by_title(kw):
                    time.sleep(1.0)
                    if workbuddy_cdp._in_conversation_page():
                        conv = workbuddy_cdp.read_conversation()
                        if conv and any(m.get("role") == "user"
                                       and head in (m.get("text") or "") for m in conv):
                            return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _editor_still_has_text(needle: str) -> bool:
    """发送后输入框里是否仍留着待发送内容（用于判断 Enter 是否真的提交了）。"""
    try:
        import workbuddy_cdp
        if not workbuddy_cdp.is_workbuddy_running():
            return False
        head = (needle or "").strip()[:40]
        return bool(head) and bool(workbuddy_cdp.editor_contains(head))
    except Exception:
        return False


def send_task(task: dict, cfg: dict):
    mode = cfg.get("send_mode", "notify")
    state = load_state()  # 发送前取最新状态（含 model_catalog / free_info）
    # 这两个变量在函数末尾的余额守卫中无条件引用，必须在此初始化，
    # 否则 command / api 模式会因未定义而抛 NameError，导致任务状态永远标不上 sent。
    auto = cfg.get("auto_send", {})
    balance_before = None  # 发送前余额基线（仅 notify 模式实际读取）
    log(f"发送任务 #{task['id']}: {task['content'][:50]} (mode={mode})")
    # 发送前用最新状态再校验一次免费资格（状态可能在入队后变化：
    # 目录过期 / 模型被限流 / 余额守卫暂停 / 免费时段已过）。
    if cfg.get("auto_send", {}).get("free_only", True) and not task.get("allow_credit"):
        block = free_gate_reason(task.get("model", ""), state, cfg)
        if block:
            log(f"任务 #{task['id']} 发送前拦截：{block}")
            return
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
                    if not ok_m:
                        why = f"模型切换失败: {why_m}"
                        log(f"任务 #{task['id']} {why}；为防止误用其他模型/消耗积分，已暂缓发送")
                        update_task(task["id"], last_error=why)
                        return
                    # CDP 的选择结果必须可验证；select_model 自身会核对当前模型。
                    log(f"已验证切换模型到 {want_model}")
                except Exception as e:
                    why = f"模型切换异常: {e}"
                    log(f"任务 #{task['id']} {why}；为防止误用其他模型/消耗积分，已暂缓发送")
                    update_task(task["id"], last_error=why)
                    return
            ok, msg = auto_send_to_workbuddy(task["content"],
                                             auto.get("window_title", "WorkBuddy"),
                                             model=want_model)
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
    win = (effective_free_window(state or {}, model, cfg)
           if (state is not None or model) else None)
    if win:
        return in_window(win, now)
    # 无 state 时退回配置窗口
    return in_window({"start": str(w.get("start", "23:00")),
                      "end": str(w.get("end", "08:00"))}, now)


def next_free_window(cfg: dict, state=None, now=None):
    """返回下一个免费时段开始的 datetime（用于倒计时显示）。"""
    if state is not None:
        win = effective_free_window(state, None, cfg)
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


def _cleanup_rate_limits(cfg: dict = None):
    """清理已过重置时间且无待发任务的限流记录（避免无限堆积）。在锁内合并写回。"""
    now = datetime.now()
    with state_lock():
        state = load_state()
        rl = state.get("rate_limits", {})
        if not isinstance(rl, dict) or not rl:
            return
        changed = False
        for key, info in list(rl.items()):
            reset_str = (info or {}).get("reset", "") if isinstance(info, dict) else ""
            if not reset_str:
                continue
            try:
                reset = datetime.strptime(reset_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if now >= reset and not pending_tasks(state, model=(info or {}).get("model", key)):
                rl.pop(key, None)
                changed = True
        if changed:
            save_state(state)


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


def startup_selfcheck(cfg: dict, state: dict = None) -> str:
    """启动自检：把运行环境与免费额度保护状态一次性写进日志，便于事后审计。
    返回一行人类可读摘要（也用于 CLI 打印）。"""
    state = state if state is not None else load_state()
    lines = []
    auto = cfg.get("auto_send", {}) or {}
    try:
        import workbuddy_cdp
        cdp_ok = workbuddy_cdp.is_workbuddy_running()
    except Exception:
        cdp_ok = False
        workbuddy_cdp = None
    lines.append("——— 启动自检 ———")
    lines.append(f"WorkBuddy/CDP: {'在线' if cdp_ok else '不可用（模型发现与余额读取将受限）'}")
    lines.append(f"免费保护: free_only={auto.get('free_only', True)} "
                 f"balance_guard={auto.get('balance_guard', True)}")
    lines.append(f"目录发现: models={auto.get('discover_free_models', True)} "
                 f"window={auto.get('discover_free_window', True)}")
    win = effective_free_window(state, None, cfg)
    lines.append(f"免费时段: {win['start']}-{win['end']} (来源: {win.get('source')})")
    lines.append(f"当前是否免费时段: {'是' if in_window(win) else '否'}")
    cat = state.get("model_catalog") or []
    free_names = [c.get("name") for c in cat if c.get("free")]
    lines.append(f"免费模型目录: 共 {len(cat)} 个 / 免费 {len(free_names)} 个 "
                 f"({', '.join(free_names) if free_names else '无'})  "
                 f"可信度={_catalog_trust(state, cfg)}")
    if balance_guard_paused(state):
        lines.append("余额守卫: ⛔ 已暂停（检测到过积分消耗或人工暂停）")
    active = [c for c in ensure_campaigns(state) if c.get("status") == "running"]
    if active:
        for c in active:
            lines.append(f"活跃迭代: #{c['id']} 已 {c.get('current_iteration', 0)} 轮 / 目标 {c.get('goal', '')[:40]}")
    else:
        lines.append("活跃迭代: 无")
    lines.append("———————————————")
    text = "\n".join(lines)
    for ln in lines:
        log(f"[自检] {ln}")
    return text


def monitor_tick(cfg: dict, now=None) -> dict:
    """单轮监控步骤（CLI 与 GUI 共用同一实现，避免两处逻辑漂移）。

    依次执行：读最新状态 → 限流检测 → 遍历待发任务（命中发送条件即发）
    → 免费时段内推进自动迭代（含目录自愈）→ 心跳。返回本轮摘要，供调用方
    决定是否记录日志/刷新界面。"""
    now = now or datetime.now()
    info = {"sent": 0, "blocked": 0, "iterated": False, "guard_paused": False,
            "in_free_window": False, "errors": []}
    state = load_state()
    try:
        check_rate_limits(state, cfg)
    except Exception as e:
        info["errors"].append(f"check_rate_limits: {e}")
        log(f"限流检测异常(已跳过): {e}")
    if balance_guard_paused(state):
        info["guard_paused"] = True
        return info
    # ① 统一调度发送
    try:
        pending = pending_tasks(state)
        for t in pending:
            if not task_sendable_now(t, state, cfg, now):
                info["blocked"] += 1
                continue
            # 双重保险：发送前从磁盘确认仍是 pending（可能已被其他进程发送）
            fresh = load_state()
            still = next((x for x in fresh.get("tasks", [])
                          if x.get("id") == t.get("id") and x.get("status") == "pending"), None)
            if not still:
                continue
            send_task(t, cfg)
            after = next((x for x in load_state().get("tasks", [])
                          if x.get("id") == t.get("id")), None)
            if after and after.get("status") == "sent":
                info["sent"] += 1
            else:
                # 未发送成功（被门控拦下/切换模型失败/发送器报错）：计入受阻，
                # 便于日志与界面回答"为什么这一轮没发出东西"。
                info["blocked"] += 1
            state = load_state()
    except Exception as e:
        info["errors"].append(f"send: {e}")
        log(f"任务调度异常(已跳过本轮): {e}")
    # ② 自动迭代（仅免费时段）
    try:
        info["in_free_window"] = in_free_window(cfg, now, state)
        if info["in_free_window"]:
            refresh_free_discovery(cfg, state)   # 目录不可信时按节流重新发现
            run_iteration_step(state, cfg, now)
            info["iterated"] = True
    except Exception as e:
        info["errors"].append(f"iteration: {e}")
        log(f"自动迭代异常(已跳过本轮): {e}")
    return info


def _maybe_heartbeat(cfg: dict, counter: list, interval: int = 600):
    """按间隔写心跳；counter 为 [上次时间戳] 的可变容器。"""
    now = time.time()
    if now - counter[0] >= interval:
        counter[0] = now
        try:
            log_heartbeat(load_state(), cfg)
        except Exception as e:
            log(f"心跳记录失败(可忽略): {e}")


def run_loop(cfg: dict, once: bool = False):
    interval = max(5, int(cfg.get("poll_interval", 30)))
    log(f"开始监控 (interval={interval}s, mode={cfg.get('send_mode')})")
    # 启动时尝试从 WorkBuddy 底层发现免费模型与免费时段（失败仅记录，不影响后续）
    try:
        discover_free_models(cfg, None)
        discover_free_window(cfg, None)
    except Exception as e:
        log(f"初始免费发现失败(可忽略): {e}")
    startup_selfcheck(cfg)
    _hb_at = [0.0]
    while True:
        try:
            info = monitor_tick(cfg)
            if info.get("sent"):
                log(f"本轮发送 {info['sent']} 个任务"
                    + (f"（{info['blocked']} 个待发任务仍被门控扣留）" if info.get("blocked") else ""))
            # 清理已过重置且无待发任务的限流记录（避免无限堆积）
            _cleanup_rate_limits(cfg)
            # 查询余额（可选）
            bal, usage = balance_check(cfg)
            if bal is not None:
                log(f"余额={bal} 用量={usage}")
            _maybe_heartbeat(cfg, _hb_at)
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
                   help="迭代策略: staged(自定义阶段循环)/improve(内置渐进优化)/"
                        "reflect(模型自主反思)/autopilot(下一步方向完全由模型自己决定)")
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
    p.add_argument("--doctor", action="store_true",
                   help="启动自检：WorkBuddy/CDP、免费保护开关、免费模型与时段、活跃迭代")
    p.add_argument("--report", action="store_true",
                   help="汇总近期工作情况：各迭代活动轮次/最近产出/是否被暂停及原因")
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

    if args.doctor:
        print(startup_selfcheck(cfg))
        return

    if args.report:
        state = load_state()
        camps = ensure_campaigns(state)
        if not camps:
            print("(无迭代活动)")
        pend = [t for t in state.get("tasks", []) if t.get("status") == "pending"]
        print(f"待发任务: {len(pend)} 个")
        if pend:
            for t in pend[:10]:
                why = send_block_reason(t, state, cfg)
                print(f"  ⏳ #{t['id']} model={t.get('model') or '-'} {t['content'][:40]!r}"
                      f"  状态: {why or '可发送'}")
        for c in camps:
            mx = "∞" if not c.get("max_iterations") else c.get("max_iterations")
            print(f"\n#{c['id']} [{c.get('status')}] {c.get('current_iteration')}/{mx} 轮 "
                  f"策略={c.get('strategy')} 模型={c.get('model') or '自动'}")
            print(f"   目标: {c.get('goal', '')[:80]}")
            if c.get("pause_reason"):
                print(f"   ⏸ 暂停原因: {c['pause_reason']}（{c.get('paused_at', '')}）")
            if c.get("strategy") == "autopilot" and c.get("next_step"):
                print(f"   🧭 模型自定下一步: {_truncate(c['next_step'], 100)}")
            if c.get("last_result"):
                print(f"   最近产出: {_truncate(c['last_result'], 120)}")
            h = c.get("history", [])
            if h:
                print(f"   最近 3 条历史:")
                for it in h[-3:]:
                    print(f"     [{it.get('time', '')}] {_truncate(it.get('result', ''), 90)}")
        print(f"\n(日志: {LOG_FILE})")
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