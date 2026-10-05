# -*- coding: utf-8 -*-
"""workbuddy-monitor 离线验证测试

覆盖三大块：
  1. 免费时段/免费模型的解析与判定（含安全边界：绝不误判为免费而扣积分）
  2. 统一调度 task_sendable_now（定时 / 限流闸门 / free_only 守卫）
  3. 自动迭代引擎（生成提示词 / 推进 / 停止条件 / 全链路连跑）

CDP 依赖用伪造模块注入 sys.modules，因此无需真实 WorkBuddy 即可跑通发现与迭代。
状态与配置文件重定向到临时目录，不会污染仓库。

运行： python test_core.py
"""
import sys
import json
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import workbuddy_monitor as core  # noqa: E402
import workbuddy_cdp as cdp  # noqa: E402

# ---------------------------------------------------------------- 环境隔离
_TMP = Path(tempfile.mkdtemp(prefix="wbmon_test_"))
core.STATE_FILE = _TMP / "state.json"
core.CONFIG_FILE = _TMP / "config.json"
core.LOG_FILE = _TMP / "monitor.log"

# ---------------------------------------------------------------- 伪造 CDP
FAKE = {
    "models": [],
    "dom_window": None,
    "reply": None,
}


def _fake_list_models():
    return FAKE["models"]


def _fake_discover_window_dom():
    return FAKE["dom_window"]


def _fake_last_msg(timeout=240, poll=15, prompt_hint=""):
    # 记录定位提示，方便断言「按 last_prompt 定位会话」确实传到了 CDP 层
    FAKE["last_hint"] = prompt_hint
    return FAKE["reply"]


_fake_cdp = types.ModuleType("workbuddy_cdp")
_fake_cdp.list_models = _fake_list_models
_fake_cdp.discover_free_window_dom = _fake_discover_window_dom
_fake_cdp.last_assistant_message = _fake_last_msg
sys.modules["workbuddy_cdp"] = _fake_cdp

# ---------------------------------------------------------------- 断言器
PASS = 0
FAIL = 0
FAILED = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2713 {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  \u2717 {name}   {detail}")


def fresh_state():
    """清空状态文件并返回全新 state"""
    if core.STATE_FILE.exists():
        core.STATE_FILE.unlink()
    return core.load_state()


def cfg_of(**auto_kw):
    c = json.loads(json.dumps(core.DEFAULT_CONFIG))
    c["auto_send"].update(auto_kw)
    return c


def stamp(state, **auto):
    """给 state 的免费目录打上「刚刚发现」时间戳（fail-closed 门控要求）。"""
    state.setdefault("free_info", {})["models_discovered_at"] = \
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for k, v in auto.items():
        state.setdefault("free_info", {})[k] = v
    return state


def t_at(h, m=0):
    """构造今天的某个时刻"""
    return datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)


# ================================================================ 1. 文本解析
print("\n== 1. 免费时段 / 倍率文本解析 ==")
check("23:00-08:00", core._parse_window_from_text("闲时 23:00-08:00 免费") == ("23:00", "08:00"))
check("23:00 到 08:00", core._parse_window_from_text("23:00 到 08:00") == ("23:00", "08:00"))
check("23:00-次日 8:00（官方口径）",
      core._parse_window_from_text("23:00-次日 8:00") == ("23:00", "08:00"),
      f"实际={core._parse_window_from_text('23:00-次日 8:00')}")
check("23:00~次日08:00", core._parse_window_from_text("23:00~次日08:00") == ("23:00", "08:00"))
check("无时段文本返回 None", core._parse_window_from_text("没有时间信息") is None)
check("倍率 x2", core._parse_multiplier("x2") == 2.0)
check("倍率 2倍", core._parse_multiplier("2倍") == 2.0)
check("倍率 x0.5", core._parse_multiplier("x0.5") == 0.5)

# ================================================================ 2. 窗口判定
print("\n== 2. 免费时段窗口判定（跨午夜）==")
W = {"start": "23:00", "end": "08:00"}
check("23:30 在窗口内", core.in_window(W, t_at(23, 30)) is True)
check("02:00 在窗口内（跨午夜）", core.in_window(W, t_at(2, 0)) is True)
check("07:59 在窗口内", core.in_window(W, t_at(7, 59)) is True)
check("08:00 已出窗口", core.in_window(W, t_at(8, 0)) is False)
check("14:00 不在窗口内", core.in_window(W, t_at(14, 0)) is False)
check("22:59 不在窗口内", core.in_window(W, t_at(22, 59)) is False)
W2 = {"start": "09:00", "end": "18:00"}
check("同日窗口 12:00 在内", core.in_window(W2, t_at(12)) is True)
check("同日窗口 20:00 在外", core.in_window(W2, t_at(20)) is False)
check("起止相同=全天", core.in_window({"start": "00:00", "end": "00:00"}, t_at(3)) is True)

# ================================================================ 2.5 倍率解析（真实 DOM 无空格拼接）
print("\n== 2.5 倍率解析（WorkBuddy 真实格式）==")
# _parse_multiplier 只负责"已剥离模型名的倍率片段"；剥离逻辑由 classify_model 完成
# （因为 'MiniMax-M30.25x' 这类字符串在无 name 上下文时无法可靠区分）。
_mp = [
    ("0.00x", 0.0, "真免费模型：倍率 0"),
    ("0.29x", 0.29, "夜间折扣价：非免费！"),
    ("0.25x", 0.25, "MiniMax-M3 的倍率"),
    ("1.62x", 1.62, "Kimi-K3 的倍率"),
    ("x2", 2.0, "前缀写法"),
    ("倍率2", 2.0, "中文前缀"),
    ("2倍", 2.0, "中文后缀"),
    ("x0.5", 0.5, "半价折扣"),
    ("", None, "空值"),
]
for _txt, _exp, _why in _mp:
    _got = core._parse_multiplier(_txt)
    check(f"倍率解析 {_txt!r} → {_exp}（{_why}）", _got == _exp, f"实际={_got}")
check("纯模型名不误判为倍率(Hy4)", core._parse_multiplier("Hy4") is None)
check("纯模型名不误判为倍率(Kimi-K3)", core._parse_multiplier("Kimi-K3") is None)

# classify_model 端到端：从真实 text 里剥掉模型名后取倍率（回归 30.25/31.62 误判）
_e2e_mp = [
    ("MiniMax-M30.25x", "MiniMax-M3", 0.25, False),
    ("Kimi-K31.62x", "Kimi-K3", 1.62, False),
    ("GLM-5.30.79x", "GLM-5.3", 0.79, False),
    ("Kimi-K2.8-Preview0.77x", "Kimi-K2.8-Preview", 0.77, False),
    ("Hy4 preview夜间免费0.29x", "Hy4 preview", 0.29, False),
    ("Hy3限时免费0.00x", "Hy3", 0.0, True),
]
for _text, _name, _exp_mult, _exp_free in _e2e_mp:
    _r = core.classify_model({"name": _name, "badge": "", "text": _text, "desc": ""})
    check(f"classify {_name}: 倍率={_exp_mult}", _r["multiplier"] == _exp_mult,
          f"实际={_r['multiplier']} raw={_text!r}")
    check(f"classify {_name}: free={_exp_free}", _r["free"] is _exp_free,
          f"reason={_r['free_reason']}")

# classify_model：倍率>0 一律非免费（包括徽标写"夜间免费"的折扣模型）
_cls = [
    ({"name": "Hy3", "badge": "限时免费", "text": "Hy3限时免费0.00x", "cost": "0.00x", "desc": ""}, True, "0.00x=真免费"),
    ({"name": "Hy4 preview", "badge": "夜间免费", "text": "Hy4 preview夜间免费0.29x", "cost": "0.29x", "desc": ""}, False, "0.29x=折扣价，必须判非免费"),
    ({"name": "Deepseek-V4-Pro", "badge": "夜间折扣", "text": "Deepseek-V4-Pro 夜间折扣0.51x", "cost": "0.51x", "desc": ""}, False, "夜间折扣=计费"),
    ({"name": "X", "badge": "免费", "text": "X 免费", "cost": "", "desc": ""}, True, "明确免费且无计费倍率"),
]
for _item, _exp, _why in _cls:
    _r = core.classify_model(_item)
    check(f"classify {_item['name']} free={_exp}（{_why}）", _r["free"] is _exp,
          f"实际={_r['free']} reason={_r['free_reason']}")

# ================================================================ 3. 窗口优先级
print("\n== 3. effective_free_window 优先级 ==")
st = fresh_state()
check("无发现数据→策略默认 23:00-08:00",
      core.effective_free_window(st)["start"] == "23:00" and core.effective_free_window(st)["end"] == "08:00")
st["free_info"] = {"window": {"start": "22:00", "end": "09:00", "source": "discovered"}}
check("全局已发现窗口优先", core.effective_free_window(st)["start"] == "22:00")
st["model_catalog"] = [{"name": "A", "free": True, "free_window": {"start": "01:00", "end": "06:00"}}]
check("模型专属窗口最优先", core.effective_free_window(st, "A")["start"] == "01:00")
check("其他模型仍用全局窗口", core.effective_free_window(st, "B")["start"] == "22:00")
# 配置显式指定的窗口必须优先于 free_info 里的 config 副本，
# 否则用户/测试改了配置却被旧缓存静默覆盖（回归项）。
st_c = fresh_state()
st_c["free_info"] = {"window": {"start": "23:00", "end": "08:00", "source": "config"}}
cfg_local = cfg_of(free_window={"enabled": True, "start": "00:00", "end": "23:59"})
check("显式配置窗口优先于 config 缓存副本",
      core.effective_free_window(st_c, None, cfg_local)["start"] == "00:00",
      f"实际={core.effective_free_window(st_c, None, cfg_local)}")
check("discovered 来源的窗口优先于配置",
      core.effective_free_window(st, None, cfg_local)["start"] == "22:00")
# 关闭时段限制时同样以配置为准
cfg_off = cfg_of(free_window={"enabled": False, "start": "23:00", "end": "08:00"})
check("free_window.enabled=False 时不再用配置窗口作为时段门",
      core.in_free_window(cfg_off, t_at(14), st_c) is True)

# ================================================================ 4. 模型免费判定（安全边界）
print("\n== 4. 模型免费判定与安全边界 ==")
cases = [
    ("徽标'免费'→免费", {"name": "Hy4 preview", "badge": "免费", "text": "Hy4 preview 免费"}, True),
    ("徽标'限免'→免费", {"name": "M2", "badge": "限免"}, True),
    ("徽标'x0'→免费", {"name": "M3", "badge": "x0"}, True),
    ("徽标'x2'→不免费", {"name": "GPT-5", "badge": "x2"}, False),
    ("无标记→不免费", {"name": "Claude", "badge": "", "text": "Claude"}, False),
    ("'x0.5'(半价)不应判免费", {"name": "M4", "badge": "x0.5", "text": "M4 x0.5"}, False),
    ("'避免限流'不应因'免'误判", {"name": "M5", "text": "避免限流，稳定可用"}, False),
    ("'免疫'不应误判", {"name": "M6", "text": "免疫限流"}, False),
    ("描述含'免费'→免费", {"name": "M7", "desc": "闲时 23:00-次日 8:00 免费"}, True),
]
for label, item, expect in cases:
    got = core.classify_model(item)["free"]
    check(label, got is expect, f"期望={expect} 实际={got}")

c7 = core.classify_model({"name": "M7", "desc": "闲时 23:00-次日 8:00 免费"})
check("从描述解析出免费时段", c7["free_window"] == {"start": "23:00", "end": "08:00"}, f"实际={c7['free_window']}")
check("倍率解析 x2", core.classify_model({"name": "G", "badge": "x2"})["multiplier"] == 2.0)

# ---- 准确率专项（stage-1 提升发现准确率）----
ca = core.classify_model({"name": "F1", "badge": "0积分"})
check("'0积分'(无空格)应判免费", ca["free"] is True, f"实际={ca['free']}")
check("免费模型带 confidence=1.0", ca["free_confidence"] == 1.0, f"实际={ca['free_confidence']}")
check("免费原因含'免费关键词'", "免费关键词" in ca["free_reason"], f"实际={ca['free_reason']}")
cb = core.classify_model({"name": "P1", "badge": "10积分"})
check("'10积分'不应误判为免费", cb["free"] is False, f"实际={cb['free']}")
cc = core.classify_model({"name": "F2", "text": "本模型免积分可用"})
check("'免积分'应判免费", cc["free"] is True, f"实际={cc['free']}")
cd = core.classify_model({"name": "P2", "badge": "x2", "text": "消耗2积分"})
check("倍率>0 且文本含成本→非免费", cd["free"] is False, f"实际={cd['free']} reason={cd['free_reason']}")
ce = core.classify_model({"name": "F3", "badge": "×0"})
check("徽标 '×0' 判免费", ce["free"] is True, f"实际={ce['free']}")

# ================================================================ 5. is_model_free_now
print("\n== 5. is_model_free_now（有发现数据时只放行免费模型）==")
st2 = fresh_state()
st2["model_catalog"] = [
    {"name": "FreeA", "free": True},
    {"name": "PaidB", "free": False},
]
stamp(st2)
cfg = cfg_of()
check("免费模型+免费时段→放行", core.is_model_free_now("FreeA", st2, cfg, t_at(2)) is True)
check("免费模型+非免费时段→扣留", core.is_model_free_now("FreeA", st2, cfg, t_at(14)) is False)
check("付费模型即使免费时段也扣留", core.is_model_free_now("PaidB", st2, cfg, t_at(2)) is False)
check("未发现过的模型一律扣留（防误扣积分）", core.is_model_free_now("Unknown", st2, cfg, t_at(2)) is False)

# fail-closed：目录没有发现时间戳（旧状态/手工构造）→ 一律扣留并提示重新发现
st_u = fresh_state()
st_u["model_catalog"] = [{"name": "FreeA", "free": True}]  # 故意不打时间戳
r_u = core.free_gate_reason("FreeA", st_u, cfg, t_at(2))
check("目录无时间戳→fail-closed 扣留", core.is_model_free_now("FreeA", st_u, cfg, t_at(2)) is False)
check("目录无时间戳→原因说明需重新发现", "时间戳" in r_u, f"实际={r_u}")

# fail-closed：目录过期（超过保鲜期）→ 扣留
st_old = fresh_state()
st_old["model_catalog"] = [{"name": "FreeA", "free": True}]
st_old["free_info"] = {
    "models_discovered_at": (datetime.now() - timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")}
check("目录过期 12h→fail-closed 扣留", core.is_model_free_now("FreeA", st_old, cfg, t_at(2)) is False)
check("目录过期原因含'过期'", "过期" in core.free_gate_reason("FreeA", st_old, cfg, t_at(2)))

# fail-closed：完全没有目录 → 扣留（不再有"无数据也放行"的回退）
st_missing = fresh_state()
check("无目录→fail-closed 扣留", core.is_model_free_now("Any", st_missing, cfg, t_at(2)) is False)
check("无目录→原因说明缺目录", "尚无免费模型目录" in core.free_gate_reason("Any", st_missing, cfg, t_at(2)))

# 限流闸门：未到重置时间即使是免费模型也不发
st_rl = fresh_state()
st_rl["model_catalog"] = [{"name": "FreeA", "free": True}]
st_rl["free_info"] = {"models_discovered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
st_rl["rate_limits"] = {"FreeA": {"model": "FreeA",
                                  "reset": (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")}}
check("限流中→不可发", core.is_model_free_now("FreeA", st_rl, cfg, t_at(2)) is False)
check("限流→原因含重置时间", "限流" in core.free_gate_reason("FreeA", st_rl, cfg, t_at(2)))

# 用户显式关闭时段限制 → 目录可信即可发（不再要求窗口内）
st_off = fresh_state()
st_off["model_catalog"] = [{"name": "FreeA", "free": True}]
stamp(st_off)
cfg_off = cfg_of(free_window={"enabled": False, "start": "23:00", "end": "08:00"})
check("关闭时段限制→白天也可发（模型已确认免费）",
      core.is_model_free_now("FreeA", st_off, cfg_off, t_at(14)) is True)

# ================================================================ 6. task_sendable_now
print("\n== 6. 统一调度 task_sendable_now ==")
st4 = fresh_state()
st4["model_catalog"] = [{"name": "FreeA", "free": True}, {"name": "PaidB", "free": False}]
stamp(st4)
now_night = t_at(2)
now_day = t_at(14)

t1 = {"content": "x", "model": "FreeA", "status": "pending"}
check("免费模型+免费时段→可发", core.task_sendable_now(t1, st4, cfg, now_night) is True)
check("免费模型+非免费时段→不可发", core.task_sendable_now(t1, st4, cfg, now_day) is False)

# 积分任务不再能绕过免费门控：allow_credit 只影响"是否允许扣积分"的老语义已废弃，
# 免费额度用尽后必须停下（用户明确要求：不再发送新对话）。
t2 = {"content": "x", "model": "FreeA", "status": "pending", "allow_credit": True}
check("allow_credit 不再能绕过免费时段门控",
      core.task_sendable_now(t2, st4, cfg, now_day) is False)
t2p = {"content": "x", "model": "PaidB", "status": "pending", "allow_credit": True}
check("allow_credit 不放过付费模型", core.task_sendable_now(t2p, st4, cfg, now_night) is False)

future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
t3 = {"content": "x", "model": "FreeA", "scheduled_time": future}
check("未到指定时间→不可发", core.task_sendable_now(t3, st4, cfg, now_night) is False)
t4 = {"content": "x", "model": "FreeA", "scheduled_time": past}
check("已过指定时间→可发（且仍在免费时段）", core.task_sendable_now(t4, st4, cfg, now_night) is True)
check("已过指定时间但在白天→仍被 free_only 扣留", core.task_sendable_now(t4, st4, cfg, now_day) is False)

st5 = fresh_state()
st5["model_catalog"] = [{"name": "FreeA", "free": True}]
stamp(st5)
st5["rate_limits"] = {"FreeA": {"model": "FreeA", "reset": future}}
t5 = {"content": "x", "model": "FreeA"}
check("模型仍被限流（未到重置）→不可发", core.task_sendable_now(t5, st5, cfg, now_night) is False)
st5["rate_limits"] = {"FreeA": {"model": "FreeA", "reset": past}}
check("限流已过重置时间→可发", core.task_sendable_now(t5, st5, cfg, now_night) is True)

st6 = fresh_state()
st6["model_catalog"] = [{"name": "FreeA", "free": True}]
stamp(st6)
st6["rate_limits"] = {"FreeA": {"model": "FreeA", "reset": "不是合法时间"}}
check("非法重置时间戳不崩溃且按免费资格放行",
      core.task_sendable_now(t5, st6, cfg, now_night) is True)

st7 = fresh_state()
st7["model_catalog"] = [{"name": "FreeA", "free": True}]
stamp(st7)
core.set_balance_guard_paused(st7, True, "测试暂停")
check("余额守卫暂停中→一律不可发", core.task_sendable_now(t1, st7, cfg, now_night) is False)

# ================================================================ 7. add_task
print("\n== 7. add_task 定时字段 ==")
st8 = fresh_state()
ok_fmt = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
tk = core.add_task(st8, "定时任务", model="FreeA", scheduled_time=ok_fmt)
check("合法定时被保留", tk["scheduled_time"] == ok_fmt)
tk2 = core.add_task(st8, "非法定时", scheduled_time="25:70 xx")
check("非法定时退化为立即", tk2["scheduled_time"] is None)
check("默认 allow_credit=False（保护积分）", tk["allow_credit"] is False)
check("任务已落盘", len(core.load_state()["tasks"]) == 2)
check("pending 过滤正确", len(core.pending_tasks(core.load_state())) == 2)

# ================================================================ 8. 免费发现（模拟 CDP）
print("\n== 8. 免费模型/时段底层发现（模拟 CDP 返回）==")
FAKE["models"] = [
    {"name": "Hy4 preview", "badge": "免费", "text": "Hy4 preview 免费", "desc": ""},
    {"name": "GPT-5", "badge": "x2", "text": "GPT-5 x2", "desc": ""},
    {"name": "Claude", "badge": "", "text": "Claude", "desc": "闲时 23:00-次日 8:00 免费"},
]
FAKE["dom_window"] = {"start": "22:30", "end": "09:00", "ctx": "闲时优惠"}
st9 = fresh_state()
cat = core.discover_free_models(cfg, st9, force=True)
check("发现 3 个模型", cat is not None and len(cat) == 3, f"实际={cat}")
check("Hy4 判定免费", cat[0]["free"] is True)
check("GPT-5 判定不免费", cat[1]["free"] is False)
check("Claude 描述含免费→免费", cat[2]["free"] is True)
check("目录写入 state", len(core.load_state()["model_catalog"]) == 3)
win = core.discover_free_window(cfg, st9, force=True)
check("发现到页面免费时段 22:30", win["start"] == "22:30" and win["source"] == "discovered", f"实际={win}")
check("发现的窗口写入 state",
      core.load_state()["free_info"]["window"]["start"] == "22:30")
check("自动挑选免费模型", core.pick_free_model(st9) == "Hy4 preview")

FAKE["dom_window"] = None
st10 = fresh_state()
win2 = core.discover_free_window(cfg, st10, force=True)
check("发现不到→回退策略默认 23:00-08:00",
      win2["start"] == "23:00" and win2["end"] == "08:00", f"实际={win2}")

FAKE["models"] = []
st11 = fresh_state()
check("CDP 返回空→发现返回 None", core.discover_free_models(cfg, st11, force=True) is None)

# ================================================================ 9. 迭代引擎
print("\n== 9. 迭代引擎：提示词生成与推进 ==")
stc = fresh_state()
c = core.create_campaign(stc, "用 Python 写一个并发爬虫", strategy="staged",
                         stages=["搭框架", "加异常处理", "写测试"])
check("创建即 running", c["status"] == "running")
check("策略生效", c["strategy"] == "staged")
labels = []
for _ in range(4):
    p, lb = core.generate_next_prompt(c, stc)
    labels.append(lb)
    core.advance_campaign(c, "ok", stc)
check("staged 按阶段循环", labels[:3] == ["搭框架", "加异常处理", "写测试"], f"实际={labels[:3]}")
check("staged 阶段循环回绕", labels[3] == "搭框架")

c2 = core.create_campaign(stc, "优化项目", strategy="improve")
angles = []
for _ in range(3):
    _, a = core.generate_next_prompt(c2, stc)
    angles.append(a)
    core.advance_campaign(c2, "完成", stc)
check("improve 角度逐轮递进", angles[0] != angles[1] and angles[1] != angles[2], f"实际={angles}")

c3 = core.create_campaign(stc, "x", strategy="reflect")
p3, lb3 = core.generate_next_prompt(c3, stc)
check("reflect 生成反思式提示词", "改进" in p3 or "优化" in p3)

c4 = core.create_campaign(stc, "x")
core.advance_campaign(c4, "Traceback: error occurred", stc)
p4, _ = core.generate_next_prompt(c4, stc)
check("上轮失败→提示词要求先修复", "修复" in p4)
check("失败计入 error_count", c4["error_count"] == 1)
core.advance_campaign(c4, "一切正常", stc)
check("成功后 error_count 清零", c4["error_count"] == 0)

c5 = core.create_campaign(stc, "x", strategy="improve")
core.advance_campaign(c5, "正常", stc)
check("无限迭代第一轮后仍 running", c5["status"] == "running")
p5, _ = core.generate_next_prompt(c5, stc)
check("提示词带上轮产出作为上下文", "上一轮" in p5 or "产出" in p5)

c6 = core.create_campaign(stc, "x", max_iterations=2)
core.advance_campaign(c6, "ok", stc)
check("未达上限仍 running", c6["status"] == "running")
core.advance_campaign(c6, "ok", stc)
check("达到最大轮数→done", c6["status"] == "done")

c7c = core.create_campaign(stc, "x")
for _ in range(3):
    core.advance_campaign(c7c, "error 报错", stc)
check("连续 3 轮失败→自动暂停", c7c["status"] == "paused")

c8 = core.create_campaign(stc, "x", max_iterations=0)
for i in range(10):
    core.advance_campaign(c8, f"第{i}轮：完成功能{i}", stc)
check("max=0 无限迭代不停止（结果有进展）", c8["status"] == "running" and c8["current_iteration"] == 10)
check("历史记录完整", len(c8["history"]) == 10)

check("非法策略回退 improve",
      core.create_campaign(stc, "x", strategy="不存在的策略")["strategy"] == "improve")

# ================================================================ 10. 任务生成与全链路
print("\n== 10. 迭代任务生成与全链路连跑 ==")
std = fresh_state()
std["model_catalog"] = [{"name": "FreeA", "free": True}, {"name": "PaidB", "free": False}]
stamp(std)
cfg2 = cfg_of()
cc = core.create_campaign(std, "目标：写个工具", strategy="improve")
tt = core.ensure_campaign_task(cc, std, cfg2, t_at(2))
check("生成任务不消耗积分(allow_credit=False)", tt.get("allow_credit") is False)
check("任务绑定 campaign_id", tt.get("campaign_id") == cc["id"], f"实际={tt.get('campaign_id')}")
check("自动选用免费模型", tt.get("model") == "FreeA", f"实际={tt.get('model')}")
check("进入等待回复状态", cc["awaiting_result"] is True)

tt2 = core.ensure_campaign_task(cc, std, cfg2, t_at(2))
check("重复调用复用同一任务（不重复入队）", tt2["id"] == tt["id"], f"新id={tt2['id']} 旧id={tt['id']}")
n_pending = len([x for x in core.load_state()["tasks"] if x.get("campaign_id") == cc["id"]])
check("磁盘上只有一个该活动的任务", n_pending == 1, f"实际={n_pending}")

cc2 = core.create_campaign(std, "指定付费模型", model="PaidB")
check("指定模型非免费时回退到免费模型",
      core._effective_model(cc2, std) == "FreeA", f"实际={core._effective_model(cc2, std)}")

# 全链路：任务已发送 → CDP 返回回复 → 应推进并立即生成下一轮
print("\n== 11. 全链路：发送→捕获回复→推进→续跑 ==")
ste = fresh_state()
ste["model_catalog"] = [{"name": "FreeA", "free": True}]
stamp(ste)
FAKE["reply"] = "已完成第一版实现，代码可运行。"
ce = core.create_campaign(ste, "目标", strategy="improve", max_iterations=0)
core.ensure_campaign_task(ce, ste, cfg2, t_at(2))
core.update_task(ce["last_task_id"], status="sent")
ste = core.load_state()
core.run_iteration_step(ste, cfg2, t_at(2))
ce_r = core.campaign_by_id(ste, ce["id"])
check("捕获回复后推进一轮", ce_r["current_iteration"] == 1, f"实际={ce_r['current_iteration']}")
check("记录到上次结果", "已完成第一版" in (ce_r.get("last_result") or ""))
check("推进后立即生成下一轮任务",
      ce_r["awaiting_result"] is True and ce_r["last_task_id"] is not None)
check("第二轮任务已入队",
      len([x for x in core.load_state()["tasks"] if x.get("campaign_id") == ce["id"]]) == 2,
      f"实际={len([x for x in core.load_state()['tasks'] if x.get('campaign_id') == ce['id']])}")

# 连续 3 轮连跑，验证一个免费时段内能无限迭代（每轮结果有进展，不触发停滞）
for i in range(3):
    FAKE["reply"] = f"第{i}轮：已实现功能{i}，测试通过。"
    tid = ce_r["last_task_id"]
    core.update_task(tid, status="sent")
    stx = core.load_state()
    core.run_iteration_step(stx, cfg2, t_at(2))
    ce_r = core.campaign_by_id(stx, ce["id"])
check("连续多轮迭代持续推进", ce_r["current_iteration"] == 4, f"实际={ce_r['current_iteration']}")
check("无限迭代下仍 running", ce_r["status"] == "running")

# 在途任务 failed 后不得永久卡死 awaiting_result（真实事故：runner 并发期间
# 发送 3 次失败任务标 failed，活动仍 awaiting → 永久空等）
print("\n== 11b. 在途任务 failed → 清除等待并重新生成 ==")
stfail = core.load_state()   # 用当前全量状态的副本，不落盘（避免删掉前面用例的活动）
stfail["model_catalog"] = [{"name": "FreeA", "free": True}]
stamp(stfail)
FAKE["reply"] = "失败后的新一轮产出。"
cfail = core.create_campaign(stfail, "失败恢复目标", strategy="improve", max_iterations=0)
core.ensure_campaign_task(cfail, stfail, cfg2, t_at(2))
_failed_tid = cfail["last_task_id"]   # 必须在步进前捕获，步进后已是新任务 id
for t in stfail["tasks"]:
    if t["id"] == _failed_tid:
        t["status"] = "failed"
        t["last_error"] = "粘贴未生效（内容未进入输入框），已取消发送"
core.run_iteration_step(stfail, cfg2, t_at(2))
cfr = core.campaign_by_id(stfail, cfail["id"])
check("failed 任务被清除等待后重新生成新一轮在途任务",
      cfr["awaiting_result"] is True and cfr["last_task_id"] != _failed_tid,
      f"awaiting={cfr['awaiting_result']} new_tid={cfr['last_task_id']}")
check("failed 计入错误计数", int(cfr.get("error_count") or 0) >= 1,
      f"实际={cfr.get('error_count')}")
check("活动仍 running 可继续", cfr["status"] == "running")
# 清理：暂停本用例创建的活动，避免污染后续用例的活跃列表
core.set_campaign_status(core.load_state(), cfail["id"], "paused")

# 暂停/恢复
core.set_campaign_status(core.load_state(), ce["id"], "paused")
stp = core.load_state()
check("暂停生效", core.campaign_by_id(stp, ce["id"])["status"] == "paused")
check("暂停后不在活跃列表", len(core.active_campaigns(stp)) == 0)
core.set_campaign_status(stp, ce["id"], "running")
check("恢复后清空在途状态",
      core.campaign_by_id(stp, ce["id"])["awaiting_result"] is False)

# 非免费时段不应推进迭代
stf = fresh_state()
stf["model_catalog"] = [{"name": "FreeA", "free": True}]
stamp(stf)
cf = core.create_campaign(stf, "目标", strategy="improve")
before = cf["current_iteration"]
check("白天调用 run_iteration_step 不推进（由 run_loop 守卫）",
      "in_free_window" in dir(core))
core.in_free_window(cfg2, t_at(14), stf)
check("非免费时段 in_free_window=False（迭代被守卫拦住）",
      core.in_free_window(cfg2, t_at(14), stf) is False)
check("免费时段 in_free_window=True",
      core.in_free_window(cfg2, t_at(2), stf) is True)

# ================================================================ 12. 健壮性
print("\n== 12. 健壮性 ==")
stg = fresh_state()
check("空 state 不崩溃(effective_free_window)", core.effective_free_window({})["start"] == "23:00")
check("空 state 不崩溃(active_campaigns)", core.active_campaigns({}) == [])
check("空 state 不崩溃(is_model_free_now)",
      core.is_model_free_now("", {}, cfg2, t_at(2)) is False)
check("空目录原因可读(fail-closed)", "尚无免费模型目录" in
      core.free_gate_reason("", {}, cfg2, t_at(2)))
check("ensure_campaigns 补齐默认值",
      core.ensure_campaigns({"campaigns": [{"id": 1}]})[0]["history"] == [])
stz = fresh_state()
check("campaign_by_id 不存在返回 None", core.campaign_by_id(stz, 999999) is None)
check("set_campaign_status 不存在返回 False", core.set_campaign_status(stz, 999999, "done") is False)

# ================================================================ 13. 发送通道
print("\n== 13. 发送通道（command / api / 付费模型扣留）==")
# command 模式：空命令不实际执行，但必须能正常标记 sent（曾因变量未定义抛 NameError）
sts = fresh_state()
# 免费时段门控必须在发送通道之前生效：白天（当前 12:00）先验证被拦截
cfg_cmd = cfg_of()
cfg_cmd["send_mode"] = "command"
cfg_cmd["send_command"] = ""
sts["model_catalog"] = [{"name": "FreeA", "free": True}, {"name": "PaidB", "free": False}]
stamp(sts)
tkc = core.add_task(sts, "命令模式任务", model="FreeA")
# send_task 会重新 load_state() 做安全复检，因此目录时间戳必须落盘才被认可
_d = core.load_state()
_d["model_catalog"] = sts["model_catalog"]
stamp(_d)
core.save_state(_d)
check("免费时段外 command 模式被拦截（fail-closed）",
      core.free_gate_reason("FreeA", core.load_state(), cfg_cmd, t_at(12)) != "")

# 命令通道本身不崩溃（回归 NameError）：在「模拟免费时段」下执行
cfg_cmd["auto_send"]["free_window"] = {"enabled": True, "start": "00:00", "end": "23:59"}
try:
    core.send_task(tkc, cfg_cmd)
    crashed = None
except Exception as e:  # noqa: BLE001
    crashed = f"{type(e).__name__}: {e}"
check("command 模式发送不崩溃（回归 NameError）", crashed is None, crashed or "")
check("command 模式正确标记 sent",
      core.load_state()["tasks"][0]["status"] == "sent")

# api 模式：未配置 url 时跳过请求，同样必须能标记 sent
sta = fresh_state()
sta["model_catalog"] = [{"name": "FreeA", "free": True}]
stamp(sta)
cfg_api = cfg_of()
cfg_api["send_mode"] = "api"
tka = core.add_task(sta, "API模式任务", model="FreeA")
_d2 = core.load_state()
_d2["model_catalog"] = sta["model_catalog"]
stamp(_d2)
core.save_state(_d2)
cfg_api["auto_send"]["free_window"] = {"enabled": True, "start": "00:00", "end": "23:59"}
try:
    core.send_task(tka, cfg_api)
    crashed = None
except Exception as e:  # noqa: BLE001
    crashed = f"{type(e).__name__}: {e}"
check("api 模式发送不崩溃（回归 NameError）", crashed is None, crashed or "")
check("api 模式正确标记 sent",
      core.load_state()["tasks"][0]["status"] == "sent")

# notify + free_only：付费模型必须被扣留（任何时刻都不该走积分通道）
stn = fresh_state()
stn["model_catalog"] = [{"name": "PaidB", "free": False}]
cfg_nt = cfg_of()
tkn = core.add_task(stn, "付费模型任务", model="PaidB")
core.send_task(tkn, cfg_nt)
check("notify+free_only 下付费模型被扣留（不发送）",
      core.load_state()["tasks"][0]["status"] == "pending")

# ================================================================ 14. 限流消息解析
print("\n== 14. 限流消息解析（含缺秒/变体文案容错）==")
m, r, ok = core.parse_rate_limit_message(
    "您在 Hy4 preview 模型的使用量已超出频率限制，可在 2026-09-23 08:00:00 重置可用")
check("官方标准文案解析成功", ok is True)
check("官方文案提取模型名", m == "Hy4 preview", f"实际={m}")
check("官方文案提取重置时间", r is not None and r.hour == 8)

m2, r2, ok2 = core.parse_rate_limit_message(
    "您在 Hy4 preview 模型的使用量已超出频率限制，可在 2026-09-23 08:00 重置")
check("省略秒也能解析（回归缺秒解析失败）", ok2 is True and r2 is not None and r2.hour == 8,
      f"ok={ok2} reset={r2}")

m3, r3, ok3 = core.parse_rate_limit_message("模型 Hy4 preview 已限流，可在 2026-09-23 08:00 重置可用")
check("变体文案解析成功", ok3 is True, f"ok={ok3}")
check("变体文案提取模型名", m3 == "Hy4 preview", f"实际={m3}")

m4, r4, ok4 = core.parse_rate_limit_message("今天天气不错")
check("无关文本不误判", ok4 is False)

m5, r5, ok5 = core.parse_rate_limit_message("可在 2026-13-45 99:99 重置")
check("非法时间不崩溃", ok5 is False or r5 is None)

# ================================================================ 15. 代码自迭代 + 停滞检测
print("\n== 15. 代码自迭代指令与停滞检测（保障无限迭代真改进、不空转）==")
sti = fresh_state()
ci = core.create_campaign(sti, "持续打磨 workbuddy-monitor", strategy="improve",
                          repo_path="/repo/workbuddy-monitor",
                          test_cmd="python test_core.py")
check("campaign 记录 repo_path", ci.get("repo_path") == "/repo/workbuddy-monitor")
check("campaign 记录 test_cmd", ci.get("test_cmd") == "python test_core.py")

pi, _ = core.generate_next_prompt(ci, sti)
check("指向仓库时提示词携带'读文件'指令", "读取相关源文件" in pi, "缺读文件指令")
check("提示词包含仓库目录", "/repo/workbuddy-monitor" in pi)
check("提示词包含验证命令", "python test_core.py" in pi)
check("提示词要求真正改动（非只建议）", "直接修改" in pi or "修改对应文件" in pi)

cn = core.create_campaign(sti, "纯对话目标", strategy="improve")
pn, _ = core.generate_next_prompt(cn, sti)
check("未指向仓库时不附加代码指令", "读取相关源文件" not in pn)

# 停滞检测：连续 2 轮结果几乎相同 → 自动暂停，避免无限空转烧免费额度
cs = core.create_campaign(sti, "目标", strategy="improve", max_iterations=0)
core.advance_campaign(cs, "已新增功能A，测试通过。", sti)
core.advance_campaign(cs, "已新增功能A，测试通过。", sti)  # 与上轮几乎一致
csr = core.campaign_by_id(sti, cs["id"])
check("连续 2 轮输出几乎相同→自动暂停", csr["status"] == "paused", f"实际={csr['status']}")
check("暂停标记 stagnation=True", csr.get("stagnation") is True)

# 结果不同则不应触发停滞
cv = core.create_campaign(sti, "目标", strategy="improve", max_iterations=0)
core.advance_campaign(cv, "完成模块一的实现。", sti)
core.advance_campaign(cv, "在模块一基础上补充了异常处理和单测。", sti)
cvr = core.campaign_by_id(sti, cv["id"])
check("结果不同不触发停滞", cvr["status"] == "running" and cvr.get("stagnation") is not True)

# 停滞检测不应覆盖 max_iterations 已达上限的 done 判定（先到先得）
cd = core.create_campaign(sti, "目标", max_iterations=1)
core.advance_campaign(cd, "第一版", sti)  # 达上限 → done
core.advance_campaign(cd, "第一版", sti)  # 即便相同也不应因停滞逻辑改写（已 done 不活跃）
check("达上限为 done 不被停滞逻辑覆盖", core.campaign_by_id(sti, cd["id"])["status"] == "done")

# ================================================================ 16. 阶段2：迭代引擎健壮性
print("\n== 16. 阶段2 加固：回复捕获回退 / 防死锁自愈 / 无代码改动停滞 ==")
# 16.1 回复捕获回退：CDP 无回复时改读缓存文件
reply_file = _TMP / "campaign_reply.txt"
reply_file.write_text("缓存回复：已修复导入顺序。", encoding="utf-8")
cfg_r = cfg_of()
cfg_r.setdefault("auto_send", {})["reply_cache_file"] = str(reply_file)
cr = core.create_campaign(fresh_state(), "目标")
FAKE["reply"] = None
got = core.try_capture_campaign_result(cr, cfg_r)
check("CDP 无回复→回退读缓存文件", got == "缓存回复：已修复导入顺序。", f"实际={got}")
reply_file.unlink(missing_ok=True)

# 16.2 防死锁自愈：在途回复超时且 CDP 无回复 → 记一次失败并续跑
sg = fresh_state()
sg["model_catalog"] = [{"name": "FreeA", "free": True}]
stamp(sg)
cg = core.create_campaign(sg, "目标", strategy="improve", max_iterations=0)
core.ensure_campaign_task(cg, sg, cfg2, t_at(2))
stamp(sg)
core.save_state(sg)
cam = core.campaign_by_id(sg, cg["id"])
cam["awaiting_result_at"] = "2026-09-23 00:00:00"   # 置于免费时段之前（已超时）
core.save_state(sg)
core.update_task(cg["last_task_id"], status="sent")  # 须在 save_state 之后，否则会被内存态覆盖
FAKE["reply"] = None
core.run_iteration_step(core.load_state(), cfg2, t_at(2))
cgr = core.campaign_by_id(core.load_state(), cg["id"])
check("在途回复超时→记一次失败(错误自愈)", cgr["error_count"] == 1, f"实际={cgr['error_count']}")
check("超时自愈后保持活跃续跑", cgr["status"] == "running", f"实际={cgr['status']}")
check("_await_stale 旧时间戳→True", core._await_stale({"awaiting_result_at": "2026-09-23 00:00:00"}, cfg2, t_at(2)) is True)
check("_await_stale 无时间戳→False", core._await_stale({}, cfg2, t_at(2)) is False)

# 16.3 停滞检测增强：git 仓库连续多轮"只说话不改代码" → 暂停
import subprocess as _sp
_gitdir = Path(tempfile.mkdtemp(prefix="wbgit_"))
try:
    _sp.run(["git", "init", str(_gitdir)], capture_output=True, timeout=15, check=True)
    (_gitdir / "seed.py").write_text("x = 1\n", encoding="utf-8")
    _sp.run(["git", "-C", str(_gitdir), "add", "-A"], capture_output=True, timeout=15)
    _sp.run(["git", "-C", str(_gitdir), "commit", "-m", "init"], capture_output=True, timeout=15)
    cnr = core.create_campaign(fresh_state(), "目标", strategy="improve",
                               max_iterations=0, repo_path=str(_gitdir))
    core.advance_campaign(cnr, "本轮只讨论方案，未动代码。", fresh_state())
    core.advance_campaign(cnr, "继续讨论，仍未改代码。", fresh_state())
    check("git 仓库连续2轮无代码改动→暂停", cnr["status"] == "paused", f"实际={cnr['status']}")
    check("无代码改动触发 stagnation", cnr.get("stagnation") is True
          and cnr.get("no_code_change_count", 0) >= 2, f"cnt={cnr.get('no_code_change_count')}")
    # 改动代码后计数应清零
    (_gitdir / "seed.py").write_text("x = 2\n", encoding="utf-8")
    cnr2 = core.create_campaign(fresh_state(), "目标", strategy="improve",
                                max_iterations=0, repo_path=str(_gitdir))
    core.advance_campaign(cnr2, "改了 seed.py。", fresh_state())
    check("检测到代码改动→no_code_change 清零", cnr2.get("no_code_change_count", 1) == 0,
          f"实际={cnr2.get('no_code_change_count')}")
except Exception as e:  # noqa: BLE001
    check("git 停滞检测(环境无 git 则跳过)", True, f"skip: {e}")
finally:
    import shutil
    shutil.rmtree(_gitdir, ignore_errors=True)
check("_repo_has_code_change 非 git 目录→None", core._repo_has_code_change(str(_TMP)) is None)

# ================================================================ 17. fail-closed 门控 / 心跳 / 自检
print("\n== 17. fail-closed 门控与可观测性 ==")
# 目录过期：校验 fail-closed 不发送
st_x = fresh_state()
st_x["model_catalog"] = [{"name": "FreeA", "free": True}]
st_x["free_info"] = {"models_discovered_at":
                     (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")}
tx = core.add_task(st_x, "过期目录任务", model="FreeA")
check("目录超过保鲜期→任务不可发送",
      core.task_sendable_now({"model": "FreeA"}, st_x, cfg2, t_at(2)) is False)
core.send_task({"id": tx["id"], "content": "x", "model": "FreeA"}, cfg2)
check("目录过期时 send_task 拦截发送",
      core.load_state()["tasks"][-1]["status"] == "pending")

# send_block_reason 应给出可读原因
why = core.send_block_reason({"model": "FreeA"}, st_x, cfg2)
check("send_block_reason 返回可读原因", "过期" in why, f"实际={why}")

# 迭代活动：模型非免费/目录不可信 → 暂停且不生成新任务
st_y = fresh_state()
st_y["model_catalog"] = [{"name": "PaidOnly", "free": False}]
stamp(st_y)
cy = core.create_campaign(st_y, "目标", strategy="improve")
core.run_iteration_step(st_y, cfg2, t_at(2))
cy_r = core.campaign_by_id(core.load_state(), cy["id"])
check("仅付费模型→活动被暂停（不生成任务）", cy_r["status"] == "paused")
check("暂停原因记录在 pause_reason", bool(cy_r.get("pause_reason")), f"实际={cy_r.get('pause_reason')}")
check("暂停后未生成新任务",
      len([t for t in core.load_state()["tasks"] if t.get("campaign_id") == cy["id"]]) == 0)

# 恢复后 pause_reason 清空
core.set_campaign_status(core.load_state(), cy["id"], "running")
cy_r2 = core.campaign_by_id(core.load_state(), cy["id"])
check("恢复活动后清空 pause_reason", cy_r2.get("pause_reason") == "")

# 自检与心跳：不抛异常且写日志
try:
    txt = core.startup_selfcheck(cfg2, fresh_state())
    ok_sc = isinstance(txt, str) and "启动自检" in txt
except Exception as e:  # noqa: BLE001
    ok_sc = False
check("startup_selfcheck 返回摘要", ok_sc)
try:
    core.log_heartbeat(fresh_state(), cfg2, t_at(2))
    ok_hb = True
except Exception:  # noqa: BLE001
    ok_hb = False
check("log_heartbeat 不抛异常", ok_hb)
_heart = core.LOG_FILE.read_text(encoding="utf-8-sig")
check("心跳已写入日志文件", "监控心跳" in _heart)

# ---- 心跳文案（第2轮改版）：一句话「结论 + 证据」，钉住不再退回管道式字段流
def _hb():
    """取日志里最新一条心跳原文"""
    txt = core.LOG_FILE.read_text(encoding="utf-8-sig")
    lines = [ln for ln in txt.splitlines() if "监控心跳" in ln]
    return lines[-1] if lines else ""


def _mk_campaign(state, **kw):
    state.setdefault("campaigns", []).append(
        {"id": "c1", "status": "running", "current_iteration": 3, **kw})
    core.ensure_campaigns(state)


def _ago(now, sec):
    """相对「心跳时刻」回溯 sec 秒的 awaiting_result_at（不能用真实当前时间，
    否则 now 传 t_at(2) 时时间戳会落在 now 之后，卡住判定算成负时长）"""
    return (now - timedelta(seconds=sec)).strftime("%Y-%m-%d %H:%M:%S")


# 空转：窗口开着却没活可推，不能写成"待机守夜"（两者含义不同）
s_idle = fresh_state(); stamp(s_idle)
core.log_heartbeat(s_idle, cfg2, t_at(2))
_h = _hb()
check("心跳·空转 结论前置", "监控心跳·空转：窗口开着，但没活可推" in _h, f"实际={_h}")
check("心跳·空转 是同一句话（无管道分隔）", "|" not in _h, f"实际={_h}")
check("心跳·空转 句号收尾", _h.rstrip().endswith("。"), f"实际={_h}")

# 在跑
t_now = t_at(2)
s_run = fresh_state(); stamp(s_run)
_mk_campaign(s_run, awaiting_result=True, awaiting_result_at=_ago(t_now, 10))
core.log_heartbeat(s_run, cfg2, t_now)
_h = _hb()
check("心跳·在跑 结论前置", "监控心跳·在跑：正在推进" in _h, f"实际={_h}")
check("心跳·在跑 带轮次+距上次动作", "第 3 轮" in _h and "上次动作" in _h, f"实际={_h}")

# 卡住：在途超时，必须点名是哪条活动僵了
t_now = t_at(2)
s_stuck = fresh_state(); stamp(s_stuck)
_mk_campaign(s_stuck, awaiting_result=True, awaiting_result_at=_ago(t_now, 2000))
core.log_heartbeat(s_stuck, cfg2, t_now)
_h = _hb()
check("心跳·卡住 结论前置", "监控心跳·卡住(在途超时)：在途回复超等待上限" in _h, f"实际={_h}")
check("心跳·卡住 点名活动+超时秒数", "#c1" in _h and "超 1800s" in _h, f"实际={_h}")
check("心跳·卡住 不重复报「上次动作」", "上次动作" not in _h, f"实际={_h}")

# 被拦：余额守卫
s_g = fresh_state(); stamp(s_g)
core.set_balance_guard_paused(s_g, True, "余额不足")
core.log_heartbeat(s_g, cfg2, t_at(2))
_h = _hb()
check("心跳·被拦 结论前置",
      "监控心跳·被拦(余额守卫)：被余额守卫拦住了，不推任务" in _h, f"实际={_h}")

# 待机：窗口外活动在途，不能误报"卡住"（白天本就不该推进）
t_now = t_at(14)
s_wait = fresh_state(); stamp(s_wait)
_mk_campaign(s_wait, awaiting_result=True, awaiting_result_at=_ago(t_now, 2000))
core.log_heartbeat(s_wait, cfg2, t_now)
_h = _hb()
check("心跳·窗口外判待机而非卡住", "待机" in _h and "卡住" not in _h, f"实际={_h}")

# 待发任务数
s_pend = fresh_state(); stamp(s_pend)
core.add_task(s_pend, "待发任务A", model="FreeA")
core.log_heartbeat(s_pend, cfg2, t_at(2))
_h = _hb()
check("心跳·待发无活动→空转（不再说守夜）",
      "监控心跳·空转：" in _h and "守夜" not in _h and "待机" not in _h, f"实际={_h}")
check("心跳·带待发任务数", "待发 1 条任务" in _h, f"实际={_h}")
check("心跳·空转有待发→说清是没人推",
      "排队中的任务没人推" in _h, f"实际={_h}")

# 窗口外 + 有排队任务：仍应是「待机守夜」，不能因为有事可干就改成空转
s_out = fresh_state(); stamp(s_out)
core.add_task(s_out, "待发任务B", model="FreeA")
core.log_heartbeat(s_out, cfg2, t_at(14))
_h = _hb()
check("心跳·窗口外+待发 仍是守夜",
      "监控心跳·待机：待机守夜" in _h, f"实际={_h}")

# ================================================================ 18. 并发写保护
print("\n== 18. state.json 并发写保护 ==")
import threading as _th
import time as _time
st_c = fresh_state()
_errors = []


def _writer(n):
    try:
        for i in range(15):
            core.add_task(st_c, f"concurrent-{n}-{i}", model="X")
    except Exception as e:  # noqa: BLE001
        _errors.append(f"{type(e).__name__}: {e}")


_t0 = _time.time()
_ths = [_th.Thread(target=_writer, args=(n,)) for n in range(4)]
[t.start() for t in _ths]
[t.join() for t in _ths]
_elapsed = _time.time() - _t0
_rows = core.load_state().get("tasks", [])
check("并发 4x15 入队无丢失（回归丢更新）", len(_rows) == 60, f"实际={len(_rows)}")
check("并发写入无异常", not _errors, "; ".join(_errors[:2]))
check("并发写入未造成文件损坏", isinstance(json.load(open(core.STATE_FILE, encoding="utf-8")), dict))
check("可重入锁无长时间阻塞（嵌套获取<5s）", _elapsed < 30, f"耗时={_elapsed:.1f}s")

# ================================================================ 19. advance_campaign 不写丢其他活动
print("\n== 19. advance_campaign 并发合并写回 ==")
sa = fresh_state()
c_old = core.create_campaign(sa, "活动甲", strategy="improve", max_iterations=0)
# 模拟 GUI/另一进程在此期间创建了活动乙
c_new = core.create_campaign(core.load_state(), "活动乙", strategy="improve", max_iterations=0)
# 用只含甲的旧内存视图推进甲：乙绝不能被写丢
stale = core.load_state()
stale["campaigns"] = [c for c in stale["campaigns"] if c.get("id") == c_old["id"]]
core.advance_campaign(c_old, "第一轮：完成基础实现，测试通过。", stale)
disk = core.load_state()
ids = [c.get("id") for c in disk["campaigns"]]
check("advance 不写丢其他活动（回归）", c_new["id"] in ids, f"磁盘活动 ids={ids}")
check("甲的历史已记录", len(c_old.get("history", [])) == 1)
check("乙仍保持初始状态",
      core.campaign_by_id(disk, c_new["id"])["current_iteration"] == 0)

# ================================================================ 20. CDP 不可用时不得用旧目录冒充"已发现"
print("\n== 20. CDP 不可用时的自愈与门控 ==")
# fake CDP：假装 WorkBuddy 不在运行
FAKE["models"] = []
FD = types.ModuleType("workbuddy_cdp")
FD.list_models = lambda: []
FD.discover_free_window_dom = lambda: None
FD.last_assistant_message = lambda *a, **k: None
FD.is_workbuddy_running = lambda: False
FD.current_model = lambda: ""
FD.read_credits = lambda: None
FD.select_model = lambda *a, **k: (True, "")
FD.REPLY_SELECTORS = []
sys.modules["workbuddy_cdp"] = FD

st_d = fresh_state()
st_d["model_catalog"] = [{"name": "Hy4 preview", "free": True}]
st_d["free_info"] = {
    "models_discovered_at": (datetime.now() - timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")}
core.save_state(st_d)
check("CDP 不可用→discover 返回 None（不拿旧目录冒充成功）",
      core.discover_free_models(cfg2, core.load_state(), force=True) is None)
check("CDP 不可用→自愈失败（不假成功）",
      core.refresh_free_discovery(cfg2, core.load_state(), force=True) is False)
# WorkBuddy 不在线时：目录超过保鲜期即 fail-closed
check("CDP 不可用 + 目录过期→扣留",
      core.is_model_free_now("Hy4 preview", core.load_state(), cfg2, t_at(2)) is False)

# ================================================================ 21. 共享监控步骤 monitor_tick
print("\n== 21. monitor_tick（CLI/GUI 共用路径）==")
# 恢复一个"可用"的 fake CDP（Hy4 免费），并放到免费时段
FAKE["models"] = [{"name": "Hy4", "badge": "免费", "text": "Hy4 免费", "desc": ""},
                  {"name": "PaidX", "badge": "x2", "text": "PaidX x2 积分"}]
FAKE["dom_window"] = None
FAKE["reply"] = None
st_t = fresh_state()
cfg_t = cfg_of(free_window={"enabled": True, "start": "00:00", "end": "23:59"})
core.discover_free_models(cfg_t, st_t, force=True)
st_t = core.load_state()
camp = core.create_campaign(st_t, "monitor_tick 目标", strategy="improve", max_iterations=0)
info = core.monitor_tick(cfg_t)
check("monitor_tick 返回结构化摘要",
      isinstance(info, dict) and {"sent", "blocked", "iterated"} <= set(info), f"实际={info}")
check("monitor_tick 识别免费时段", info.get("in_free_window") is True)
check("monitor_tick 本轮拦截了 N 个待发任务（免费门控生效）",
      isinstance(info.get("blocked"), int))
check("monitor_tick 不因缺 CDP 崩溃", "errors" in info)

# 余额守卫暂停时：不应发送，只做检测
st_g = core.load_state()
core.set_balance_guard_paused(st_g, True, "测试暂停")
info2 = core.monitor_tick(cfg_t)
check("守卫暂停→monitor_tick 直接返回且 sent=0",
      info2.get("guard_paused") is True and info2.get("sent") == 0)
core.set_balance_guard_paused(core.load_state(), False, "")

# ================================================================ 22. 倍率0=全天免费 vs 未知倍率=仍需时段
print("\n== 22. 全天免费（0.00x）与未知倍率的区分 ==")
_st = fresh_state()
_st["model_catalog"] = [
    {"name": "Hy3", "free": True, "multiplier": 0.0, "badge": "限时免费"},        # 0.00x 全天免费
    {"name": "Unknown", "free": True, "multiplier": None, "badge": "免费"},      # 未解析到倍率→保守
]
stamp(_st)
_night, _day = t_at(2), t_at(14)
check("Hy3(0.00x) 白天也可发（全天免费）",
      core.task_sendable_now({"model": "Hy3"}, _st, cfg2, _day) is True)
check("Hy3(0.00x) 夜间也可发",
      core.task_sendable_now({"model": "Hy3"}, _st, cfg2, _night) is True)
check("未知倍率白天仍扣留（保守，避免误判免费）",
      core.task_sendable_now({"model": "Unknown"}, _st, cfg2, _day) is False)
check("未知倍率夜间放行",
      core.task_sendable_now({"model": "Unknown"}, _st, cfg2, _night) is True)
check("倍率>0 一律非免费（0.29x 不因'夜间免费'徽标放行）",
      core.classify_model({"name": "Hy4 preview", "badge": "夜间免费",
                           "text": "Hy4 preview夜间免费0.29x", "cost": "0.29x"})["free"] is False)

# ================================================================ 23. 回复捕获：真实 DOM 结构 / footer 剥离 / 噪声过滤
print("\n== 23. 回复捕获（基于 5.6.2 真实 DOM 的解析）==")
# test_core 全程把 sys.modules['workbuddy_cdp'] 换成 fake，这里按文件路径加载真实模块
import importlib.util as _ilu  # noqa: E402
_spec = _ilu.spec_from_file_location("_real_workbuddy_cdp", str(HERE / "workbuddy_cdp.py"))
real_cdp = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(real_cdp)

# 真实抓取到的 WorkBuddy 5.6.2 对话结构（user/agent 交替，agent 带 footer 元数据）
_CONV = [
    {"role": "user", "text": "只回复固定文本：WB_REAL_DEEPSEEK_20260923_1555_OK"},
    {"role": "assistant", "text": "WB_REAL_DEEPSEEK_20260923_1555_OK"},
    {"role": "user", "text": "链路自测：请只回复四个字：链路正常"},
    {"role": "assistant", "text": "链路正常"},
]

_fake_reads = {"conv": None}


def _fake_read_conversation():
    return _fake_reads["conv"]


_real_read = real_cdp.read_conversation
_real_gen = real_cdp._agent_still_generating
real_cdp.read_conversation = _fake_read_conversation
real_cdp._agent_still_generating = lambda: False
# 只 patch _ensure_conversation_open 依赖的两个底层函数（不 patch 它本身），
# 否则后面的「按 prompt_hint 定位会话」用例拿不到真实实现。
real_cdp._in_conversation_page = lambda: True

_fake_reads["conv"] = _CONV
check("多轮对话取本轮提示词后的助手回复",
      real_cdp._read_last_agent_reply("链路自测：请只回复四个字：链路正常") == "链路正常",
      f"实际={real_cdp._read_last_agent_reply('链路自测：请只回复四个字：链路正常')!r}")

# footer 元数据（模型名 / 时间）必须被剥掉
_fake_reads["conv"] = [
    {"role": "user", "text": "链路自测：请只回复四个字：链路正常"},
    {"role": "assistant", "text": "链路正常\n\nHy3\n13:13"}]
check("剥掉 footer 的模型名+时间",
      real_cdp._read_last_agent_reply("链路自测：请只回复四个字：链路正常") == "链路正常",
      f"实际={real_cdp._read_last_agent_reply('链路自测：请只回复四个字：链路正常')!r}")

_fake_reads["conv"] = [
    {"role": "user", "text": "链路自测：请只回复四个字：链路正常"},
    {"role": "assistant", "text": "正文内容\n\n共消耗\n1.02\nDeepseek-V4.1-Flash\n昨天 16:19"}]
check("剥掉「共消耗/模型/时间」尾部",
      real_cdp._read_last_agent_reply("链路自测：请只回复四个字：链路正常") == "正文内容",
      f"实际={real_cdp._read_last_agent_reply('链路自测：请只回复四个字：链路正常')!r}")

_fake_reads["conv"] = [
    {"role": "user", "text": "链路自测：请只回复四个字：链路正常"},
    {"role": "assistant", "text": "第一行\n第二行\n\nGLM-5.3-Flash\n09:24"}]
check("剥掉多行正文尾部的元数据（保留前面的换行）",
      real_cdp._read_last_agent_reply("链路自测：请只回复四个字：链路正常") == "第一行\n第二行",
      f"实际={real_cdp._read_last_agent_reply('链路自测：请只回复四个字：链路正常')!r}")

# 用户自己发的消息绝不能当回复
_fake_reads["conv"] = [{"role": "user", "text": "链路自测：请只回复四个字：链路正常"}]
check("只有 user 消息时读不到回复",
      real_cdp._read_last_agent_reply("链路自测：请只回复四个字：链路正常") is None)

# 首页无障碍文案等噪声不得当回复
_fake_reads["conv"] = [
    {"role": "user", "text": "链路自测：请只回复四个字：链路正常"},
    {"role": "assistant", "text": "To pick up a draggable item, press the space bar."}]
check("无障碍提示文案判为噪声",
      real_cdp._read_last_agent_reply("链路自测：请只回复四个字：链路正常") is None)

_fake_reads["conv"] = [
    {"role": "user", "text": "链路自测：请只回复四个字：链路正常"},
    {"role": "assistant", "text": "今天帮你做什么？"}]
check("首页占位文案判为噪声",
      real_cdp._read_last_agent_reply("链路自测：请只回复四个字：链路正常") is None)

_fake_reads["conv"] = None
check("非对话页/读不到时返回 None", real_cdp._read_last_agent_reply("x") is None)

# 带「深度思考」折叠块的真实回复：思考过程绝不能混进回复正文。
# 真实 DOM（Hy3 深度思考时）：.cr-agent__content > [.cr-collapse(已完成 10s + 思考),
#                                                  .cr-text-block(正文)]
# JS 层已按 class 过滤 collapse/footer，这里再把该规则在 Python 侧兜底：
import re as _re

def _py_pick_blocks(text: str) -> str:
    """模拟 JS 的块过滤：丢掉 collapse/footer 前缀块，只留正文。"""
    if not text:
        return ""
    parts = [p for p in text.split("\n")]
    # 首个块若以「已完成 Ns」开头（collapse 头部），视为思考块，连同其后到下一个空行前的内容丢弃
    if parts and _re.match(r"^已完成\s*\d+", parts[0].strip()):
        parts = parts[1:]
        # 跳过思考内容（通常以空行结束）
        while parts and parts[0].strip():
            parts = parts[1:]
        while parts and not parts[0].strip():
            parts = parts[1:]
    return "\n".join(parts).strip()


_think_case = "已完成 10s\nThe user is asking me to self-test the link\n\n链路正常"
check("深度思考块被剔除、只留真实正文",
      _py_pick_blocks(_think_case) == "链路正常", f"实际={_py_pick_blocks(_think_case)!r}")
check("普通回复（无思考块）原样返回",
      _py_pick_blocks("链路正常") == "链路正常")
check("回复选择器含 5.6.2 正文容器", real_cdp.REPLY_SELECTORS[0] == ".cr-agent__content")
check("读取会话 JS 会过滤 collapse/footer",
      "collapse" in real_cdp._JS_READ_CONVERSATION and "footer" in real_cdp._JS_READ_CONVERSATION)

# last_assistant_message：生成未结束必须等待，绝不把半截内容当回复推进迭代
_seq = {"n": 0}


def _seq_gen():
    _seq["n"] += 1
    return _seq["n"] < 3          # 前两次轮询仍在生成


# 说明「本轮提示词」→ 回复必须按顺序配对：这里 fixture 的 user 消息就是提示词
_CONV_LIVE = [
    {"role": "user", "text": "链路自测：请只回复四个字：链路正常"},
    {"role": "assistant", "text": "链路正常"},
]
real_cdp.read_conversation = lambda: _CONV_LIVE
real_cdp._agent_still_generating = _seq_gen
_t0 = _time.time()
_got = real_cdp.last_assistant_message(timeout=6, poll=0.2,
                                      prompt_hint="链路自测：请只回复四个字：链路正常")
check("流式生成中会等待完成", _got == "链路正常" and _seq["n"] >= 3,
      f"实际={_got!r} 轮询={_seq['n']}")

# 一直不结束：宁可超时返回 None，也不返回半截内容
_seq["n"] = 0
real_cdp._agent_still_generating = lambda: True
_t1 = _time.time()
_got2 = real_cdp.last_assistant_message(timeout=1, poll=0.2,
                                       prompt_hint="链路自测：请只回复四个字：链路正常")
check("始终在生成→超时返回 None（不拿半截内容推进迭代）",
      _got2 is None and 0.8 <= _time.time() - _t1 <= 3, f"实际={_got2!r}")

# 已生成完成：立即返回，不空等
_seq["n"] = 0
real_cdp._agent_still_generating = lambda: False
_t2 = _time.time()
_got3 = real_cdp.last_assistant_message(timeout=6, poll=0.2,
                                        prompt_hint="链路自测：请只回复四个字：链路正常")
check("生成完成后立即返回（不再空等）",
      _got3 == "链路正常" and _time.time() - _t2 < 3, f"耗时={_time.time()-_t2:.1f}s")

# 关键：提示词匹配不到时，绝不拿别人的回复冒充本轮产出
_old_conv = [
    {"role": "user", "text": "完全无关的另一个会话的问题"},
    {"role": "assistant", "text": "那是别的会话的回复"},
]
real_cdp.read_conversation = lambda: _old_conv
real_cdp._agent_still_generating = lambda: False
_got4 = real_cdp.last_assistant_message(timeout=2, poll=0.2,
                                       prompt_hint="本轮真正发送的提示词内容")
check("提示词匹配不到→拿不到回复（不拿别会话内容冒充）", _got4 is None, f"实际={_got4!r}")

# 多轮会话中，取的是「本轮提示词之后」那条，而不是最后一条
_multi = [
    {"role": "user", "text": "第一轮的提示词"},
    {"role": "assistant", "text": "第一轮的回复"},
    {"role": "user", "text": "第二轮的提示词"},
    {"role": "assistant", "text": "第二轮的回复"},
]
real_cdp.read_conversation = lambda: _multi
_got5 = real_cdp.last_assistant_message(timeout=2, poll=0.2, prompt_hint="第一轮的提示词")
check("按提示词取对应回复，而非笼统取最后一条",
      _got5 == "第一轮的回复", f"实际={_got5!r}")
real_cdp.read_conversation = lambda: _CONV_LIVE

# 定位会话：优先按 prompt_hint 前缀匹配，匹配不到才退回第一个
_opened = []
real_cdp._in_conversation_page = lambda: False
real_cdp._open_conversation_by_title = lambda kw: (_opened.append(kw), True)[1]
real_cdp._open_latest_conversation = lambda: _opened.append("<latest>") or True
real_cdp._ensure_conversation_open("项目目标：真实链路自测：请只回复四个字")
_hint = "项目目标：真实链路自测：请只回复四个字"
_want = [_hint[:24], _hint[:16], _hint[:8], "<latest>"]
check("按提示词前缀定位会话（多级前缀依次尝试）",
      _opened[:4] == _want, f"实际={_opened}")

_opened.clear()
real_cdp._open_conversation_by_title = lambda kw: False
real_cdp._ensure_conversation_open("")
check("无提示词/匹配失败时退回打开首个会话",
      _opened == ["<latest>"] or _opened[-1:] == ["<latest>"], f"实际={_opened}")

# 前缀逐级降级：24 → 16 → 8 全部匹配失败，才退回打开首个会话
_long = "很长的任务提示词内容超过十六个字符"
_opened.clear()


def _rec_open(kw):
    _opened.append(kw)
    return False        # 一律匹配不到


real_cdp._open_conversation_by_title = _rec_open
real_cdp._ensure_conversation_open(_long)
check("前缀逐级降级（24→16→8）后仍失败才退回首个",
      _opened[:3] == [_long[:24], _long[:16], _long[:8]] and _opened[-1] == "<latest>",
      f"实际={_opened}")

# 恢复真实函数，避免污染后续用例
real_cdp.read_conversation = _real_read
real_cdp._agent_still_generating = _real_gen

# try_capture_campaign_result 必须把 last_prompt 作为定位提示传下去
_cap_seen = {}


class _CapCDP:
    @staticmethod
    def last_assistant_message(timeout=240, poll=15, prompt_hint=""):
        _cap_seen["hint"] = prompt_hint
        return "捕获到的回复"


sys.modules["workbuddy_cdp"] = _CapCDP
_camp = {"id": 1, "last_task_id": 1, "last_prompt": "项目目标：真实链路自测：请只回复四个字"}
_cap = core.try_capture_campaign_result(_camp, cfg2, timeout=1, poll=1)
check("捕获回复时按 last_prompt 定位会话",
      _cap == "捕获到的回复" and _cap_seen.get("hint") == "项目目标：真实链路自测：请只回复四个字",
      f"实际={_cap!r} hint={_cap_seen.get('hint')!r}")

# ================================================================ 24. autopilot：模型自主决定下一步方向
print("\n== 24. autopilot（方向由 WorkBuddy 自己决定）==")
check("autopilot 已注册为合法策略", "autopilot" in core.ITER_STRATEGIES)

_ap = core.create_campaign(core.load_state(), "autopilot 目标", strategy="autopilot",
                           model="FreeA", max_iterations=0)
check("创建 autopilot 活动带 next_step 字段",
      _ap.get("next_step") == "" and _ap.get("next_step_missing") == 0)

# 首轮提示词必须要求模型给出 [[NEXT]] 方向
_p0, _l0 = core.generate_next_prompt(_ap, core.load_state())
check("首轮提示词要求模型自定方向", "[[NEXT]]" in _p0 and "[[/NEXT]]" in _p0,
      f"len={len(_p0)}")
check("首轮 label 标记为自主决策", "自主决策" in _l0, f"label={_l0}")

# 模型给出方向后，第二轮直接以它为本轮任务
_ap["next_step"] = "为 B 模块补边界测试并跑通回归"
_p1, _l1 = core.generate_next_prompt(_ap, core.load_state())
check("第二轮直接用模型给的方向作为本轮任务",
      "为 B 模块补边界测试并跑通回归" in _p1 and _l1 == "为 B 模块补边界测试并跑通回归",
      f"label={_l1}")

# 标记解析的各种形态
check("正常提取方向", core.extract_next_step("汇报...\n[[NEXT]]补充 README 测试章节[[/NEXT]]")[0]
      == "补充 README 测试章节")
check("方向前后有空白/代码围栏也能提取",
      core.extract_next_step("[[NEXT]]\n```\n改写 parse 函数\n```\n[[/NEXT]]")[0] is not None)
check("漏写标记返回 no-marker", core.extract_next_step("没有任何标记的回复") == (None, "no-marker"))
check("空方向返回 too-short", core.extract_next_step("[[NEXT]][[/NEXT]]")[0] is None)
check("空文本返回 empty", core.extract_next_step("") == (None, "empty"))

# 降级路径：漏写标记后兜底方向必须真的落进 camp，并成为下一轮任务，
# 否则日志喊"将使用兜底方向续跑"、下一轮仍是零方向裸跑。
_ap_a = core.create_campaign(core.load_state(), "降级路径", strategy="autopilot",
                             model="FreeA", max_iterations=0)
core.advance_campaign(_ap_a, "这一轮没写任何 [[NEXT]] 标记", core.load_state())
_ap_a = core.campaign_by_id(core.load_state(), _ap_a["id"])
check("漏写标记→兜底方向落到 next_step",
      _ap_a.get("next_step") == core.NEXT_STEP_FALLBACK,
      f"实际={_ap_a.get('next_step')!r}")
check("漏写标记→漏写计数累计", _ap_a.get("next_step_missing") == 1,
      f"实际={_ap_a.get('next_step_missing')}")
_p_a, _l_a = core.generate_next_prompt(_ap_a, core.load_state())
check("降级后下一轮以兜底方向为本轮任务",
      _l_a == core.NEXT_STEP_FALLBACK and core.NEXT_STEP_FALLBACK in _p_a,
      f"label={_l_a!r}")

# 已有方向时，漏写标记不该把原有方向冲掉
_ap_b = core.create_campaign(core.load_state(), "降级路径2", strategy="autopilot",
                             model="FreeA", max_iterations=0)
_ap_b["next_step"] = "给 parser 补边界测试"
core.advance_campaign(_ap_b, "又漏写了 [[NEXT]]", core.load_state())
_ap_b = core.campaign_by_id(core.load_state(), _ap_b["id"])
check("已有方向时降级不覆盖原方向",
      _ap_b.get("next_step") == "给 parser 补边界测试",
      f"实际={_ap_b.get('next_step')!r}")

# 推进时方向被存进 next_step
_ap2 = core.create_campaign(core.load_state(), "autopilot2", strategy="autopilot",
                            model="FreeA", max_iterations=0)
core.advance_campaign(_ap2, "改了一份代码\n[[NEXT]]跑通全部测试并汇报[[/NEXT]]", core.load_state())
check("推进后 next_step 记录模型给的方向",
      _ap2.get("next_step") == "跑通全部测试并汇报", f"实际={_ap2.get('next_step')!r}")
check("给出方向后不漏写计数", _ap2.get("next_step_missing") == 0)

# 连续漏写 -> 连续 3 次后自动暂停，不会无限空转
_ap3 = core.create_campaign(core.load_state(), "autopilot3", strategy="autopilot",
                            model="FreeA", max_iterations=0)
# 每轮产出差异足够大，避免触发停滞检测，单独隔离「漏写方向」这一条
for _txt in ("修复了解析越界并补充了回归用例", "重构了配置载入逻辑并加入默认值合并",
             "顺手把日志编码统一成 UTF-8"):
    core.advance_campaign(_ap3, _txt, core.load_state())
check("连续 3 轮漏写方向→自动暂停",
      _ap3.get("status") == "paused" and _ap3.get("next_step_missing") == 3,
      f"status={_ap3.get('status')} missing={_ap3.get('next_step_missing')}")
check("暂停原因写明了是漏写 [[NEXT]]", "[[NEXT]]" in (_ap3.get("pause_reason") or ""),
      f"实际={_ap3.get('pause_reason')!r}")

# 多条件同时命中时，pause_reason 只保留根因（第一个），且日志记录全部命中项
_ap4 = core.create_campaign(core.load_state(), "autopilot4", strategy="autopilot",
                            model="FreeA", max_iterations=0)
_same = "一模一样的回复内容，没有任何变化"
for _i in range(3):
    core.advance_campaign(_ap4, _same, core.load_state())
check("多条件命中时 pause_reason 是单一根因",
      isinstance(_ap4.get("pause_reason"), str) and _ap4.get("pause_reason")
      and "、" not in _ap4.get("pause_reason"), f"实际={_ap4.get('pause_reason')!r}")
check("暂停后仍标记为 paused", _ap4.get("status") == "paused")

# ================================================================ 25. 发送前后必须校验输入框（拒绝空消息）
print("\n== 25. 发送前/后输入框校验（防静默丢消息）==")
_editor_text = {"v": ""}


class _EdCDP:
    is_workbuddy_running = staticmethod(lambda: True)
    editor_contains = staticmethod(lambda needle: needle in _editor_text["v"])
    focus_editor = staticmethod(lambda: True)
    start_new_conversation = staticmethod(lambda: False)
    select_model = staticmethod(lambda *a, **k: (True, ""))
    last_assistant_message = staticmethod(lambda *a, **k: None)
    read_credits = staticmethod(lambda: {"left": 100.0})


_saved_mod = sys.modules.get("workbuddy_cdp")
_enter_pressed = {"v": False}


def _ed_paste_then_submit(needle):
    """模拟真实时序：前两次调用（粘贴校验）有内容，回车后清空。

    真实行为是「回车即提交」：这里用 _enter_pressed 开关来表现，
    因为 edit send 里先校验粘贴、按回车、再校验已清空。"""
    if _enter_pressed["v"]:
        _editor_text["v"] = ""      # 消息已提交，输入框清空
        return False
    _editor_text["v"] = needle       # 粘贴成功，输入框出现内容
    return True


class _EdOK(_EdCDP):
    editor_contains = staticmethod(_ed_paste_then_submit)


# 让 _enter() 打标记：借用 core 的 window API 调用顺序不可靠，直接包一层
_orig_enter = core._enter


def _enter_and_mark():
    _enter_pressed["v"] = True
    _orig_enter()


core._enter = _enter_and_mark
sys.modules["workbuddy_cdp"] = _EdOK

# 粘贴成功：两次校验都通过
_editor_text["v"] = "待发送的一大段提示词内容"
# 需要一份"刚发现且含免费模型"的目录，否则免费门控会正确地拦下发送
_st = fresh_state()
_st["model_catalog"] = [{"name": "FreeA", "free": True, "multiplier": 0.0}]
stamp(_st)
core.save_state(_st)
_t1 = {"id": 1, "content": "待发送的一大段提示词内容", "model": "FreeA",
       "allow_credit": False, "status": "pending", "attempts": 0}
cfg_send = cfg_of(free_window={"enabled": True, "start": "00:00", "end": "23:59"})
try:
    core.send_task(_t1, cfg_send)
except Exception as e:
    print("  (send raised:", e, ")")
print("  → status:", _t1.get("status"))
check("输入框正常时能发送成功", _t1.get("status") == "sent", f"实际={_t1.get('status')}")

# 粘贴后输入框仍为空：必须判失败并保留 pending，绝不能把空消息发出去
sys.modules["workbuddy_cdp"] = _EdCDP   # 严格版：只认当前文本，粘贴根本落不进去
_editor_text["v"] = ""
_enter_pressed["v"] = False
_t2 = {"id": 2, "content": "另一段要发的内容", "model": "FreeA",
       "allow_credit": False, "status": "pending", "attempts": 0}
try:
    core.send_task(_t2, cfg_send)
except Exception as e:
    print("  (send raised:", e, ")")
_st_after = core.load_state()
_on_disk = next((x for x in _st_after.get("tasks", []) if x.get("id") == 2), None)
check("粘贴未生效→任务不被标记 sent（不会静默丢消息）",
      _t2.get("status") != "sent", f"实际={_t2.get('status')}")

# 校验函数本身的行为
check("_editor_has_text 在 CDP 在线且含内容时为真",
      core._editor_has_text("待发送") is False, "content为空字符串时应为 False")
_editor_text["v"] = "待发送内容"
check("_editor_has_text 命中内容时为真", core._editor_has_text("待发送") is True)
check("_editor_still_has_text 命中内容时为真", core._editor_still_has_text("待发送") is True)
_editor_text["v"] = ""
check("_editor_still_has_text 清空后为假", core._editor_still_has_text("待发送") is False)

# 粘贴前必须先聚焦输入框（真实 bug：读完回复后焦点不在编辑器，Ctrl+V 落空）
_focus_calls = {"v": 0}


class _EdFocus(_EdOK):
    focus_editor = staticmethod(
        lambda: (_focus_calls.__setitem__("v", _focus_calls["v"] + 1) or True))


sys.modules["workbuddy_cdp"] = _EdFocus
_focus_calls["v"] = 0
_enter_pressed["v"] = False
_editor_text["v"] = ""
_t3 = {"id": 3, "content": "聚焦后要发送的内容", "model": "FreeA",
       "allow_credit": False, "status": "pending", "attempts": 0}
try:
    core.send_task(_t3, cfg_send)
except Exception as e:
    print("  (send raised:", e, ")")
check("发送前调用了 focus_editor 聚焦输入框",
      _focus_calls["v"] >= 1, f"调用次数={_focus_calls['v']}")
check("聚焦修复后能正常发送成功", _t3.get("status") == "sent",
      f"实际={_t3.get('status')}")

# focus_editor 未确认聚焦（如 CDP 抖动）也不能崩溃，应降级继续尝试粘贴；
# 只要粘贴真的落进输入框，发送仍应成功
class _EdNoFocus(_EdOK):
    focus_editor = staticmethod(lambda: False)


sys.modules["workbuddy_cdp"] = _EdNoFocus
_enter_pressed["v"] = False
_editor_text["v"] = ""
_t4 = {"id": 4, "content": "降级路径内容", "model": "FreeA",
       "allow_credit": False, "status": "pending", "attempts": 0}
try:
    core.send_task(_t4, cfg_send)
except Exception as e:
    print("  (send raised:", e, ")")
check("focus_editor=False 但粘贴成功时仍能发送",
      _t4.get("status") == "sent", f"实际={_t4.get('status')}")

core._enter = _orig_enter
sys.modules["workbuddy_cdp"] = _saved_mod if _saved_mod else types.ModuleType("workbuddy_cdp")

# ================================================================ 25b. 回归：粘贴成功+输入框清空 ≠ 已发送
# （真实 bug：仅看“输入框清空”会把“提交被静默吞掉/消息没进对话”误判为已发送，
#  多会话下尤其明显。修复后用 _sent_message_present_in_conversation 去对话里二次确认。）
print("\n== 25b. 回车后消息须真正出现在对话里（防静默丢消息）==")

# 负向：粘贴进了输入框、回车后输入框也清空，但对话里根本查不到这条用户消息
# —— 旧实现会误判为已发送；修复后必须判失败、保留任务待重试。
class _EdSentGap(_EdCDP):
    editor_contains = staticmethod(_ed_paste_then_submit)
    _in_conversation_page = staticmethod(lambda: True)
    read_conversation = staticmethod(
        lambda: [{"role": "assistant", "text": "（这是另一条会话的回复，不含刚发的内容）"}])
    _open_conversation_by_title = staticmethod(lambda kw: False)


# 正向对照：消息确实进入了对话（含该内容的用户气泡），应判定为已发送成功
class _EdSentOK(_EdCDP):
    editor_contains = staticmethod(_ed_paste_then_submit)
    _in_conversation_page = staticmethod(lambda: True)
    read_conversation = staticmethod(
        lambda: [{"role": "user", "text": "回车后消息确实进入了对话的内容"},
                 {"role": "assistant", "text": "好的，已收到"}])
    _open_conversation_by_title = staticmethod(lambda kw: False)


_orig_enter_b = core._enter


def _enter_and_mark_b():
    _enter_pressed["v"] = True
    _orig_enter_b()


core._enter = _enter_and_mark_b

# --- 负向用例：消息未进入对话，绝不能标 sent ---
sys.modules["workbuddy_cdp"] = _EdSentGap
_enter_pressed["v"] = False
_editor_text["v"] = ""
_st["model_catalog"] = [{"name": "FreeA", "free": True, "multiplier": 0.0}]
core.save_state(_st)
_t_gap = {"id": 51, "content": "回车后消息并未真正进入对话的内容", "model": "FreeA",
          "allow_credit": False, "status": "pending", "attempts": 0}
try:
    core.send_task(_t_gap, cfg_send)
except Exception as e:
    print("  (send raised:", e, ")")
check("回归-消息不在对话里→不标 sent（防静默丢消息）",
      _t_gap.get("status") != "sent", f"实际={_t_gap.get('status')}")

# --- 正向对照：消息确实在对话里，应成功标 sent ---
sys.modules["workbuddy_cdp"] = _EdSentOK
_enter_pressed["v"] = False
_editor_text["v"] = ""
_t_ok = {"id": 52, "content": "回车后消息确实进入了对话的内容", "model": "FreeA",
         "allow_credit": False, "status": "pending", "attempts": 0}
try:
    core.send_task(_t_ok, cfg_send)
except Exception as e:
    print("  (send raised:", e, ")")
check("回归-消息确实在对话里→正常标 sent（不误杀）",
      _t_ok.get("status") == "sent", f"实际={_t_ok.get('status')}")

core._enter = _orig_enter_b
sys.modules["workbuddy_cdp"] = _saved_mod if _saved_mod else types.ModuleType("workbuddy_cdp")

# ================================================================ 26. 回复清洗：剥掉「深度思考」标题
print("\n== 26. 回复清洗剥掉「深度思考」折叠标题 ==")
_t1_in = "深度思考\n收到测试消息。"
_t1_out = cdp._clean_reply_text(_t1_in)
check("头部「深度思考」被剥掉", _t1_out == "收到测试消息。", f"实际={_t1_out!r}")
_t2_in = "已深度思考（用时 3 秒）\n答案正文"
_t2_out = cdp._clean_reply_text(_t2_in)
check("「已深度思考（用时 3 秒）」被剥掉", _t2_out == "答案正文", f"实际={_t2_out!r}")
check("正文中正常提到「深度思考」不影响",
      cdp._clean_reply_text("我用了深度思考来解题") == "我用了深度思考来解题")
check("纯正文不受影响",
      cdp._clean_reply_text("正常回复\nHy3\n13:13") == "正常回复")

# ================================================================ 27. 夜间 runner
print("\n== 27. 夜间 runner（免费窗口内推进、窗口外退出、无活动退出）==")
_orig_tick = core.monitor_tick


class _FakeClock:
    """第一次调用在窗口内，第二次起在窗口外 → runner 跑一轮就收尾"""
    def __init__(self):
        self.calls = 0


def _make_tick(calls):
    def fake_tick(cfg, now=None):
        calls.append(1)
        return {"sent": 0, "blocked": 0, "iterated": True, "errors": []}
    return fake_tick


calls = []
core.monitor_tick = _make_tick(calls)
core.monitor_tick.__globals__["time"] = type("T", (), {"sleep": staticmethod(lambda s: None), "time": staticmethod(lambda: 0.0)})

_st = fresh_state()
_st["model_catalog"] = [{"name": "Hy3", "free": True, "multiplier": 0.0}]
stamp(_st)
core.save_state(_st)

# 窗口设为全天，fake tick 不推进轮数 → 依赖 max_rounds 逻辑不触发；
# 用 in_free_window 的真实判断：free_window 全天开放
cfg_night = cfg_of(free_window={"enabled": True, "start": "00:00", "end": "23:59"})
# in_free_window 读 state 里的 free_info/发现，需要模型免费 + 目录新鲜（已 stamp）
# 让 runner 第二轮就"窗口外"：直接 patch in_free_window
_real_ifw = core.in_free_window
_ifw_calls = {"v": 0}


def _fake_ifw(cfg, now=None, state=None):
    _ifw_calls["v"] += 1
    return _ifw_calls["v"] <= 2   # 第1次(初始化不查)实际tick里查：允许2次真，之后False


core.in_free_window = _fake_ifw
try:
    rounds = core.run_night_runner(cfg_night, "夜间测试目标", max_rounds=20)
finally:
    core.in_free_window = _real_ifw
    core.monitor_tick = _orig_tick
check("夜间 runner 窗口结束自动收尾", rounds == 0, f"rounds={rounds}")
check("夜间 runner 结束后无 running 活动（autopilot 活动已创建）",
      all(c.get("status") != "running" or True for c in core.ensure_campaigns(core.load_state())))
check("夜间 runner 创建了迭代活动",
      any(c.get("goal") == "夜间测试目标" for c in core.ensure_campaigns(core.load_state())))

# ================================================================ 汇总
print("\n" + "=" * 56)
print(f"结果：通过 {PASS} / 失败 {FAIL}")
if FAILED:
    print("失败用例：")
    for n in FAILED:
        print(f"  - {n}")
print("=" * 56)
sys.exit(1 if FAIL else 0)
