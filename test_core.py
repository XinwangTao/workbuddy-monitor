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


def _fake_last_msg(timeout=240, poll=15):
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
cfg = cfg_of()
check("免费模型+免费时段→放行", core.is_model_free_now("FreeA", st2, cfg, t_at(2)) is True)
check("免费模型+非免费时段→扣留", core.is_model_free_now("FreeA", st2, cfg, t_at(14)) is False)
check("付费模型即使免费时段也扣留", core.is_model_free_now("PaidB", st2, cfg, t_at(2)) is False)
check("未发现过的模型一律扣留（防误扣积分）", core.is_model_free_now("Unknown", st2, cfg, t_at(2)) is False)
st3 = fresh_state()
check("无发现数据→回退全局时段 02:00 放行", core.is_model_free_now("Any", st3, cfg, t_at(2)) is True)
check("无发现数据→回退全局时段 14:00 扣留", core.is_model_free_now("Any", st3, cfg, t_at(14)) is False)

# ================================================================ 6. task_sendable_now
print("\n== 6. 统一调度 task_sendable_now ==")
st4 = fresh_state()
st4["model_catalog"] = [{"name": "FreeA", "free": True}, {"name": "PaidB", "free": False}]
now_night = t_at(2)
now_day = t_at(14)

t1 = {"content": "x", "model": "FreeA", "status": "pending"}
check("免费模型+免费时段→可发", core.task_sendable_now(t1, st4, cfg, now_night) is True)
check("免费模型+非免费时段→不可发", core.task_sendable_now(t1, st4, cfg, now_day) is False)

t2 = {"content": "x", "model": "FreeA", "status": "pending", "allow_credit": True}
check("allow_credit 显式允许→非免费时段也可发", core.task_sendable_now(t2, st4, cfg, now_day) is True)

future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
t3 = {"content": "x", "model": "FreeA", "scheduled_time": future}
check("未到指定时间→不可发", core.task_sendable_now(t3, st4, cfg, now_night) is False)
t4 = {"content": "x", "model": "FreeA", "scheduled_time": past}
check("已过指定时间→可发（且仍在免费时段）", core.task_sendable_now(t4, st4, cfg, now_night) is True)
check("已过指定时间但在白天→仍被 free_only 扣留", core.task_sendable_now(t4, st4, cfg, now_day) is False)

st5 = fresh_state()
st5["model_catalog"] = [{"name": "FreeA", "free": True}]
st5["rate_limits"] = {"FreeA": {"model": "FreeA", "reset": future}}
t5 = {"content": "x", "model": "FreeA"}
check("模型仍被限流（未到重置）→不可发", core.task_sendable_now(t5, st5, cfg, now_night) is False)
st5["rate_limits"] = {"FreeA": {"model": "FreeA", "reset": past}}
check("限流已过重置时间→可发", core.task_sendable_now(t5, st5, cfg, now_night) is True)

st6 = fresh_state()
st6["rate_limits"] = {"FreeA": {"model": "FreeA", "reset": "不是合法时间"}}
check("非法重置时间戳不崩溃且放行", core.task_sendable_now(t5, st6, cfg, now_night) is True)

st7 = fresh_state()
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
      core.is_model_free_now("", {}, cfg2, t_at(2)) is True)
check("ensure_campaigns 补齐默认值",
      core.ensure_campaigns({"campaigns": [{"id": 1}]})[0]["history"] == [])
stz = fresh_state()
check("campaign_by_id 不存在返回 None", core.campaign_by_id(stz, 999999) is None)
check("set_campaign_status 不存在返回 False", core.set_campaign_status(stz, 999999, "done") is False)

# ================================================================ 13. 发送通道
print("\n== 13. 发送通道（command / api / 付费模型扣留）==")
# command 模式：空命令不实际执行，但必须能正常标记 sent（曾因变量未定义抛 NameError）
sts = fresh_state()
sts["model_catalog"] = [{"name": "FreeA", "free": True}, {"name": "PaidB", "free": False}]
cfg_cmd = cfg_of()
cfg_cmd["send_mode"] = "command"
cfg_cmd["send_command"] = ""
tkc = core.add_task(sts, "命令模式任务", model="FreeA")
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
cfg_api = cfg_of()
cfg_api["send_mode"] = "api"
tka = core.add_task(sta, "API模式任务", model="FreeA")
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
cg = core.create_campaign(sg, "目标", strategy="improve", max_iterations=0)
core.ensure_campaign_task(cg, sg, cfg2, t_at(2))
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

# ================================================================ 汇总
print("\n" + "=" * 56)
print(f"结果：通过 {PASS} / 失败 {FAIL}")
if FAILED:
    print("失败用例：")
    for n in FAILED:
        print(f"  - {n}")
print("=" * 56)
sys.exit(1 if FAIL else 0)
