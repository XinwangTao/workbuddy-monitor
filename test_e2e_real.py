# -*- coding: utf-8 -*-
"""实测层1：无真实 WorkBuddy/CDP 时的真实运行时行为。
使用隔离的临时 state/config，不污染项目 state.json。"""
import sys, tempfile
from pathlib import Path
from datetime import datetime

sys.path.insert(0, ".")
TMP = Path(tempfile.mkdtemp())
import workbuddy_monitor as core
core.STATE_FILE = TMP / "state.json"
core.CONFIG_FILE = TMP / "config.json"
core.LOG_FILE = TMP / "monitor.log"

cfg = core.load_config()
state = core.load_state()

print("=" * 56)
print("实测1: 无 CDP 下的「从底层发现」回退逻辑")
print("=" * 56)
cat = core.discover_free_models(cfg, state, force=True)
print("  discover_free_models(无CDP) ->", cat, "  # 预期 None（连不上 WorkBuddy）")
win = core.discover_free_window(cfg, state, force=True)
print("  discover_free_window        ->", win, "  # 预期回退 23:00-08:00 (source=config)")

print("\n" + "=" * 56)
print("实测2: 免费时段门控判定（核心保护逻辑）")
print("=" * 56)
for label, t in [("白天 14:03", datetime(2026, 9, 23, 14, 3)),
                 ("免费起点 23:00", datetime(2026, 9, 23, 23, 0)),
                 ("免费中段 02:30", datetime(2026, 9, 23, 2, 30)),
                 ("免费结束 08:00", datetime(2026, 9, 23, 8, 0)),
                 ("刚过 08:01", datetime(2026, 9, 23, 8, 1))]:
    w = core.effective_free_window(state)
    print(f"  {label}: in_free_window={core.in_window(w, t)}")

print("\n" + "=" * 56)
print("实测3: 单次监控循环（白天，应只排队不发送）")
print("=" * 56)
# 加一个待发任务，验证白天不会被发出
tid = core.add_task(state, "实测任务：白天不应发送", "", scheduled_time=None, allow_credit=False)
print(f"  已插入待发任务 #{tid}")
core.run_loop(cfg, once=True)
after = core.load_state()
print(f"  循环后磁盘任务数: {len(after.get('tasks', []))}")
for t in after.get("tasks", []):
    print(f"    #{t['id']} status={t['status']} content={t['content'][:20]!r}")
task = next((x for x in after["tasks"] if x["id"] == tid), None)
print(f"  目标任务状态: {task['status'] if task else 'NOT FOUND'}  # 预期 pending（白天被门控拦住）")

print("\n" + "=" * 56)
print("实测4: 状态持久化（free_info 已写入）")
print("=" * 56)
saved = core.load_state()
print("  free_info:", saved.get("free_info"))
print("\n全部实测完成，临时目录:", TMP)
