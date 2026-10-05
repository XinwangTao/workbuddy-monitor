"""一次性补丁：把 log_heartbeat 的管道字段拼装改成一句话文案。

带并发保护——workbuddy_monitor.py 正被后台迭代进程改写，
如果落盘前的 mtime 已经变了就放弃，避免互相覆盖。
"""
import os
import sys
import time

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workbuddy_monitor.py")
START = "def log_heartbeat(state: dict, cfg: dict, now=None):"
END = "# ---------------------------------------------------------------------------\n# 限流消息解析"

NEW = '''def log_heartbeat(state: dict, cfg: dict, now=None):
    """周期性心跳：让"昨晚监控在不在岗、有没有真在推进"一眼可读。

    输出是一句话，不是管道拼字段：先给结论（在跑/待机/被拦/卡住），
    再补证据（窗口、轮次、排队数、距上次动作）。仍保留 `监控心跳` 前缀，
    旧 grep 与回归测试不受影响；活动 id 平时不进日志（噪音），
    只有真卡住时才带上，因为那时你必须知道是哪条活动僵了。"""
    now = now or datetime.now()
    auto = cfg.get("auto_send", {}) or {}
    win = effective_free_window(state, None, cfg)
    in_win = in_window(win, now)
    paused = balance_guard_paused(state)
    active = [c for c in ensure_campaigns(state) if c.get("status") == "running"]
    pend = len(pending_tasks(state))

    # 在途回复等太久 = CDP 断了或回复捕获失败，比"进程还活着"更值得报警
    await_timeout = int(auto.get("await_timeout", 1800) or 1800)
    stuck = None
    last_act = None
    for c in active:
        t = _parse_dt(c.get("awaiting_result_at")) if c.get("awaiting_result") else None
        if t and (last_act is None or t > last_act):
            last_act = t
        if t and (now - t).total_seconds() > await_timeout:
            if stuck is None or t > stuck["t"]:
                stuck = {"t": t, "c": c}

    # 判定顺序即优先级：白天窗口外本来就不该推进，那里报"卡住"是误报，
    # 所以"待机"要在"卡住"之前判。
    if paused:
        tag = "被拦(余额守卫)"
        verdict = "被余额守卫拦住了，不推任务"
    elif not active or not in_win:
        tag = "待机"
        verdict = "待机守夜"
    elif stuck:
        tag = "卡住(在途超时)"
        verdict = "在途回复超等待上限，疑似 CDP 断了"
    else:
        tag = "在跑"
        verdict = "正在推进"

    wl = _window_left(win, now)
    seg_win = f"免费时段{'内' if in_win else '外'} {win.get('start', '?')}-{win.get('end', '?')}，{wl}"

    c = (stuck or {}).get("c") or (active[0] if active else None)
    if c:
        it = int(c.get("current_iteration", 0) or 0)
        if stuck:
            elapsed = _fmt_dur((now - stuck["t"]).total_seconds())
            seg_camp = f"；迭代 #{c['id']} 第{it}轮在途回复已等 {elapsed}（超 {await_timeout}s）"
        else:
            seg_camp = f"；{len(active)} 个迭代活动推进到第 {it} 轮" + (
                "、在等模型回复" if c.get("awaiting_result") else "、正在推进")
    else:
        seg_camp = "；无迭代活动在跑" if not paused else "；迭代活动已全部挂起"

    # 心跳在 ≠ 在干活：把"距上次真实推进多久"摆到明面上
    if last_act is None:
        cand = [_parse_dt(t.get("scheduled_time")) for t in state.get("tasks", []) or [] if t.get("scheduled_time")]
        cand = [d for d in cand if d]
        last_act = max(cand) if cand else None
    seg_last = f"；上次动作 {_ago_text(last_act, now)}" if last_act is not None else ""

    seg_pend = f"；待发 {pend} 条任务" if pend else "；无待发任务"
    log(f"监控心跳·{tag}：{verdict}——{seg_win}{seg_camp}{seg_pend}{seg_last}。")


'''


def main():
    for attempt in range(5):
        st = os.stat(SRC)
        text = open(SRC, encoding="utf-8").read()
        i = text.find(START)
        j = text.find(END)
        if i < 0 or j < 0 or j <= i:
            print("ANCHOR_NOT_FOUND")
            return 1
        new_text = text[:i] + NEW + text[j:]
        open(SRC, "w", encoding="utf-8", newline="").write(new_text)
        print("OKPATCHED", st.st_size, st.st_mtime_ns)
        # 后台迭代可能正在写同一个文件：确认这次写入没被立刻覆盖
        time.sleep(1.5)
        if os.stat(SRC).st_mtime_ns == st.st_mtime_ns:
            print("GUARD_OK")
            return 0
        print("GUARD_RACED, retry")
        time.sleep(2)
    print("GUARD_FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
