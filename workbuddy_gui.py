# -*- coding: utf-8 -*-
"""
WorkBuddy 监控 - 可视化任务管理界面（响应式布局版）
运行: pythonw workbuddy_gui.py
"""
import sys
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
import workbuddy_monitor as core


class App:
    DEFAULT_MODELS = ["Hy4 preview", "Hy4", "GPT-5", "Claude Sonnet", "Claude Opus"]

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("WorkBuddy 用量/余额监控 - 任务管理")
        self.root.geometry("780x560")
        self.root.minsize(620, 460)

        self.state = core.load_state()
        self.cfg = core.load_config()
        self.monitoring = False
        self._mon_stop = threading.Event()

        self._build_ui()
        self.refresh_all()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(2000, self._auto_sync)

    # ================================================================ UI
    def _build_ui(self):
        # 根容器：网格 3 行，第 2 行可伸缩
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        self._build_top()      # row 0: 限流状态
        self._build_task_area()  # row 1: 任务列表（可伸缩）
        self._build_bottom()   # row 2: 监控控制

    def _build_top(self):
        top = ttk.LabelFrame(self.root, text="限流状态 / 免费信息")
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=0)

        self.lbl_limit = ttk.Label(top, text="（无）", font=("Microsoft YaHei", 10))
        self.lbl_limit.grid(row=0, column=0, sticky="w", padx=8, pady=4)

        # 限流按钮条：右侧折行排列
        btns = ttk.Frame(top)
        btns.grid(row=0, column=1, sticky="e", padx=8)
        ttk.Button(btns, text="📋 解析消息", command=self.on_parse_msg).grid(row=0, column=0, padx=2)
        ttk.Button(btns, text="🕊 剪贴板", command=self.on_clipboard).grid(row=0, column=1, padx=2)
        ttk.Button(btns, text="🗑 清除限流", command=self.on_clear_limit).grid(row=0, column=2, padx=2)

        # 已发现的免费模型 / 免费时段
        self.lbl_free = ttk.Label(top, text="", font=("Microsoft YaHei", 9), foreground="#555555")
        self.lbl_free.grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 4))

        # 自动迭代活动状态
        self.lbl_iter = ttk.Label(top, text="", font=("Microsoft YaHei", 9), foreground="#2a7")
        self.lbl_iter.grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

    def _build_task_area(self):
        mid = ttk.LabelFrame(self.root, text="待办任务列表（双击编辑）")
        mid.grid(row=1, column=0, sticky="nsew", padx=10, pady=5)
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)

        # 任务表格
        tree_frame = ttk.Frame(mid)
        tree_frame.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=6)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        cols = ("model", "content", "created", "scheduled", "status")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("model", text="模型")
        self.tree.heading("content", text="任务内容")
        self.tree.heading("created", text="创建时间")
        self.tree.heading("scheduled", text="定时发送")
        self.tree.heading("status", text="状态")
        self.tree.column("model", width=110, anchor="w", stretch=True)
        self.tree.column("content", width=260, anchor="w", stretch=True)
        self.tree.column("created", width=130, anchor="center", stretch=False)
        self.tree.column("scheduled", width=130, anchor="center", stretch=False)
        self.tree.column("status", width=80, anchor="center", stretch=False)
        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns", pady=6)
        self.tree.tag_configure("sent", foreground="gray")
        self.tree.tag_configure("failed", foreground="red")
        self.tree.tag_configure("pending", foreground="black")
        self.tree.bind("<Double-1>", lambda e: self.on_edit())

        # 任务输入行：输入框随窗口伸缩
        inbar = ttk.Frame(mid)
        inbar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
        inbar.columnconfigure(1, weight=1)

        ttk.Label(inbar, text="任务:").grid(row=0, column=0, padx=(0, 4))
        self.ent_task = ttk.Entry(inbar)
        self.ent_task.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.ent_task.bind("<Return>", lambda e: self.on_add())

        ttk.Label(inbar, text="模型:").grid(row=0, column=2, padx=(0, 4))
        self.ent_model = ttk.Combobox(inbar, width=14, values=self.model_choices())
        self.ent_model.grid(row=0, column=3, padx=(0, 8))

        ttk.Label(inbar, text="定时:").grid(row=0, column=4, padx=(0, 4))
        self.ent_schedule = ttk.Entry(inbar, width=15)
        self.ent_schedule.grid(row=0, column=5, padx=(0, 4))
        self.ent_schedule.insert(0, "")
        ttk.Button(inbar, text="🌙", width=3, command=self.on_fill_free_window).grid(row=0, column=6, padx=(0, 8))

        # 操作按钮：单独一行，按宽度自适应折行
        self.act_frame = ttk.Frame(inbar)
        self.act_frame.grid(row=1, column=0, columnspan=7, sticky="e", pady=(4, 0))
        self._act_buttons = [
            ("✏ 编辑", self.on_edit),
            ("👁 查看全文", self.on_view),
            ("🔁 重新入队", self.on_requeue),
            ("✕ 删除", self.on_delete),
        ]
        self._layout_act_buttons()
        self.act_frame.bind("<Configure>", lambda e: self._layout_act_buttons())

    def _layout_act_buttons(self):
        """根据输入行宽度决定操作按钮一行放几个（自适应折行）"""
        w = self.act_frame.winfo_width()
        if w < 10:
            return  # 首次布局前跳过
        per_row = 4 if w > 420 else (3 if w > 320 else 2)
        for i, (text, cmd) in enumerate(self._act_buttons):
            btn = getattr(self, f"_btn{i}", None)
            if btn is None:
                btn = ttk.Button(self.act_frame, text=text, command=cmd)
                setattr(self, f"_btn{i}", btn)
            btn.grid(row=i // per_row, column=i % per_row, padx=2, pady=2, sticky="ew")

    def _build_bottom(self):
        bot = ttk.LabelFrame(self.root, text="监控控制")
        bot.grid(row=2, column=0, sticky="ew", padx=10, pady=(5, 10))
        bot.columnconfigure(2, weight=1)

        self.btn_toggle = ttk.Button(bot, text="▶ 启动监控", command=self.on_toggle_monitor)
        self.btn_toggle.grid(row=0, column=0, padx=8, pady=6)
        self.lbl_mon = ttk.Label(bot, text="⏸ 未运行", foreground="red")
        self.lbl_mon.grid(row=0, column=1, padx=4)
        self.lbl_guard = ttk.Label(bot, text="", foreground="red",
                                   font=("Microsoft YaHei", 9, "bold"))
        self.lbl_guard.grid(row=0, column=2, padx=4)
        ttk.Button(bot, text="⚙ 设置", command=self.on_settings).grid(row=0, column=3, padx=2)
        ttk.Button(bot, text="💰 查看余额", command=self.on_balance).grid(row=0, column=4, padx=2)
        ttk.Button(bot, text="🔍 发现免费", command=self.on_discover).grid(row=0, column=5, padx=2)
        ttk.Button(bot, text="🔁 迭代", command=self.on_iter_manage).grid(row=0, column=6, padx=2)
        ttk.Button(bot, text="使用说明", command=self.on_help).grid(row=0, column=7, padx=2)

    # ========================================================= 模型选项
    def model_choices(self):
        got = list(self.DEFAULT_MODELS)
        rl = self.state.get("rate_limits", {})
        if isinstance(rl, dict):
            for v in rl.values():
                m = v.get("model") if isinstance(v, dict) else None
                if m and m not in got:
                    got.append(m)
        for t in self.state.get("tasks", []):
            m = t.get("model")
            if m and m not in got:
                got.append(m)
        return got

    # ========================================================= 刷新显示
    def refresh_all(self):
        self.refresh_limit()
        self.refresh_free()
        self.refresh_iter()
        self.refresh_tasks()

    def refresh_limit(self):
        rl = self.state.get("rate_limits", {})
        if not isinstance(rl, dict) or not rl:
            self.lbl_limit.config(text="当前无限流，所有模型可用", foreground="green")
            return
        lines = []
        for k, v in rl.items():
            reset = v.get("reset", "?")
            left = ""
            try:
                dt = datetime.strptime(reset, "%Y-%m-%d %H:%M:%S")
                sec = (dt - datetime.now()).total_seconds()
                left = f"（约 {int(sec//3600)}小时{int(sec%3600//60)}分后恢复）" if sec > 0 else "（已可恢复）"
            except Exception:
                pass
            lines.append(f"⛔ {v.get('model', k)}  重置于 {reset}  {left}")
        self.lbl_limit.config(text="\n".join(lines), foreground="orange")

    def refresh_free(self):
        state = self.state
        cat = state.get("model_catalog") or []
        fi = state.get("free_info", {}).get("window")
        if fi:
            win = f"{fi.get('start')}-{fi.get('end')}（来源:{fi.get('source','?')}）"
        else:
            win = "23:00-08:00（默认/政策）"
        free_models = [c["name"] for c in cat if c.get("free")]
        if free_models:
            self.lbl_free.config(
                text=f"🟢 免费模型: {', '.join(free_models)}   |   免费时段: {win}")
        else:
            self.lbl_free.config(
                text=f"免费时段: {win}（未识别免费模型，启动监控/点「发现免费」后可自动发现）")

    def refresh_iter(self):
        camps = core.ensure_campaigns(self.state)
        active = [c for c in camps if c.get("status") == "running"]
        if not camps:
            self.lbl_iter.config(text="")
            return
        if active:
            parts = []
            for c in active:
                mx = "∞" if not c.get("max_iterations") else c["max_iterations"]
                parts.append(f"#{c['id']} {c.get('current_iteration')}/{mx}轮")
            self.lbl_iter.config(text="🔁 自动迭代进行中: " + "  ".join(parts),
                                 foreground="#2a7")
        else:
            done = sum(1 for c in camps if c.get("status") == "done")
            self.lbl_iter.config(
                text=f"自动迭代: 共 {len(camps)} 个活动（{done} 个已完成）",
                foreground="#888")

    def refresh_tasks(self):
        self.tree.delete(*self.tree.get_children())
        for t in self.state.get("tasks", []):
            st = t.get("status", "pending")
            tag = {"sent": "sent", "failed": "failed"}.get(st, "pending")
            mark = {"sent": "✅已发送", "failed": "❌发送失败"}.get(st, "⏳等待中")
            if st == "failed" and t.get("last_error"):
                mark += f" ({t['last_error'][:20]})"
            self.tree.insert("", "end", iid=str(t["id"]),
                             values=(t.get("model") or "-", t["content"], t.get("created", ""),
                                     t.get("scheduled_time") or "-", mark),
                             tags=(tag,))

    def _save(self):
        core.save_state(self.state)

    def _auto_sync(self):
        """UI 主线程定时从磁盘同步状态，界面永远显示最新数据"""
        try:
            self.state = core.load_state()
            self.refresh_all()
            if core.balance_guard_paused(self.state):
                self.lbl_guard.config(text="⛔ 余额守卫：自动发送已暂停", foreground="red")
            elif not core.in_free_window(self.cfg, self.state):
                win = core.effective_free_window(self.state)
                nxt = core.next_free_window(self.cfg, self.state)
                nxt_s = nxt.strftime("%H:%M") if nxt else win.get("start", "23:00")
                self.lbl_guard.config(text=f"⏳ 等待免费时段 {win['start']}-{win['end']}（{nxt_s} 后开始）",
                                      foreground="#B8860B")
            else:
                self.lbl_guard.config(text="🟢 免费时段中", foreground="green")
        except Exception:
            pass
        self.root.after(2000, self._auto_sync)

    # ============================================================ 操作
    def on_add(self):
        content = self.ent_task.get().strip()
        if not content:
            messagebox.showwarning("提示", "请输入任务内容")
            return
        sched = self.ent_schedule.get().strip() or None
        core.add_task(self.state, content, self.ent_model.get().strip(),
                      scheduled_time=sched)
        self.ent_task.delete(0, "end")
        self.ent_schedule.delete(0, "end")
        self.ent_model["values"] = self.model_choices()
        self.refresh_tasks()

    def on_fill_free_window(self):
        nxt = core.next_free_window(self.cfg, self.state)
        if nxt:
            self.ent_schedule.delete(0, "end")
            self.ent_schedule.insert(0, nxt.strftime("%Y-%m-%d %H:%M:%S"))

    def _selected_task(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择任务")
            return None
        tid = int(sel[0])
        return next((t for t in self.state.get("tasks", []) if t["id"] == tid), None)

    def on_delete(self):
        task = self._selected_task()
        if not task:
            return
        self.state["tasks"] = [t for t in self.state.get("tasks", []) if t["id"] != task["id"]]
        self._save()
        self.refresh_tasks()

    def on_requeue(self):
        task = self._selected_task()
        if not task:
            return
        # 从磁盘最新状态合并写回，并清空失败计数，让自动发送可以再次尝试
        fresh = core.load_state()
        for t in fresh.get("tasks", []):
            if t.get("id") == task["id"]:
                t["status"] = "pending"
                t["attempts"] = 0
                t.pop("last_error", None)
                break
        core.save_state(fresh)
        self.state = fresh
        self.refresh_tasks()

    def on_edit(self):
        task = self._selected_task()
        if not task:
            return
        dlg = tk.Toplevel(self.root)
        dlg.title(f"编辑任务 #{task['id']}")
        dlg.geometry("480x370")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.columnconfigure(0, weight=1)
        dlg.rowconfigure(1, weight=1)

        ttk.Label(dlg, text="任务内容:").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 0))
        txt = tk.Text(dlg, font=("Microsoft YaHei", 10))
        txt.grid(row=1, column=0, sticky="nsew", padx=10, pady=4)
        txt.insert("1.0", task["content"])

        fr = ttk.Frame(dlg)
        fr.grid(row=2, column=0, sticky="ew", padx=10, pady=4)
        ttk.Label(fr, text="模型:").pack(side="left")
        cmb = ttk.Combobox(fr, width=16, values=self.model_choices())
        cmb.pack(side="left", padx=4)
        cmb.set(task.get("model") or "")
        ttk.Label(fr, text="定时:").pack(side="left", padx=(8, 2))
        ent_sched = ttk.Entry(fr, width=17)
        ent_sched.pack(side="left", padx=2)
        ent_sched.insert(0, task.get("scheduled_time") or "")
        var_credit = tk.BooleanVar(value=bool(task.get("allow_credit")))
        ttk.Checkbutton(fr, text="允许消耗积分", variable=var_credit).pack(side="left", padx=(8, 2))

        btns = ttk.Frame(dlg)
        btns.grid(row=3, column=0, pady=(4, 10))

        def save():
            c = txt.get("1.0", "end").strip()
            if not c:
                messagebox.showwarning("提示", "内容不能为空", parent=dlg)
                return
            sched = ent_sched.get().strip()
            if sched:
                try:
                    datetime.strptime(sched, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    messagebox.showwarning("提示", "定时格式应为 YYYY-MM-DD HH:MM:SS", parent=dlg)
                    return
            task["content"] = c
            task["model"] = cmb.get().strip()
            task["scheduled_time"] = sched or None
            task["allow_credit"] = var_credit.get()
            self._save()
            self.refresh_tasks()
            dlg.destroy()

        ttk.Button(btns, text="💾 保存", command=save).pack(side="left", padx=4)
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="left", padx=4)
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    def on_view(self):
        task = self._selected_task()
        if not task:
            return
        dlg = tk.Toplevel(self.root)
        dlg.title(f"任务详情 #{task['id']}")
        dlg.geometry("480x340")
        info = (f"编号: {task['id']}\n"
                f"模型: {task.get('model') or '-'}\n"
                f"创建时间: {task.get('created', '')}\n"
                f"定时发送: {task.get('scheduled_time') or '（立即/等免费时段）'}\n"
                f"允许消耗积分: {'是' if task.get('allow_credit') else '否'}\n"
                f"状态: {task['status']}\n\n"
                f"────── 任务内容 ──────\n{task['content']}")
        txt = tk.Text(dlg, wrap="word", font=("Microsoft YaHei", 10))
        txt.pack(fill="both", expand=True, padx=10, pady=10)
        txt.insert("1.0", info)
        txt.config(state="disabled")

    def on_parse_msg(self):
        msg = simpledialog.askstring("解析限流消息", "粘贴 WorkBuddy 限流消息：", parent=self.root)
        self._apply_limit_text(msg)

    def on_clipboard(self):
        try:
            self._apply_limit_text(self.root.clipboard_get())
        except Exception:
            messagebox.showwarning("提示", "剪贴板为空")

    def _apply_limit_text(self, msg):
        if not msg:
            return
        model, reset, ok = core.parse_rate_limit_message(msg)
        if not ok:
            messagebox.showwarning("解析失败", "未能从消息中识别模型/时间")
            return
        key = model or "default"
        entry = {"model": model or key}
        if reset:
            entry["reset"] = reset.strftime("%Y-%m-%d %H:%M:%S")
        entry["raw"] = msg.strip()[:300]
        self.state.setdefault("rate_limits", {})[key] = entry
        self._save()
        self.refresh_all()
        self.ent_model["values"] = self.model_choices()
        messagebox.showinfo("解析成功", f"模型: {model}\n重置时间: {reset}")

    def on_clear_limit(self):
        self.state["rate_limits"] = {}
        self._save()
        self.refresh_limit()

    def on_balance(self):
        # 优先走 WorkBuddy 官方 API（CDP token），带套餐明细
        try:
            import workbuddy_cdp
            info = workbuddy_cdp.read_credits()
        except Exception:
            info = None
        if info:
            lines = [f"剩余积分: {info['left']} / {info['total']}",
                     f"已用: {info['used']}    用户类型: {'付费' if info['is_paid'] else '免费'}",
                     ""]
            for p in info.get("packages", []):
                lines.append(f"· {p['code'][:24]}…  {p['remain']} / {p['total']} {p['unit']}")
            try:
                cur = workbuddy_cdp.current_model()
                if cur:
                    lines.append("")
                    lines.append(f"当前模型: {cur}")
            except Exception:
                pass
            messagebox.showinfo("余额查询（官方 API）", "\n".join(lines))
            return
        bal, usage = core.balance_check(self.cfg)
        if bal is None:
            messagebox.showinfo("余额查询",
                "无法通过官方 API 读取（WorkBuddy 未运行或 token 失效）。\n\n"
                "请先启动 WorkBuddy（带 CDP 参数），或编辑 config.json 填写 api.url")
        else:
            messagebox.showinfo("余额查询", f"余额: {bal}\n用量: {usage}")

    def on_discover(self):
        """从 WorkBuddy 底层（模型选择器 / 页面 DOM）发现免费模型与免费时段"""
        try:
            cat = core.discover_free_models(self.cfg, self.state, force=True)
            win = core.discover_free_window(self.cfg, self.state, force=True)
            self.refresh_free()
            if cat is None:
                messagebox.showinfo("发现免费模型",
                    "无法从 WorkBuddy 底层读取模型列表。\n"
                    "请确认 WorkBuddy 已启动且开启 CDP（默认端口 9222）。\n\n"
                    f"免费时段将使用配置/策略默认: {win['start']}-{win['end']}")
            else:
                names = ", ".join(c["name"] for c in cat if c.get("free")) or "（无）"
                messagebox.showinfo("发现免费模型",
                    f"共发现 {len(cat)} 个模型，免费: {names}\n"
                    f"免费时段: {win['start']}-{win['end']}（来源: {win.get('source')}）")
        except Exception as e:
            messagebox.showerror("发现失败", str(e))

    def on_settings(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("设置")
        dlg.geometry("520x460")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.columnconfigure(0, weight=1)

        auto = self.cfg.get("auto_send", {})
        fw = auto.get("free_window", {})
        v1 = tk.BooleanVar(value=auto.get("enabled", True))
        v2 = tk.BooleanVar(value=auto.get("free_only", True))
        v3 = tk.BooleanVar(value=auto.get("balance_guard", True))
        v4 = tk.BooleanVar(value=fw.get("enabled", True))

        ttk.Checkbutton(dlg, text="到点自动发送到 WorkBuddy 窗口",
                        variable=v1).grid(row=0, column=0, sticky="w", padx=14, pady=(14, 4))
        ttk.Checkbutton(dlg, text="只用免费额度（限流重置后才发送）",
                        variable=v2).grid(row=1, column=0, sticky="w", padx=14, pady=4)
        ttk.Checkbutton(dlg, text="积分余额守卫（发送后余额减少立即停止+弹窗报警）",
                        variable=v3).grid(row=2, column=0, sticky="w", padx=14, pady=4)
        ttk.Checkbutton(dlg, text="只在免费时段自动发送（不消耗积分，时段可自动发现）",
                        variable=v4).grid(row=3, column=0, sticky="w", padx=14, pady=4)
        v5 = tk.BooleanVar(value=self.cfg.get("api_balance", True))
        ttk.Checkbutton(dlg, text="用官方 API 读取精确积分余额（通过 WorkBuddy 句柄，默认启用）",
                        variable=v5).grid(row=4, column=0, sticky="w", padx=14, pady=4)
        v6 = tk.BooleanVar(value=auto.get("discover_free_models", True))
        ttk.Checkbutton(dlg, text="🔍 自动发现免费模型（从模型选择器徽标判断，推荐）",
                        variable=v6).grid(row=5, column=0, sticky="w", padx=14, pady=4)
        v7 = tk.BooleanVar(value=auto.get("discover_free_window", True))
        ttk.Checkbutton(dlg, text="🌙 自动发现免费时段（从 WorkBuddy 页面 DOM 读取，回退 23:00-08:00）",
                        variable=v7).grid(row=6, column=0, sticky="w", padx=14, pady=4)

        # 守卫暂停状态 + 恢复按钮
        gs = ttk.LabelFrame(dlg, text="余额守卫状态")
        gs.grid(row=7, column=0, sticky="ew", padx=14, pady=(10, 4))
        gs.columnconfigure(0, weight=1)
        paused = core.balance_guard_paused(self.state)
        info = "⛔ 已暂停: " + core.load_state().get("balance_guard", {}).get("reason", "") \
            if paused else "✅ 正常（未检测到积分消耗）"
        lbl = ttk.Label(gs, text=info, foreground="red" if paused else "green", wraplength=380)
        lbl.grid(row=0, column=0, sticky="w", padx=8, pady=6)

        def resume():
            core.set_balance_guard_paused(None, False, "")
            lbl.config(text="✅ 正常（未检测到积分消耗）", foreground="green")

        ttk.Button(gs, text="解除暂停（确认安全后）", command=resume).grid(row=1, column=0, pady=(0, 6))

        def save():
            self.cfg.setdefault("auto_send", {}).update({
                "enabled": v1.get(), "free_only": v2.get(), "balance_guard": v3.get(),
                "free_window": {"enabled": v4.get(), "start": "23:00", "end": "08:00"},
                "discover_free_models": v6.get(), "discover_free_window": v7.get()})
            self.cfg["api_balance"] = v5.get()
            core.save_config(self.cfg)
            dlg.destroy()

        ttk.Button(dlg, text="💾 保存", command=save).grid(row=8, column=0, pady=10)

    def on_help(self):
        messagebox.showinfo(
            "使用说明",
            "1. 添加任务：底部输入内容 + 选择模型 → 回车或点添加\n"
            "2. 定时发送：在「定时」框填 YYYY-MM-DD HH:MM:SS，或点 🌙 填下一免费时段；"
            "留空=立即入队，到免费时段/限流重置自动发\n"
            "3. 免费模型/时段：点「🔍 发现免费」从 WorkBuddy 底层读取（模型选择器徽标 + 页面 DOM），"
            "读不到则回退 23:00-08:00（官方夜间闲时免费口径）\n"
            "4. 编辑任务：双击行，或选中后点「编辑」（可改定时、勾选允许消耗积分）\n"
            "5. 启动监控：免费模型的任务会在免费时段自动发送；到点任务到时发送\n"
            "6. 限流时：复制 WorkBuddy 的限流消息 → 点「剪贴板」自动解析重置时间")

    def on_iter_manage(self):
        """自动迭代管理窗口：创建活动、查看进度、暂停/恢复/停止。"""
        dlg = tk.Toplevel(self.root)
        dlg.title("自动迭代管理")
        dlg.geometry("600x540")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.columnconfigure(0, weight=1)
        dlg.rowconfigure(2, weight=1)

        # --- 新建活动 ---
        nf = ttk.LabelFrame(dlg, text="新建迭代活动")
        nf.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        nf.columnconfigure(1, weight=1)
        ttk.Label(nf, text="目标/初始提示词:").grid(row=0, column=0, sticky="nw", padx=8, pady=(6, 0))
        txt_goal = tk.Text(nf, height=3, font=("Microsoft YaHei", 10))
        txt_goal.grid(row=0, column=1, sticky="ew", padx=8, pady=6)

        opt = ttk.Frame(nf)
        opt.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 6))
        ttk.Label(opt, text="策略:").grid(row=0, column=0, padx=(0, 2))
        cmb_strat = ttk.Combobox(opt, width=12, values=list(core.ITER_STRATEGIES), state="readonly")
        cmb_strat.set("improve")
        cmb_strat.grid(row=0, column=1, padx=4)
        ttk.Label(opt, text="最大轮数(0=∞):").grid(row=0, column=2, padx=(8, 2))
        ent_max = ttk.Entry(opt, width=6)
        ent_max.insert(0, "0")
        ent_max.grid(row=0, column=3, padx=4)
        ttk.Label(opt, text="模型(空=自动):").grid(row=0, column=4, padx=(8, 2))
        ent_model = ttk.Entry(opt, width=14)
        ent_model.grid(row=0, column=5, padx=4)

        ttk.Label(nf, text="阶段(仅staged,逗号分隔):").grid(row=2, column=0, sticky="w", padx=8, pady=(0, 4))
        ent_stages = ttk.Entry(nf)
        ent_stages.grid(row=2, column=1, sticky="ew", padx=8, pady=(0, 6))

        ttk.Label(nf, text="代码目录(自迭代):").grid(row=3, column=0, sticky="w", padx=8, pady=(0, 4))
        ent_repo = ttk.Entry(nf)
        ent_repo.grid(row=3, column=1, sticky="ew", padx=8, pady=(0, 6))
        ttk.Label(nf, text="验证命令(可选):").grid(row=4, column=0, sticky="w", padx=8, pady=(0, 4))
        ent_test = ttk.Entry(nf)
        ent_test.grid(row=4, column=1, sticky="ew", padx=8, pady=(0, 6))

        ttk.Button(nf, text="▶ 开始迭代", command=lambda: do_start()).grid(row=5, column=1, sticky="e", padx=8, pady=(0, 8))

        # --- 活动列表 ---
        lf = ttk.LabelFrame(dlg, text="迭代活动")
        lf.grid(row=1, column=0, sticky="nsew", padx=10, pady=6)
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)
        cols = ("id", "status", "iter", "strategy", "goal")
        tree = ttk.Treeview(lf, columns=cols, show="headings", height=6)
        tree.heading("id", text="ID")
        tree.heading("status", text="状态")
        tree.heading("iter", text="轮次")
        tree.heading("strategy", text="策略")
        tree.heading("goal", text="目标")
        tree.column("id", width=70)
        tree.column("status", width=70)
        tree.column("iter", width=70)
        tree.column("strategy", width=80)
        tree.column("goal", width=230)
        tree.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        sb = ttk.Scrollbar(lf, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns", pady=6)

        # 详情 + 操作
        det = ttk.Frame(dlg)
        det.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 6))
        det.rowconfigure(0, weight=1)
        det.columnconfigure(0, weight=1)
        txt_det = tk.Text(det, height=8, wrap="word", font=("Microsoft YaHei", 9))
        txt_det.grid(row=0, column=0, sticky="nsew")
        txt_det.config(state="disabled")
        act = ttk.Frame(det)
        act.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(act, text="⏸ 暂停", command=lambda: do_set("paused")).pack(side="left", padx=3)
        ttk.Button(act, text="▶ 恢复", command=lambda: do_set("running")).pack(side="left", padx=3)
        ttk.Button(act, text="⏹ 停止", command=lambda: do_set("done")).pack(side="left", padx=3)

        def selected_id():
            sel = tree.selection()
            if not sel:
                return None
            return int(tree.item(sel[0], "values")[0])

        def do_set(status):
            cid = selected_id()
            if cid is None:
                messagebox.showinfo("提示", "请先选择活动", parent=dlg)
                return
            core.set_campaign_status(self.state, cid, status)
            self.refresh_iter()
            refresh_list()

        def do_start():
            goal = txt_goal.get("1.0", "end").strip()
            if not goal:
                messagebox.showwarning("提示", "请输入迭代目标", parent=dlg)
                return
            try:
                mx = int(ent_max.get().strip() or 0)
            except ValueError:
                mx = 0
            stages = [s.strip() for s in ent_stages.get().split(",") if s.strip()] or None
            core.create_campaign(self.state, goal, strategy=cmb_strat.get(),
                                 model=ent_model.get().strip(), stages=stages,
                                 max_iterations=mx,
                                 repo_path=ent_repo.get().strip(),
                                 test_cmd=ent_test.get().strip())
            txt_goal.delete("1.0", "end")
            refresh_list()
            self.refresh_iter()
            messagebox.showinfo("已创建",
                "迭代活动已创建。启动监控后将在免费时段内自动迭代。", parent=dlg)

        def refresh_list():
            tree.delete(*tree.get_children())
            for c in core.ensure_campaigns(self.state):
                mx = "∞" if not c.get("max_iterations") else c["max_iterations"]
                tree.insert("", "end", values=(c["id"], c.get("status"),
                            f"{c.get('current_iteration')}/{mx}", c.get("strategy"),
                            (c.get("goal") or "")[:28]))

        def on_select(_=None):
            cid = selected_id()
            if cid is None:
                return
            c = core.campaign_by_id(self.state, cid)
            if not c:
                return
            mx = "∞" if not c.get("max_iterations") else c["max_iterations"]
            lines = [
                f"活动 #{c['id']}  状态: {c.get('status')}  策略: {c.get('strategy')}",
                f"轮次: {c.get('current_iteration')}/{mx}  连续失败: {c.get('error_count', 0)}",
                f"目标: {c.get('goal', '')}",
                f"最近提示词: {c.get('last_prompt', '')[:200]}",
                f"最近产出(节选): {core._truncate(c.get('last_result', ''), 300)}",
                f"历史记录数: {len(c.get('history', []))}",
            ]
            txt_det.config(state="normal")
            txt_det.delete("1.0", "end")
            txt_det.insert("1.0", "\n".join(lines))
            txt_det.config(state="disabled")

        tree.bind("<<TreeviewSelect>>", on_select)
        refresh_list()

        def tick():
            if not dlg.winfo_exists():
                return
            # 重新从磁盘同步，反映监控线程的进展
            self.state = core.load_state()
            refresh_list()
            on_select()
            self.refresh_iter()
            dlg.after(3000, tick)

        dlg.after(3000, tick)

    # ======================================================== 后台监控
    def on_toggle_monitor(self):
        if self.monitoring:
            self._mon_stop.set()
            self.monitoring = False
            self.btn_toggle.config(text="▶ 启动监控")
            self.lbl_mon.config(text="⏸ 已停止", foreground="red")
        else:
            self._mon_stop = threading.Event()
            self.monitoring = True
            threading.Thread(target=self._monitor_loop, daemon=True).start()
            self.btn_toggle.config(text="⏹ 停止监控")
            self.lbl_mon.config(text="🟢 监控中…", foreground="green")

    def _monitor_loop(self):
        try:
            interval = max(5, int(float(self.cfg.get("poll_interval", 30))))
        except (TypeError, ValueError):
            interval = 30
        # 启动时从 WorkBuddy 底层发现免费模型/时段（失败仅记录）
        try:
            core.discover_free_models(self.cfg, self.state)
            core.discover_free_window(self.cfg, self.state)
        except Exception as e:
            core.log(f"初始免费发现失败(可忽略): {e}")
        while not self._mon_stop.is_set():
            try:
                # 每轮从磁盘读最新状态，避免与 UI/CLI 写入冲突
                state = core.load_state()
                self.state = state
                # 余额守卫暂停中：跳过自动发送，但仍更新限流检测
                if core.balance_guard_paused(state):
                    self._mon_stop.wait(interval)
                    continue
                core.check_rate_limits(state, self.cfg)
                now = datetime.now()
                # 统一调度：遍历所有 pending 任务，到达发送条件即发送。
                # 条件：① 限流重置已到 ② 免费模型在免费时段 ③ 用户指定时间已到
                for t in core.pending_tasks(state):
                    if core.task_sendable_now(t, state, self.cfg, now):
                        # 双重保险：发送前从磁盘确认仍是 pending（可能已被其他进程发送）
                        fresh = core.load_state()
                        still = next((x for x in fresh.get("tasks", [])
                                      if x.get("id") == t.get("id") and x.get("status") == "pending"), None)
                        if still:
                            core.send_task(t, self.cfg)
                # 自动迭代：仅在免费时段内推进
                if core.in_free_window(self.cfg, datetime.now(), state):
                    core.run_iteration_step(state, self.cfg, datetime.now())
            except Exception as e:
                core.log(f"监控线程错误: {e}")
            self._mon_stop.wait(interval)

    def _on_close(self):
        self._mon_stop.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
