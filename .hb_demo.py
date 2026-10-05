# -*- coding: utf-8 -*-
"""把 log_heartbeat 的四种判定各渲染一遍，人工看文案读不读得顺。"""
import sys, tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_TMP = Path(tempfile.mkdtemp(prefix="hbdemo_"))
import workbuddy_monitor as core

core.STATE_FILE = _TMP / "state.json"
core.CONFIG_FILE = _TMP / "config.json"
core.LOG_FILE = _TMP / "hb.log"
core.STATE_FILE.write_text("{}", encoding="utf-8")
core.CONFIG_FILE.write_text("{}", encoding="utf-8")

NOW = datetime(2026, 10, 5, 2, 0, 0)          # 免费时段内（23:00-08:00）
OUT = datetime(2026, 10, 5, 12, 0, 0)         # 窗口外
CFG = {"auto_send": {"free_window": {"start": "23:00", "end": "08:00"},
                     "await_timeout": 1800}}


def camp(i, it, awaiting, at):
    return {"id": i, "status": "running", "current_iteration": it,
            "awaiting_result": awaiting, "awaiting_result_at": at}


def task(i):
    return {"id": i, "content": f"t{i}", "status": "pending", "model": "Free"}


cases = [
    ("在跑（窗口内、活动等回复）", {"campaigns": [camp(1790230528496, 3, True, "2026-10-05 01:56")]},
     [task(1), task(2)], NOW, False),
    ("卡住（在途超过等待上限）", {"campaigns": [camp(1790230528496, 3, True, "2026-10-05 01:18")]},
     [task(1)], NOW, False),
    ("被拦（余额守卫暂停）", {"campaigns": [camp(1790230528496, 3, True, "2026-10-05 01:56")]},
     [], NOW, True),
    ("待机（窗口外）", {"campaigns": []}, [task(1), task(2)], OUT, False),
    ("空转（什么都没有）", {"campaigns": []}, [], NOW, False),
]

for name, camp_state, tasks, now, paused in cases:
    st = {"campaigns": camp_state["campaigns"], "tasks": tasks}
    if paused:
        st[core._BALANCE_STATE_KEY] = {"paused": True, "reason": "余额不足", "time": "2026-10-05 01:00:00"}
    core.log_heartbeat(st, CFG, now)
    print(f"[{name}]")
    print("  " + core.LOG_FILE.read_text(encoding="utf-8-sig").strip().split("\n")[-1])
