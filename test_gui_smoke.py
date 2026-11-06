# -*- coding: utf-8 -*-
"""workbuddy-monitor GUI 构建冒烟测试

验证界面能完整构建、定时框能填充下一免费时段、任务能入队并渲染到表格、
迭代管理窗口能打开且顶部能显示迭代状态。

需要一个带 tkinter 的 Python（受管 Python 3.13 通常没有，用系统 Python 运行）：
    python test_gui_smoke.py
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    import tkinter as tk
    from tkinter import ttk
except Exception as e:  # noqa: BLE001
    print(f"跳过：当前 Python 无 tkinter（{e}）。请用带 tkinter 的 Python 运行。")
    sys.exit(0)

import workbuddy_monitor as core  # noqa: E402

# 隔离状态/配置到临时目录，避免污染仓库
_TMP = Path(tempfile.mkdtemp(prefix="wbmon_gui_"))
core.STATE_FILE = _TMP / "state.json"
core.CONFIG_FILE = _TMP / "config.json"
core.LOG_FILE = _TMP / "monitor.log"

import workbuddy_gui as gui  # noqa: E402

PASS = FAIL = 0
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


print("== GUI 构建冒烟 ==")
try:
    root = tk.Tk()
except Exception as e:  # 无 DISPLAY（CI/headless）时 Tk 初始化失败，按跳过处理
    print(f"跳过：无法创建 Tk 窗口（{e}）")
    sys.exit(0)
root.withdraw()  # 不实际显示窗口

app = gui.App(root)
check("App 构建成功", app is not None)
check("顶部免费信息标签存在", hasattr(app, "lbl_free"))
check("顶部迭代状态标签存在", hasattr(app, "lbl_iter"))
check("定时输入框存在", hasattr(app, "ent_schedule"))

# 整体刷新（限流 / 免费信息 / 迭代状态 / 任务表）
try:
    app.refresh_all()
    ok = True
    err = ""
except Exception as e:  # noqa: BLE001
    ok, err = False, f"{type(e).__name__}: {e}"
check("refresh_all 无异常（含新增的免费/迭代刷新）", ok, err)

# 🌙 填充下一免费时段
app.ent_schedule.delete(0, "end")
app.on_fill_free_window()
filled = app.ent_schedule.get().strip()
check("🌙 填充下一免费时段成功", len(filled) == 19 and filled[4] == "-", f"实际='{filled}'")
try:
    from datetime import datetime
    datetime.strptime(filled, "%Y-%m-%d %H:%M:%S")
    ok = True
except Exception:  # noqa: BLE001
    ok = False
check("填充值是合法时间格式", ok)

# 添加一个定时任务并确认渲染进表格
app.ent_task.delete(0, "end")
app.ent_task.insert(0, "冒烟测试任务")
app.ent_schedule.delete(0, "end")
app.ent_schedule.insert(0, filled)
app.on_add()
st = core.load_state()
check("任务已写入磁盘", any(t["content"] == "冒烟测试任务" for t in st["tasks"]))
check("定时字段随任务保存",
      any(t.get("scheduled_time") == filled for t in st["tasks"]))
check("表格有渲染行", len(app.tree.get_children()) == 1,
      f"实际={len(app.tree.get_children())}")
check("表格含'定时'列", "scheduled" in app.tree["columns"], f"实际={app.tree['columns']}")

# 任务状态变化要能刷新到表格（发送成功/失败一眼可见）
_task_id = st["tasks"][0]["id"]
core.update_task(_task_id, status="sent")
app.state = core.load_state()
app.refresh_tasks(force=True)
_rows = app.tree.get_children()
_status_text = app.tree.item(_rows[0], "values")[4] if _rows else ""
check("任务状态更新后表格显示已发送", "✅已发送" in _status_text, f"实际='{_status_text}'")

# 免费目录可信度必须显示在界面（stale 会被 fail-closed，用户要能看到原因）
from datetime import datetime, timedelta  # noqa: E402

_st_free = core.load_state()
_st_free["model_catalog"] = [{"name": "FreeA", "free": True, "multiplier": 0.0}]
_st_free["free_info"] = {"models_discovered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
core.save_state(_st_free)
app.state = core.load_state()
app.refresh_free()
_free_text = app.lbl_free.cget("text")
check("新鲜目录→显示🟢免费模型且无过期告警",
      "🟢" in _free_text and "FreeA" in _free_text and "过期" not in _free_text,
      f"实际='{_free_text}'")

_st_free["free_info"] = {"models_discovered_at":
                         (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")}
core.save_state(_st_free)
app.state = core.load_state()
app.refresh_free()
check("过期目录→界面明确告警",
      "目录已过期" in app.lbl_free.cget("text"),
      f"实际='{app.lbl_free.cget('text')}'")

# 迭代状态展示
camp = core.create_campaign(core.load_state(), "冒烟迭代目标", strategy="autopilot")
app.state = core.load_state()
try:
    app.refresh_iter()
    ok = True
    err = ""
except Exception as e:  # noqa: BLE001
    ok, err = False, f"{type(e).__name__}: {e}"
check("refresh_iter 无异常", ok, err)
check("迭代状态标签已更新", "迭代" in (app.lbl_iter.cget("text") or ""),
      f"实际='{app.lbl_iter.cget('text')}'")

# 打开迭代管理窗口（构建即验证，不进入事件循环）
try:
    app.on_iter_manage()
    dialogs = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
    ok = len(dialogs) >= 1
    err = f"子窗口数={len(dialogs)}"
    for d in dialogs:
        d.destroy()
except Exception as e:  # noqa: BLE001
    ok, err = False, f"{type(e).__name__}: {e}"
check("迭代管理窗口能打开且构建无异常", ok, err)

# 设置窗口（含新增的发现开关）
try:
    app.on_settings()
    dialogs = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
    ok = len(dialogs) >= 1
    err = f"子窗口数={len(dialogs)}"
    for d in dialogs:
        d.destroy()
except Exception as e:  # noqa: BLE001
    ok, err = False, f"{type(e).__name__}: {e}"
check("设置窗口能打开且构建无异常", ok, err)

root.destroy()

print("\n" + "=" * 56)
print(f"结果：通过 {PASS} / 失败 {FAIL}")
if FAILED:
    print("失败用例：")
    for n in FAILED:
        print(f"  - {n}")
print("=" * 56)
sys.exit(1 if FAIL else 0)
