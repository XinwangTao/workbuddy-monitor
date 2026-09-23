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
import threading
import time
import urllib.request

try:
    import websocket  # websocket-client
except ImportError:
    websocket = None

CDP_HOST = "http://127.0.0.1:9222"
BILLING_URL = "https://copilot.tencent.com/billing/meter/get-user-resource-summary"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
_TOKEN_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".wb_token_cache")


# --------------------------------------------------------------------------- CDP 基础
def _http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _find_page_target():
    """返回 WorkBuddy 主页面 target（type=page），找不到返回 None"""
    try:
        for t in _http_json(CDP_HOST + "/json"):
            if t.get("type") == "page" and "workbuddy" in (t.get("url", "") + t.get("title", "")).lower():
                return t
    except Exception:
        pass
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
    const name = (it.querySelector('.cr-model-selector__item-name')?.textContent || '').trim();
    const badge = (it.querySelector('.cr-model-selector__item-badge')?.textContent || '').trim();
    // 描述/提示行：部分版本把「每日 23:00-08:00 免费」写在子元素里
    const desc = (it.querySelector('.cr-model-selector__item-desc')?.textContent
               || it.querySelector('[class*="desc"]')?.textContent
               || it.querySelector('[class*="tip"]')?.textContent || '').trim();
    const text = (it.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 160);
    return {name, badge, desc, text};
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


# 读取 WorkBuddy 最新助手回复时尝试的选择器（命中其一即可）。
# 不同版本 DOM 结构可能不同，可在 config.json 里覆盖 workbuddy_cdp.REPLY_SELECTORS。
REPLY_SELECTORS = [
    ".cr-chat-message--assistant",
    '[class*="assistant" i]',
    '[data-role="assistant"]',
    ".message-list .message:last-child",
    '[class*="message" i]:last-of-type',
]


def last_assistant_message(timeout=240, poll=15):
    """读取 WorkBuddy 当前会话最新一条助手消息文本。
    通过轮询等待文本稳定（不再变化）后返回，避免截取到一半的流式输出。
    CDP 不可用 / 读不到时返回 None。"""
    js = r"""
    (() => {
      const sels = %s;
      let el = null;
      for (const s of sels) {
        try {
          const nodes = document.querySelectorAll(s);
          if (nodes.length) { el = nodes[nodes.length - 1]; break; }
        } catch (e) {}
      }
      if (!el) {
        const area = document.querySelector('[class*="chat" i]') || document.body;
        const blocks = [...area.querySelectorAll('div, p, span, pre, code')]
          .filter(n => (n.innerText || n.textContent || '').trim().length > 20);
        el = blocks[blocks.length - 1] || null;
      }
      if (!el) return null;
      const t = (el.innerText || el.textContent || '').trim();
      return t ? t.slice(0, 4000) : null;
    })()
    """ % json.dumps(REPLY_SELECTORS)

    deadline = time.time() + max(poll, timeout)
    last = None
    while time.time() < deadline:
        try:
            val = _evaluate(js, timeout=poll + 5)
        except Exception:
            val = None
        if val:
            # 等文本稳定再返回
            try:
                stable = _evaluate(js, timeout=poll + 5)
            except Exception:
                stable = val
            if stable == val:
                return val
            last = val
        time.sleep(poll)
    return None


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
        return True, f"已点击，当前显示 {cur!r}（可能需要人工确认）"
    return False, f"未找到模型 {name!r}"


def is_workbuddy_running():
    """WorkBuddy 是否在运行且 CDP 可用"""
    return _find_page_target() is not None
