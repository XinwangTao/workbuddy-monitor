# -*- coding: utf-8 -*-
"""实测层2：端到端自动迭代闭环（有 CDP 场景）。
通过注入 fake workbuddy_cdp 模拟「已运行且开启 CDP 的 WorkBuddy」，验证监控器在免费时段内
能否完成：发现免费模型 -> 选免费模型 -> 发送任务 -> 捕获回复 -> 推进迭代 的完整闭环。
说明：本环境无真实 WorkBuddy，故用 fake CDP 替代；免费窗口临时设为全天以「模拟免费时段」
（真实场景即 23:00-08:00）；真实窗口发送(auto_send_to_workbuddy 的 Ctrl+V 输入模拟)因无
真实 WorkBuddy 窗口被 mock 为成功，其余完全走真实代码路径。"""
import sys, types, tempfile
from pathlib import Path
from datetime import datetime

sys.path.insert(0, ".")
TMP = Path(tempfile.mkdtemp())

# ---------------- 注入 fake workbuddy_cdp ----------------
fake = types.ModuleType("workbuddy_cdp")
_select_log = []

fake.list_models = lambda: [
    {"name": "Hy4", "badge": "免费", "text": "Hy4 免费", "desc": ""},
    {"name": "Hunyuan", "badge": "x2", "text": "Hunyuan x2 积分"},
]

def _discover_window():
    # 返回 None -> 回退到 config 的「全天模拟免费时段」，避免 DOM 发现的 23:00-08:00 覆盖
    return None

def _select(name):
    _select_log.append(name)
    return (True, "")

_n = [0]
_changes = [
    "修复 _parse_multiplier 误把模型名数字当倍率",
    "重构 classify_model 免费判定为两级匹配",
    "新增免费模型目录磁盘缓存",
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
fake.current_model = lambda: "Hy4"
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
core.auto_send_to_workbuddy = lambda *a, **k: (True, "mocked-send")

cfg = core.load_config()
# 模拟免费时段：临时把免费窗口设为覆盖当前时间（真实场景即夜里 23:00-08:00）
cfg.setdefault("auto_send", {})["enabled"] = True
cfg["auto_send"]["free_only"] = True
cfg["auto_send"]["free_window"] = {"start": "00:00", "end": "23:59"}
cfg["send_mode"] = "notify"
print("配置: send_mode=notify, auto.enabled=True, 免费窗口=全天(模拟免费时段)")

state = core.load_state()

print("\n" + "=" * 56)
print("实测A: 从 CDP 发现免费模型")
print("=" * 56)
cat = core.discover_free_models(cfg, state, force=True)
print("  发现:", [(c["name"], "免费" if c["free"] else "积分") for c in cat])
win = core.discover_free_window(cfg, state, force=True)
print("  免费时段:", win)

print("\n" + "=" * 56)
print("实测B: 创建自动迭代活动（指定免费模型 Hy4）")
print("=" * 56)
camp = core.create_campaign(
    state, "实测：打磨监控器免费迭代", strategy="staged", model="Hy4",
    stages=["阶段A", "阶段B", "阶段C", "阶段D"],
    repo_path=str(TMP), test_cmd="python test_core.py")  # TMP 非 git，避免无改动暂停
camp_id = camp["id"]
print(f"  创建 campaign #{camp_id} model=Hy4")

def drive_one_round(now):
    """复刻 monitor_tick 一轮：先发可发的 pending -> 再推进迭代。

    每步都重新读盘：send_task 只改磁盘，握着旧的 state 快照会让下一轮
    继续看到已发送的任务，认领闸门拒绝后被误判成"未发送"。"""
    state = core.load_state()
    for t in core.pending_tasks(state):
        if core.task_sendable_now(t, state, cfg, now):
            core.send_task(t, cfg, now)
            state = core.load_state()
    core.run_iteration_step(state, cfg, now)

now = datetime(2026, 9, 23, 23, 30)  # 夜间语义时间
print("\n" + "=" * 56)
print("实测C: 端到端自动迭代闭环（跑 6 轮）")
print("=" * 56)
for i in range(6):
    drive_one_round(now)
    c = core.campaign_by_id(core.load_state(), camp_id)
    print(f"  轮{i+1}: stage={c['stage_index']} iter={c['current_iteration']} "
          f"history={len(c['history'])} status={c['status']} 已选模型={_select_log[-1] if _select_log else '-'}")

final = core.campaign_by_id(core.load_state(), camp_id)
print("\n" + "=" * 56)
print("实测D: 断言与结论")
print("=" * 56)
print("  自动选择的模型序列:", _select_log)
assert "Hy4" in _select_log, "✗ 应当自动选择免费模型 Hy4"
assert final["current_iteration"] >= 4, "✗ 应当推进至少 4 轮"
assert len(final["history"]) >= 4, "✗ 应当有至少 4 条迭代历史"
print("  ✓ 自动选用免费模型:", set(_select_log))
print("  ✓ 迭代推进轮数:", final["current_iteration"], " 历史条数:", len(final["history"]))
print("  ✓ 端到端自动迭代闭环验证通过")
print("  临时目录:", TMP)
