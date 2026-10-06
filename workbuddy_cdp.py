# -*- coding: utf-8 -*-
"""workbuddy_cdp.py — 通过 Chrome DevTools Protocol (CDP) 与 WorkBuddy(Electron) 通信

提供三类能力（均为只读或 UI 等价操作，不消耗积分）：
1. read_credits()        — 直接调用 WorkBuddy 服务端余额 API，精确读取剩余积分
2. list_models()         — 枚举模型选择器中的全部模型（名称/免费标记/倍率）
3. select_model(name)    — 通过 UI DOM 等价点击切换模型

Token 获取策略：
- WorkBuddy 的 Authorization: Bearer JWT 由主进程注入，页面 JS 无法直接读取。
- 通过 CDP Network.requestWillBeSent 被动捕获应用自身发出的认证请求头；
- 若短时间内未捕获到，可用 Page.reload 触发一次页面刷新（应用启动即请求）。
- JWT 有效期约 7 天，捕获后缓存到本地文件跨会话复用。
"""
import json
import os
import re
import threading
import time
import urllib.request

try:
    import websocket  # websocket-client
except ImportError:
    websocket = None

# 优先使用 WORKBUDDY_CDP_HOST；未设置时自动探测常用端口。
CDP_HOST = os.environ.get("WORKBUDDY_CDP_HOST", "").rstrip("/")
CDP_PORTS = (9224, 9223, 9222)
BILLING_URL = "https://copilot.tencent.com/billing/meter/get-user-resource-summary"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
_TOKEN_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".wb_token_cache")


# --------------------------------------------------------------------------- CDP 基础
def _http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _find_page_target():
    """自动探测 WorkBuddy CDP 端口并返回主页面 target；找不到返回 None。"""
    global CDP_HOST
    hosts = [CDP_HOST] if CDP_HOST else [f"http://127.0.0.1:{p}" for p in CDP_PORTS]
    for host in hosts:
        try:
            for target in _http_json(host + "/json"):
                if (target.get("type") == "page" and
                        "workbuddy" in (target.get("url", "") + target.get("title", "")).lower()):
                    CDP_HOST = host  # 后续 billing/token 请求复用已发现的端口
                    return target
        except Exception:
            continue
    return None


class _Cdp:
    """一个极简的 CDP WebSocket 会话封装"""

    def __init__(self, target):
        if websocket is None:
            raise RuntimeError("需要 websocket-client 库（pip install websocket-client）")
        # 新版 Electron 校验 Host/Origin，必须 suppress_origin
        self.ws = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=15, suppress_origin=True)
        self._id = 0
        self._lock = threading.Lock()

    def call(self, method, params=None, timeout=15):
        with self._lock:
            self._id += 1
            mid = self._id
            self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            deadline = time.time() + timeout
            while time.time() < deadline:
                m = json.loads(self.ws.recv())
                if m.get("id") == mid:
                    if "error" in m:
                        raise RuntimeError(f"CDP {method}: {m['error']}")
                    return m.get("result", {})
            raise TimeoutError(f"CDP {method} 超时")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _evaluate(js, timeout=15):
    """在 WorkBuddy 页面里执行 JS，返回结果 value（出错返回 None）"""
    t = _find_page_target()
    if not t:
        return None
    with _Cdp(t) as c:
        r = c.call("Runtime.evaluate",
                   {"expression": js, "returnByValue": True, "awaitPromise": True},
                   timeout=timeout)
        return r.get("result", {}).get("value")


# --------------------------------------------------------------------------- token 捕获
def capture_token(refresh_if_needed=True, wait_seconds=25):
    """被动监听 WorkBuddy 自身流量，捕获 Authorization 头。
    返回 token 字符串或 None。"""
    t = _find_page_target()
    if not t:
        return None
    token_box = []

    with _Cdp(t) as c:
        c.call("Network.enable")
        c.ws.settimeout(1.0)
        deadline = time.time() + wait_seconds
        if refresh_if_needed:
            try:
                c.call("Page.enable")
                c.call("Page.reload", {"ignoreCache": False})
            except Exception:
                pass
        while time.time() < deadline:
            try:
                m = json.loads(c.ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break
            if m.get("method") == "Network.requestWillBeSent":
                hdrs = m.get("params", {}).get("request", {}).get("headers", {})
                auth = hdrs.get("Authorization") or hdrs.get("authorization")
                if auth and auth.startswith("Bearer ") and len(auth) > 30:
                    token_box.append(auth)
                    break
    if token_box:
        _save_token_cache(token_box[0])
        return token_box[0]
    return None


def _save_token_cache(token):
    try:
        from tempfile import NamedTemporaryFile
        d = os.path.dirname(_TOKEN_CACHE)
        with NamedTemporaryFile("w", dir=d, prefix=".wb_token_", delete=False) as f:
            f.write(token)
        os.replace(f.name, _TOKEN_CACHE)
    except Exception:
        pass


def _load_token_cache():
    try:
        with open(_TOKEN_CACHE, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


def _token_valid(token):
    """验证缓存的 token 是否仍有效（调用一次轻量接口）"""
    if not token:
        return False
    try:
        req = urllib.request.Request(BILLING_URL, data=b"{}", method="POST")
        req.add_header("Authorization", token)
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", _UA)
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data.get("code") == 0
    except Exception:
        return False


def get_token(max_age_seconds=86400 * 6):
    """获取可用 token：缓存优先 → 缓存失效/无缓存时重新捕获"""
    cached = _load_token_cache()
    if cached and _token_valid(cached):
        return cached
    return capture_token(wait_seconds=25)


# --------------------------------------------------------------------------- 1. 积分余额
def read_credits():
    """读取精确积分余额。
    返回 dict: {left, total, used, is_paid, packages:[{code, remain, total, unit}]}
    失败返回 None。"""
    token = _load_token_cache()
    if not _token_valid(token):
        token = get_token()
    if not token:
        return None
    try:
        req = urllib.request.Request(BILLING_URL, data=b"{}", method="POST")
        req.add_header("Authorization", token)
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", _UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        if data.get("code") != 0:
            return None
        d = data.get("data") or {}
        packages = []
        for p in d.get("Packages") or []:
            packages.append({
                "code": p.get("PackageCode", ""),
                "total": _f(p.get("CycleTotalCapacity")),
                "remain": _f(p.get("CycleRemainCapacity")),
                "unit": p.get("CapacityUnit", "credits"),
            })
        return {
            "left": round(sum(p["remain"] for p in packages), 2),
            "total": round(sum(p["total"] for p in packages), 2),
            "used": round(sum((p["total"] or 0) - (p["remain"] or 0) for p in packages), 2),
            "is_paid": bool(d.get("IsPaidUser")),
            "packages": packages,
        }
    except Exception:
        return None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- 2. 模型列表
_JS_LIST_MODELS = """
(() => {
  const items = [...document.querySelectorAll('.cr-model-selector__item')];
  return JSON.stringify(items.map(it => {
    const nameEl = it.querySelector('.cr-model-selector__item-name');
    const name = (nameEl?.textContent || '').trim();
    const badge = (it.querySelector('.cr-model-selector__item-badge')?.textContent || '').trim();
    // 描述/提示行：部分版本把「每日 23:00-08:00 免费」写在子元素里
    const desc = (it.querySelector('.cr-model-selector__item-desc')?.textContent
               || it.querySelector('[class*="desc"]')?.textContent
               || it.querySelector('[class*="tip"]')?.textContent || '').trim();
    const text = (it.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160);
    // 计费后缀：DOM 无空格拼接时模型名与倍率会粘在一起
    // （如 'MiniMax-M30.25x' = 名字 'MiniMax-M3' + 倍率 '0.25x'）。
    // 显式剥掉模型名，只把名字之后的尾部交给倍率解析，
    // 否则 'M3' 会被当成倍率 30.25，免费/计费判定会完全错误。
    let cost = '';
    if (nameEl && name) {
      const full = (it.textContent || '').replace(/\s+/g, ' ').trim();
      const idx = full.indexOf(name);
      if (idx >= 0) cost = full.slice(idx + name.length).trim();
    }
    if (!cost) cost = text.slice(Math.max(0, text.length - 24));
    return {name, badge, desc, text, cost};
  }));
})()
"""

# 从页面 DOM 发现全局免费时段（闲时夜间窗口）。
# 先扫含「免费/免/闲时/夜间/错峰」上下文的时间范围，再退化为全文首个 HH:MM-HH:MM。
_JS_FIND_FREE_WINDOW = r"""
(() => {
  const pat = /(免费|免|闲时|夜间|错峰|限免)[^\n]{0,40}?(\d{1,2}):(\d{2})\s*[-~到至]\s*(\d{1,2}):(\d{2})/;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    const m = n.nodeValue && n.nodeValue.match(pat);
    if (m) return JSON.stringify({start: m[2]+':'+m[3], end: m[4]+':'+m[5], ctx: n.nodeValue.trim().slice(0, 60)});
  }
  const body = document.body ? (document.body.innerText || '') : '';
  const m2 = body.match(/(\d{1,2}):(\d{2})\s*[-~到至]\s*(\d{1,2}):(\d{2})/);
  if (m2) return JSON.stringify({start: m2[1]+':'+m2[2], end: m2[3]+':'+m2[4], ctx: 'body'});
  return null;
})()
"""

_JS_OPEN_MENU = """
(async () => {
  const isOpen = () => document.querySelectorAll('.cr-model-selector__item').length > 0;
  if (isOpen()) return 'open';
  const t = document.querySelector('.cr-model-selector__trigger');
  if (!t) return 'no-trigger';
  const o = {bubbles: true, cancelable: true, view: window};
  t.dispatchEvent(new PointerEvent('pointerdown', o));
  t.dispatchEvent(new MouseEvent('mousedown', o));
  t.dispatchEvent(new PointerEvent('pointerup', o));
  t.dispatchEvent(new MouseEvent('mouseup', o));
  t.click();
  for (let i = 0; i < 30; i++) {
    await new Promise(r => setTimeout(r, 100));
    if (isOpen()) return 'open';
  }
  return 'fail';
})()
"""

_JS_CURRENT_MODEL = """
(() => {
  const el = document.querySelector('.cr-model-selector__trigger-label');
  return el ? el.textContent.trim() : '';
})()
"""

_JS_CLOSE_MENU = """
(() => {
  document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  const t = document.querySelector('.cr-model-selector__trigger');
  if (t) { t.blur(); }
  return true;
})()
"""


def list_models():
    """枚举模型下拉菜单。返回 [{name, badge, text}] 或 None"""
    if _evaluate(_JS_OPEN_MENU, timeout=10) != "open":
        return None
    models = _evaluate(_JS_LIST_MODELS)
    _evaluate(_JS_CLOSE_MENU)
    try:
        return json.loads(models) if models else None
    except Exception:
        return None


def current_model():
    """读取当前选中模型名"""
    return _evaluate(_JS_CURRENT_MODEL) or None


# ===========================================================================
# 回复捕获（读取回答）—— 基于 WorkBuddy 5.6.2 真实 DOM
#
# 实测结构（2026-09-24，WorkBuddy 5.6.2）：
#   .cr-message-list > .cr-message-list__content > .cr-document
#     > .cr-document__virtual-item            (一条消息，虚拟列表渲染)
#         > .cr-frame.cr-frame--right         (用户消息)
#         > .cr-frame.cr-frame--left          (助手消息)
#             > .cr-frame__content > .cr-agent
#                 > .cr-agent__header (.cr-agent__name / .cr-agent__completion-status)
#                 > .cr-agent__body
#                     > .cr-agent__content > .cr-markdown  <- 真实回答正文
#                     > .conversation-finished-footer      <- 「Hy3 / 13:13」元数据，必须排除
#
# 旧实现按 .cr-agent__body 取正文，会把 footer 的「模型名 / 时间」拼进回复；
# 选择器全部落空时还会命中首页无障碍文案（如 'To pick up a draggable item...'），
# 把 UI 噪声当成回复推进迭代 —— 两者都会让自动迭代失真。
#
# 修正：
#   1. 只从 .cr-agent__content / .cr-markdown 取正文，明确排除 footer 元数据；
#   2. 区分左右气泡（.cr-frame--left/right），绝不把用户发的消息当回复；
#   3. 支持按「上一轮发送的提示词」定位会话：WorkBuddy 发送后会回到会话列表，
#      需先点开对应会话才读得到回复；
#   4. 保留 .cr-chat-message--assistant / [data-role=assistant] 作为旧版本回退。
# ===========================================================================
REPLY_SELECTORS = [
    ".cr-agent__content",   # 5.6.2 真实正文容器（内含 .cr-markdown）
    ".cr-markdown",
    ".cr-chat-message--assistant",
    '[data-role="assistant"]',
]
# 尾部元数据：旧版本把「共消耗 N / 模型 / 时间」附在正文里
_REPLY_META_TAIL = re.compile(r"\s*共消耗\s*[\d.]+\s*.*$", re.S)
# 明显的非助手内容（导航/无障碍提示/占位符），命中即视为无效回复
_NOISE_PATTERNS = (
    "to pick up a draggable item", "press the space bar", "while dragging",
    "press space again", "press escape to cancel",
    "今天帮你做什么", "添加上下文", "调用技能与指令", "暂无对话",
    "新的对话", "开始新对话",
)

# 读取当前对话全部消息（区分用户/助手）。虚拟列表会裁剪节点，
# 因此用 .cr-document__virtual-item（每项一条消息），退化到 .cr-message-list__content > *。
_JS_READ_CONVERSATION = r"""
(() => {
  const items = [...document.querySelectorAll(
      '.cr-document__virtual-item, .cr-message-list__content > *')];
  if (!items.length) return null;
  const out = [];
  // 助手气泡里要排除的非正文块：深度思考折叠块、完成页脚、用量元数据
  const isNoise = (el) => {
    const c = ((el.className || '') + '').toLowerCase();
    return c.includes('collapse') || c.includes('footer')
        || c.includes('completion-status') || c.includes('agent__header');
  };
  const pickText = (root) => {
    // 1) 取正文容器里"可见正文块"的拼接，天然排除 collapse/footer
    const kids = [...root.children].filter(k => !isNoise(k));
    if (kids.length) {
      const t = kids.map(k => (k.innerText || k.textContent || '')).join('\n').trim();
      if (t) return t;
    }
    // 2) 退化：整体文本（旧版本结构）
    return (root.innerText || root.textContent || '').trim();
  };
  for (const it of items) {
    const isUser = !!it.querySelector('.cr-frame--right, .cr-user, .cr-message--user');
    const agent  = it.querySelector('.cr-agent');
    let body = null;
    if (agent) {
      // 只取正文容器：.cr-agent__content（内含 .cr-markdown），
      // 过滤深度思考折叠块与 footer 元数据（模型名 / 时间 / 共消耗）
      const c = agent.querySelector('.cr-agent__content')
             || agent.querySelector('.cr-markdown')
             || agent.querySelector('.cr-chat-message--assistant')
             || agent.querySelector('[data-role="assistant"]');
      if (c) body = pickText(c);
    } else if (isUser) {
      const c = it.querySelector('.cr-frame__content') || it;
      body = (c.innerText || c.textContent || '').trim();
    } else {
      continue;
    }
    if (body) out.push({role: isUser ? 'user' : 'assistant', text: body.slice(0, 4000)});
  }
  return out.length ? JSON.stringify(out) : null;
})()
"""

# 列出会话卡片标题（用于按提示词定位会话）
_JS_LIST_CONVERSATIONS = r"""
(() => {
  const cards = [...document.querySelectorAll('.conversation-agent-card')];
  if (!cards.length) return null;
  return JSON.stringify(cards.map(s => {
    const t = s.querySelector('[class*="title"], [class*="name"]');
    return {
      selected: s.className.includes('_selected'),
      title: t ? (t.textContent || '').trim().slice(0, 120) : '',
      text: (s.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200)
    };
  }));
})()
"""

# 在会话列表中按关键词点击卡片（KW 由调用方替换为 JSON 字符串字面量）
_JS_OPEN_CONV_BY_KEYWORD = r"""
(() => {
  const kw = KW.toLowerCase();
  const cards = [...document.querySelectorAll('.conversation-agent-card')];
  const hit = cards.find(s => (s.innerText || '').toLowerCase().includes(kw));
  if (!hit) return 'not-found';
  const o = {bubbles: true, cancelable: true, view: window};
  hit.dispatchEvent(new MouseEvent('mousedown', o));
  hit.dispatchEvent(new MouseEvent('mouseup', o));
  hit.click();
  return 'clicked';
})()
""".replace("KW", json.dumps("__KW__"))


def _in_conversation_page() -> bool:
    """当前是否处于对话页（能看到消息列表）。"""
    try:
        return bool(_evaluate(
            "!!document.querySelector('.cr-message-list, .cr-agent, .cr-document')",
            timeout=10))
    except Exception:
        return False


def _conversation_titles():
    """会话列表卡片标题；不在列表页或读取失败返回 None。"""
    try:
        raw = _evaluate(_JS_LIST_CONVERSATIONS, timeout=15)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _open_conversation_by_title(keyword: str) -> bool:
    """在会话列表里点击标题含 keyword 的卡片。成功返回 True。"""
    kw = (keyword or "").lower().replace("'", "").replace('"', "")
    if not kw:
        return False
    js = _JS_OPEN_CONV_BY_KEYWORD.replace("__KW__", kw)
    try:
        return _evaluate(js, timeout=15) == "clicked"
    except Exception:
        return False


def _open_latest_conversation() -> bool:
    """若当前停在会话列表（首页），点击第一个会话卡片进入对话页。

    WorkBuddy 发送消息后常回到首页列表；不回对话页就读不到 .cr-agent__content，
    自动迭代会因此永远捕获不到回复。进入失败返回 False（调用方按未就绪处理）。"""
    js = r"""
    (() => {
      const isChat = document.querySelector('.cr-message-list, .cr-agent__content, .cr-document');
      if (isChat) return 'already-chat';
      const card = document.querySelector('.conversation-agent-card');
      if (!card) return 'no-card';
      const o = {bubbles: true, cancelable: true, view: window};
      card.dispatchEvent(new MouseEvent('mousedown', o));
      card.dispatchEvent(new MouseEvent('mouseup', o));
      card.click();
      return 'clicked';
    })()
    """
    try:
        return _evaluate(js, timeout=15) in ("clicked", "already-chat")
    except Exception:
        return False


def _ensure_conversation_open(prompt_hint: str = "") -> bool:
    """确保停留在含 prompt_hint 的对话页；否则尝试打开匹配会话，退回打开首个会话。

    注意：只要给了 prompt_hint，就必须按标题定位到“本轮”会话，不能在
    `_in_conversation_page()` 处短路——否则会读到别的会话/上一轮的回复，
    自动迭代会把过期上下文当成本轮产出推进。"""
    hint = (prompt_hint or "").strip()
    if hint:
        # 会话卡片标题只显示开头若干字，取多级前缀逐一尝试
        for kw in filter(None, (hint[:24], hint[:16], hint[:8])):
            if _open_conversation_by_title(kw):
                time.sleep(1.2)
                if _in_conversation_page():
                    return True
        # 标题定位失败（旧会话可能已滚出虚拟列表）：若当前停在某个对话页，
        # 只有该页确实包含本轮提示词才可复用——绝不能在无关对话页短路，
        # 那会读到别的会话/上一轮的回复。否则退回最新会话，由下方 hint
        # 匹配兜底校验（latest 常是本轮会话）。
        if _in_conversation_page() and _conversation_has_prompt(hint):
            return True
    if _open_latest_conversation():
        time.sleep(1.2)
        if not _in_conversation_page():
            return False
        # 带 hint 时最新会话也必须真的包含本轮提示词；
        # 不带 hint 由 _find_reply_after 的提示词匹配兜底。
        return not hint or _conversation_has_prompt(hint)
    return False


def _conversation_has_prompt(hint: str) -> bool:
    """当前对话页的用户消息里是否真的包含 prompt_hint 开头（归一化后）。"""
    conv = read_conversation()
    if not conv:
        return False
    head = _norm_for_match(hint)[:_HINT_MIN_OVERLAP]
    if not head:
        return False
    return any(m.get("role") == "user"
               and head in _norm_for_match(m.get("text") or "")
               for m in conv)


def read_conversation():
    """读取当前对话页的全部消息。返回 [{role, text}]，读不到返回 None。"""
    try:
        raw = _evaluate(_JS_READ_CONVERSATION, timeout=15)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _looks_like_model_name(s: str) -> bool:
    """像模型名（含版本号/连字符/点号），用于剔除 footer 里的模型行。"""
    if not s or len(s) > 24 or " " in s:
        return False
    return bool(re.search(r"[A-Za-z]", s)) and bool(re.search(r"[-.\d]", s))


_THINKING_LINE_RE = re.compile(r"^(?:已)?深度思考(?:（[^）]*）|\([^)]*\))?$")


def _clean_reply_text(text: str) -> str:
    """剥掉头部「深度思考」标题与尾部元数据行（模型名/时间/共消耗）。"""
    if not text:
        return ""
    t = _REPLY_META_TAIL.sub("", text)
    lines = [l.rstrip() for l in t.splitlines()]
    # 头部：「深度思考」「已深度思考（用时 X 秒）」这类折叠块标题，
    # 真实实测发现 .cr-collapse 的标题文本仍会出现在 innerText 里
    while lines and _THINKING_LINE_RE.match(lines[0].strip()):
        lines.pop(0)
    # 尾部：时间戳/日期/模型名等元数据
    while lines:
        tail = lines[-1].strip()
        if (re.fullmatch(r"\d{1,2}:\d{2}", tail)
                or re.fullmatch(r"\d{4}-\d{2}-\d{2}", tail)
                or re.fullmatch(r"(今天|昨天|前天).*", tail)
                or _looks_like_model_name(tail)):
            lines.pop()
            continue
        break
    return "\n".join(lines).strip()


def _looks_like_noise(text: str) -> bool:
    """是否明显的非回复内容（无障碍提示/占位符/导航文本）。"""
    if not text:
        return True
    low = text.lower()
    if len(text.strip()) < 2:
        return True
    return any(p in low for p in _NOISE_PATTERNS)


_HINT_MIN_OVERLAP = 12   # 提示词与用户气泡至少要有这么多字符重叠才认这条回复


def _norm_for_match(s: str) -> str:
    """归一化用于匹配：去掉 BOM/空白/换行，便于在 DOM 文本里找提示词。"""
    return re.sub(r"[\s\ufeff\u200b]+", "", s or "")


def _find_reply_after(conv, prompt_hint: str):
    """在对话里定位「紧跟在本轮提示词之后」的助手回复。

    直接取最后一条助手消息是**错的**：发送后回到列表页时，最后一条助手消息
    很可能属于别的会话/上一轮，用旧回复推进迭代会让后续提示词拿到过期上下文。
    因此这里要求：提示词必须出现在最后一条用户消息里，回复取它后面的助手消息。
    匹配不上时返回 (None, reason)，调用方判定为「读不到」，绝不拿别人的回复冒进。"""
    assistants = [m for m in conv
                  if m.get("role") == "assistant" and (m.get("text") or "").strip()]
    users = [m for m in conv
             if m.get("role") == "user" and (m.get("text") or "").strip()]
    if not assistants:
        return None, "no-assistant"
    hint = _norm_for_match(prompt_hint)
    if not hint:
        # 没有提示词可用时只能退回最后一条（调用方多数场景已带 hint）
        return assistants[-1]["text"], ""
    key = hint[:48] if len(hint) > 48 else hint
    # 从后往前找包含本轮提示词的用户消息（提示词可能被截断/改写）
    idx = None
    for i in range(len(users) - 1, -1, -1):
        u = _norm_for_match(users[i]["text"])
        if key and (key in u or u[-len(key):] == key):
            idx = i
            break
    if idx is None:
        # 更宽松：用户首句与提示词开头重合
        head = hint[:_HINT_MIN_OVERLAP]
        for i in range(len(users) - 1, -1, -1):
            u = _norm_for_match(users[i]["text"])
            if u.startswith(head) or head in u:
                idx = i
                break
    if idx is None:
        return None, "prompt-not-found"
    # 取该用户消息之后的第一条助手消息
    after = [m for m in assistants
             if conv.index(m) > conv.index(users[idx])]
    if not after:
        return None, "no-reply-after-prompt"
    return after[0]["text"], ""


def _read_last_agent_reply(prompt_hint: str = ""):
    """读取「本轮提示词」对应的助手回复；匹配不到/噪声/非对话页返回 None。"""
    conv = read_conversation()
    if not conv:
        return None
    raw, why = _find_reply_after(conv, prompt_hint)
    if not raw:
        return None
    raw = raw.strip()
    if _looks_like_noise(raw):
        return None
    return _clean_reply_text(raw) or None


def last_assistant_message(timeout=240, poll=15, prompt_hint=""):
    """读取 WorkBuddy 对「本轮提示词」的回答文本。

    - 若停在会话列表，先按 prompt_hint 打开对应会话（退回打开首个会话）；
    - 只认助手气泡（.cr-frame--left / .cr-agent__content），排除 footer 元数据与
      「深度思考」折叠块；
    - 必须定位到「紧跟在本轮提示词之后」的助手消息，避免读到别的会话/上一轮的回复；
    - 过滤无障碍提示/占位符等 UI 噪声；
    - 轮询等待生成结束后返回，避免截到流式输出的一半。

    读不到 / 判断为噪声 / 仍在生成中时一律返回 None
    （调用方据此按未完成处理，绝不拿噪声或旧回复推进迭代）。"""
    if not _ensure_conversation_open(prompt_hint):
        # 带 hint 却定位不到包含该提示词的会话：立刻失败。
        # 绝不留在无关会话里轮询——那只会白等并可能读到过期回复。
        return None
    deadline = time.time() + max(poll, timeout)
    while time.time() < deadline:
        raw = _read_last_agent_reply(prompt_hint)
        if raw and not _agent_still_generating():
            # 助手气泡已生成完成，可安全返回
            return raw
        # 尚未生成完成：绝不在超时后返回半截内容，
        # 否则自动迭代会拿流式输出的一半当成本轮产出，污染后续提示词。
        time.sleep(poll)
    return None


def _agent_still_generating() -> bool:
    """最后一个助手气泡是否仍在输出中（有"停止"按钮/生成中状态）。"""
    js = r"""
    (() => {
      const agents = [...document.querySelectorAll('.cr-agent')];
      if (!agents.length) return false;
      const st = agents[agents.length - 1].querySelector('.cr-agent__completion-status');
      if (!st) return false;
      const t = (st.textContent || '').trim();
      return /生成中|进行中|思考|加载|loading|\u2026/.test(t) || t === '';
    })()
    """
    try:
        return bool(_evaluate(js, timeout=10))
    except Exception:
        return True  # 读不到状态时保守视为仍在生成


def discover_free_window_dom():
    """从 WorkBuddy 页面 DOM 发现全局免费时段（闲时夜间窗口）。
    返回 {start, end, ctx} 或 None（CDP 不可用 / 页面无时段信息）。"""
    raw = _evaluate(_JS_FIND_FREE_WINDOW, timeout=10)
    if not raw:
        return None
    try:
        d = json.loads(raw)
        sh, sm = int(d["start"].split(":")[0]), int(d["start"].split(":")[1])
        eh, em = int(d["end"].split(":")[0]), int(d["end"].split(":")[1])
        return {"start": f"{sh:02d}:{sm:02d}", "end": f"{eh:02d}:{em:02d}",
                "ctx": d.get("ctx", "")}
    except Exception:
        return None


def select_model(name):
    """选择指定模型（等价于人工点击下拉选项）。
    返回 (True, '') 或 (False, 原因)。"""
    if _evaluate(_JS_OPEN_MENU, timeout=10) != "open":
        return False, "无法打开模型菜单（WorkBuddy 未运行或无 CDP）"
    js = """
    (() => {
      const name = %s;
      const items = [...document.querySelectorAll('.cr-model-selector__item')];
      const hit = items.find(it =>
        (it.querySelector('.cr-model-selector__item-name')?.textContent || '').trim() === name);
      if (!hit) return 'not-found';
      const o = {bubbles: true, cancelable: true, view: window};
      hit.dispatchEvent(new PointerEvent('pointerdown', o));
      hit.dispatchEvent(new MouseEvent('mousedown', o));
      hit.dispatchEvent(new PointerEvent('pointerup', o));
      hit.dispatchEvent(new MouseEvent('mouseup', o));
      hit.click();
      return 'clicked';
    })()
    """ % json.dumps(name)
    result = _evaluate(js)
    time.sleep(0.4)
    _evaluate(_JS_CLOSE_MENU)
    if result == "clicked":
        # 验证是否真的切换
        time.sleep(0.6)
        cur = current_model()
        if cur and cur.strip() == name.strip():
            return True, ""
        return False, f"切换后校验失败：期望 {name!r}，当前显示 {cur!r}"
    return False, f"未找到模型 {name!r}"


def editor_contains(needle: str) -> bool:
    """WorkBuddy 输入框当前文本是否包含 needle。

    用于发送前后的双重校验：粘贴后必须能看到内容（否则焦点落错了），
    回车后必须看不到内容（否则消息没提交）。返回 True 表示"包含"；
    读不到输入框时返回 False，由调用方决定是否放行。"""
    if not needle:
        return False
    js = r"""
    (() => {
      const eds = [...document.querySelectorAll(
          '[contenteditable="true"], textarea, .cr-editor, [class*="editable"]')];
      // 只取可见的那个：对话页与首页各有一个编辑器
      const vis = eds.filter(e => {
        const r = e.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      });
      const el = vis[vis.length - 1] || eds[eds.length - 1];
      if (!el) return null;
      return (el.innerText || el.value || '').slice(0, 4000);
    })()
    """
    try:
        raw = _evaluate(js, timeout=10)
        if not raw:
            return False
        return needle in raw
    except Exception:
        return False


def start_new_conversation() -> bool:
    """点击侧栏「新建任务」，确保消息发进全新对话而不是当前打开的会话。

    真实事故：auto_send_to_workbuddy 直接粘进「当前打开的会话」——
    若 WorkBuddy 停在某个历史会话页，测试消息会污染该会话，并沿用
    会话当前的计费模型（实测 Hy3 选择被会话覆盖成 Deepseek-V4.1-Flash，
    白扣 9.17 积分）。发送前必须先回新建页。"""
    js = """
    (() => {
      const cands = [...document.querySelectorAll('button, [role=button], a')];
      const hit = cands.find(e => {
        const t = (e.innerText || e.title || e.getAttribute('aria-label') || '').trim();
        if (!t || t.length > 20) return false;
        const r = e.getBoundingClientRect();
        if (!(r.width > 0 && r.height > 0)) return false;
        return t.includes('新建任务') || t.includes('新对话') || t.includes('新聊天');
      });
      if (!hit) return 'no-button';
      const o = {bubbles: true, cancelable: true, view: window};
      hit.dispatchEvent(new PointerEvent('pointerdown', o));
      hit.dispatchEvent(new MouseEvent('mousedown', o));
      hit.dispatchEvent(new PointerEvent('pointerup', o));
      hit.dispatchEvent(new MouseEvent('mouseup', o));
      hit.click();
      return 'clicked';
    })()
    """
    try:
        if _evaluate(js, timeout=10) != "clicked":
            return False
        time.sleep(1.0)
        # 校验：新会话页不应再有历史消息列表（.cr-message-list__content 下的条目）
        check = """(() => {
          const items = document.querySelectorAll(
              '.cr-message-list__content .cr-document__virtual-item');
          return items.length;
        })()"""
        try:
            empty = _evaluate(check, timeout=10) in (0, None)
        except Exception:
            empty = False
        return empty
    except Exception:
        return False


# 输入框聚焦表达式（focus_editor 与 insert_text_into_editor 共用）
_JS_FOCUS_EDITOR = r"""
(() => {
  const eds = [...document.querySelectorAll(
      '[contenteditable="true"], textarea, .cr-editor, [class*="editable"]')];
  const vis = eds.filter(e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  const el = vis[vis.length - 1] || eds[eds.length - 1];
  if (!el) return "no-editor";
  const o = {bubbles: true, cancelable: true, view: window};
  el.dispatchEvent(new PointerEvent("pointerdown", o));
  el.dispatchEvent(new MouseEvent("mousedown", o));
  el.dispatchEvent(new PointerEvent("pointerup", o));
  el.dispatchEvent(new MouseEvent("mouseup", o));
  el.dispatchEvent(new MouseEvent("click", o));
  el.focus();
  const ae = document.activeElement;
  if (el === ae || el.contains(ae)) return "focused";
  return "focus-failed";
})()
"""


def insert_text_into_editor(text: str) -> bool:
    """通过 CDP Input.insertText 把文本直接注入已聚焦的输入框。

    为什么存在：剪贴板 Ctrl+V 链路依赖「窗口激活→系统焦点→编辑器焦点」
    三层全部成立，真实环境里前台锁定/焦点偶发丢失会让粘贴静默落空。
    Input.insertText 是浏览器内核级注入，只要求编辑器在页面内拿到焦点
    （focus_editor 负责这一步），不依赖剪贴板、不依赖系统前台。
    注入后用 editor_contains 自校验。返回 True 表示文本确实进入了输入框。"""
    if not text:
        return False
    t = _find_page_target()
    if not t:
        return False
    try:
        with _Cdp(t) as c:
            r = c.call("Runtime.evaluate",
                       {"expression": _JS_FOCUS_EDITOR,
                        "returnByValue": True, "awaitPromise": True})
            if r.get("result", {}).get("value") != "focused":
                return False
            c.call("Input.insertText", {"text": text}, timeout=10)
        # 自校验：前 40 字必须真的出现在输入框里
        return editor_contains(text.strip()[:40])
    except Exception:
        return False


def focus_editor() -> bool:
    """把 WorkBuddy 输入框拉到焦点状态（CDP 直接 DOM focus）。

    真实 bug：_activate_window 只前置窗口，不保证焦点落在输入框。
    之前发送成功靠的是 select_model 的副作用让编辑器拿到焦点；
    读回复走 _ensure_conversation_open 后焦点可能还留在别处，
    Ctrl+V 落在页面上但进不了编辑器，粘贴校验失败导致发送被取消。

    这里先派发 pointer/mouse 事件（框架可能监听 mousedown 才把光标
    放进编辑器），再调用原生 focus()，最后读回 document.activeElement
    确认真的聚焦上了。返回 True 表示编辑器已拿到焦点。"""
    js = r"""
    (() => {
      const eds = [...document.querySelectorAll(
          '[contenteditable="true"], textarea, .cr-editor, [class*="editable"]')];
      const vis = eds.filter(e => {
        const r = e.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      });
      const el = vis[vis.length - 1] || eds[eds.length - 1];
      if (!el) return "no-editor";
      const o = {bubbles: true, cancelable: true, view: window};
      el.dispatchEvent(new PointerEvent("pointerdown", o));
      el.dispatchEvent(new MouseEvent("mousedown", o));
      el.dispatchEvent(new PointerEvent("pointerup", o));
      el.dispatchEvent(new MouseEvent("mouseup", o));
      el.dispatchEvent(new MouseEvent("click", o));
      el.focus();
      const ae = document.activeElement;
      if (el === ae || el.contains(ae)) return "focused";
      // 某些框架把 focus 转发到内部节点，退而求其次：编辑器在活动路径上即可
      return ae && ae.closest && ae.closest('[contenteditable="true"], .cr-editor')
          ? "focused-inner" : "not-focused";
    })()
    """
    try:
        return _evaluate(js, timeout=10) in ("focused", "focused-inner")
    except Exception:
        return False


def is_workbuddy_running():
    """WorkBuddy 是否在运行且 CDP 可用"""
    return _find_page_target() is not None


def clear_editor(needle: str = "") -> bool:
    """清空输入框里残留的草稿（Enter 没提交成功、或注入落了一半时用）。

    真实缺陷：旧代码只在回车后校验「输入框仍有内容」，判失败就直接返回，
    草稿却还留在编辑器里。下一轮重试时新内容被追加到旧草稿之后，
    一次发送变成「旧稿+新内容」拼起来的那条，幂等去重认不出来
    （它查的是本轮内容的前 40 字，对话里出现的是拼接后的整条）。
    消息照样进了会话，但内容已经不是要发的那条了。

    needle 是这份草稿的头部特征（调用方传待发送内容前 40 字）：
    只有它确实还在输入框里才动手，避免误删用户正在写的别的内容。
    返回 True 表示残留已清掉（或本就不存在），False 表示没清掉。"""
    t = _find_page_target()
    if not t:
        return False
    if needle and not editor_contains(needle):
        return True          # 输入框里没有这份草稿，本来就是干净的
    try:
        with _Cdp(t) as c:
            r = c.call("Runtime.evaluate",
                       {"expression": _JS_FOCUS_EDITOR,
                        "returnByValue": True, "awaitPromise": True},
                       timeout=10)
            if r.get("result", {}).get("value") != "focused":
                return False
            c.call("Input.dispatchKeyEvent",
                   {"type": "rawKeyDown", "key": "a", "code": "KeyA",
                    "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65,
                    "modifier": 2}, timeout=10)   # modifier=2 → Ctrl
            c.call("Input.dispatchKeyEvent",
                   {"type": "keyUp", "key": "a", "code": "KeyA",
                    "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65,
                    "modifier": 2}, timeout=10)
            c.call("Input.dispatchKeyEvent",
                   {"type": "keyDown", "key": "Backspace", "code": "Backspace",
                    "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8,
                    "text": ""}, timeout=10)
            c.call("Input.dispatchKeyEvent",
                   {"type": "keyUp", "key": "Backspace", "code": "Backspace",
                    "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8},
                   timeout=10)
        time.sleep(0.2)
        return not editor_contains(needle) if needle else True
    except Exception:
        return False
