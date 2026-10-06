# -*- coding: utf-8 -*-
"""回归:全天免费模型(倍率=0,如 Hy3 限时免费)在白天也应放行自动迭代。
白天 + fresh 目录含倍率0免费模型 -> 放行;目录缺失/过期/仅收费 -> fail-closed 不放行。"""
import sys, tempfile
from pathlib import Path
from datetime import datetime, timedelta
sys.path.insert(0, ".")
TMP = Path(tempfile.mkdtemp())
import workbuddy_monitor as core
core.STATE_FILE = TMP / "state.json"
core.CONFIG_FILE = TMP / "config.json"
core.LOG_FILE = TMP / "monitor.log"

cfg = core.load_config()
cfg.setdefault("auto_send", {})["free_window"] = {"enabled": True, "start": "23:00", "end": "08:00"}
day = datetime(2026, 10, 6, 10, 30)  # 白天 10:30

DISC_AT = (day - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")

def mk_state(models, discovered_minutes_ago=10):
    ts = (day - timedelta(minutes=discovered_minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
    return {"model_catalog": models,
            "free_info": {"models_discovered_at": ts,
                          "window": {"start": "23:00", "end": "08:00", "source": "config"}}}

HY3 = {"name": "Hy3", "free": True, "multiplier": 0.0, "free_window": None, "free_reason": "倍率为0"}
HY4 = {"name": "Hy4 preview", "free": False, "multiplier": 0.29, "free_window": None, "free_reason": "积分成本"}

ok = True
def check(label, got, want):
    global ok
    mark = "PASS" if got == want else "FAIL"
    if got != want: ok = False
    print(f"  [{mark}] {label}: got={got} want={want}")

print("== 白天迭代门控(全天免费模型) ==")
check("白天+fresh目录含Hy3(倍率0) -> 放行", core.iteration_allowed(cfg, day, mk_state([HY3, HY4])), True)
check("白天+空目录 -> 不放行", core.iteration_allowed(cfg, day, mk_state([])), False)
check("白天+仅收费模型 -> 不放行", core.iteration_allowed(cfg, day, mk_state([HY4])), False)
check("白天+目录过期(stale) -> 不放行", core.iteration_allowed(cfg, day, mk_state([HY3], discovered_minutes_ago=600)), False)
check("白天+无状态 -> 不放行", core.iteration_allowed(cfg, day, None), False)
check("夜间(窗口内)照常放行", core.iteration_allowed(cfg, datetime(2026,10,6,23,30), mk_state([HY4])), True)
print()
print("结果:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
