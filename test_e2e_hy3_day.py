# -*- coding: utf-8 -*-
"""实测：白天 hy3 免费场景下的自动迭代流程。
注入 fake workbuddy_cdp 模拟「已运行且开启 CDP 的 WorkBuddy」，其中：
  - hy3:    免费模型，专属免费时段 白天 08:00-23:00
  - hy4:    免费模型，但专属免费时段 夜间 23:00-08:00（白天不免费，用于对比）
  - Hunyuan:x2 积分（非免费）
把全局免费窗口设为白天 08:00-23:00（代表「白天即免费时段」），在白天语义时间(14:31)实测：
  发现 hy3 免费 -> 自动选 hy3（而非 hy4）-> 发送 -> 余额无扣 -> 推进迭代。
说明：本环境无真实 WorkBuddy，真实窗口发送被 mock 为成功，其余完全走真实代码路径。
"""
import sys, types, tempfile
from pathlib import Path
from datetime import datetime

sys.path.insert(0, ".")
TMP = Path(tempfile.mkdtemp())

# ---------------- 注入 fake workbuddy_cdp ----------------
fake = types.ModuleType("workbuddy_cdp")
_select_log = []

fake.list_models = lambda: [
    {"name": "hy3", "badge": "免费", "text": "hy3 免费", "desc": "白天 08:00-23:00 免费"},
    {"name": "hy4", "badge": "免费", "text": "hy4 免费", "desc": "夜间 23:00-08:00 免费"},
    {"name": "Hunyuan", "badge": "x2", "text": "Hunyuan x2 积分"},
]

def _discover_window():
    # 返回 None -> 回退到 config 的白天窗口，避免 DOM 发现的夜间窗口覆盖
    return None

def _select(name):
    _select_log.append(name)
    return (True, "")

_n = [0]
_changes = [
    "修复 _parse_multiplier 误把模型名数字当倍率",
    "重构 classify_model 两级免费判定与负向成本信号",
    "新增免费模型目录磁盘缓存与 free_confidence",
    "优化限流消息解析支持秒级",
    "补充端到端集成测试与 CI",
    "完善 README 一键启动说明",
]
def _reply(*a, **k):
    r = _changes[_n[0] % len(_changes)]
    _n[0] += 1
    return f"第{_n[0]}轮完成：{r}。验证命令 test_core 通过，py_compile 通过。"

fake.discover_free_window_dom = _discover_window
fake.select_model = _select
fake.last_assistant_message = _reply
fake.current_model = lambda: "hy3"
fake.read_credits = lambda: {"left": 100.0, "total": 100.0, "used": 0.0,
                             "is_paid": False, "packages": []}
fake.is_workbuddy_running = lambda: True
fake.REPLY_SELECTORS = []
sys.modules["workbuddy_cdp"] = fake

import workbuddy_monitor as core
core.STATE_FILE = TMP / "state.json"
core.CONFIG_FILE = TMP / "config.json"
core.LOG_FILE = TMP / "monitor.log"
# 真实窗口发送无法在无窗口时执行，mock 为成功（其余路径真实）
core.auto_send_to_workbuddy = lambda content, title=None, model="": (True, "mocked-send")

cfg = core.load_config()
cfg.setdefault("auto_send", {})["enabled"] = True
cfg["auto_send"]["free_only"] = True
cfg["auto_send"]["free_window"] = {"start": "08:00", "end": "23:00"}  # 白天即免费时段
cfg["send_mode"] = "notify"
print("配置: send_mode=notify, auto.enabled=True, free_only=True, 全局免费窗口=08:00-23:00(白天)")

state = core.load_state()
now = datetime(2026, 9, 23, 14, 31)  # 白天语义时间
print(f"当前语义时间: {now:%Y-%m-%d %H:%M:%S}（白天）")

print("\n" + "=" * 56)
print("实测A: 从 CDP 发现免费模型与时段")
print("=" * 56)
cat = core.discover_free_models(cfg, state, force=True)
for c in cat:
    fw = c.get("free_window")
    print(f"  {c['name']:8} free={c['free']} conf={c['free_confidence']} "
          f"window={fw['start'] + '-' + fw['end'] if fw else '-'} reason={c['free_reason']}")
win = core.discover_free_window(cfg, state, force=True)
print(f"  全局免费时段(回退 config): {win['start']}-{win['end']}")

print("\n" + "=" * 56)
print("实测B: 白天免费门控判定（hy3 vs hy4 vs 积分模型）")
print("=" * 56)
h3 = core.is_model_free_now("hy3", state, cfg, now)
h4 = core.is_model_free_now("hy4", state, cfg, now)
hu = core.is_model_free_now("Hunyuan", state, cfg, now)
print(f"  is_model_free_now('hy3')    @白天 = {h3}  (预期 True)")
print(f"  is_model_free_now('hy4')    @白天 = {h4}  (预期 False, 仅夜间免费)")
print(f"  is_model_free_now('Hunyuan')@白天 = {hu}  (预期 False, 积分模型)")

print("\n" + "=" * 56)
print("实测C: 创建自动迭代活动（model=''，交给引擎自动选白天免费模型）")
print("=" * 56)
camp = core.create_campaign(
    state, "实测：白天用 hy3 免费迭代打磨监控器", strategy="staged", model="",
    stages=["阶段A", "阶段B", "阶段C", "阶段D", "阶段E"],
    repo_path=str(TMP), test_cmd="python test_core.py")  # TMP 非 git，避免无改动暂停
camp_id = camp["id"]
print(f"  创建 campaign #{camp_id} model=auto")

def drive_one_round(now):
    """严格复刻 run_loop：先发送所有可发的 pending 任务，再推进迭代。"""
    for t in core.pending_tasks(state):
        if core.task_sendable_now(t, state, cfg, now):
            core.send_task(t, cfg)
    core.run_iteration_step(state, cfg, now=now)

print("\n" + "=" * 56)
print("实测D: 端到端自动迭代闭环（白天，跑 6 轮）")
print("=" * 56)
for i in range(6):
    drive_one_round(now)
    c = core.campaign_by_id(state, camp_id)
    print(f"  轮{i+1}: stage={c['stage_index']} iter={c['current_iteration']} "
          f"history={len(c['history'])} status={c['status']} "
          f"本轮所选模型={_select_log[-1] if _select_log else '-'}")

final = core.campaign_by_id(state, camp_id)
print("\n" + "=" * 56)
print("实测E: 断言与结论")
print("=" * 56)
print("  自动选择的模型序列:", _select_log)
assert h3 is True, "✗ 白天 hy3 应判定为免费"
assert h4 is False, "✗ 白天 hy4 不应判定为免费（误判会错用夜间模型）"
assert hu is False, "✗ 积分模型不应判定为免费"
assert all(m == "hy3" for m in _select_log), f"✗ 每轮都应自动选 hy3，实际 {_select_log}"
assert final["current_iteration"] >= 4, "✗ 应推进至少 4 轮"
assert len(final["history"]) >= 4, "✗ 应至少有 4 条迭代历史"
print("  ✓ 白天 hy3 正确判定为免费，且被自动选为迭代模型:", set(_select_log))
print("  ✓ 白天 hy4 正确判定为非免费（未被误选，避免错乱）")
print("  ✓ 积分模型 Hunyuan 绝不被选（free_only 保护）")
print("  ✓ 迭代推进轮数:", final["current_iteration"], " 历史条数:", len(final["history"]))
print("  ✓ 端到端自动迭代闭环（白天 hy3 免费）验证通过")
print("  临时目录:", TMP)
