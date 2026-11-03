"""通知中心：把面板里值得知道的事推到企业微信群（图文卡片消息）。

设计要点（都是踩过的坑）：
  * 更新/清理/节点/frp 全在后台线程里跑，通知必须「永不抛异常、永不阻塞」——
    一个 webhook 超时不能拖住定时任务，所以发送一律丢到 daemon 线程里做。
  * 企业微信机器人有频率限制（每分钟 20 条），所以要按 (事件, key) 去抖：
    节点抖动、frp 反复重连时只报一次，冷却期内不重复。
  * 发送结果落 /data/notify_history.json，UI 里能看最近 30 条，出问题不用翻日志。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

from . import db

HIST_FILE = os.path.join(db.DATA_DIR, "notify_history.json")
_LOCK = threading.Lock()
_LAST: dict[tuple[str, str], float] = {}

# 事件清单：key -> (中文名, 默认冷却秒数)
EVENTS: dict[str, tuple[str, int]] = {
    "update_ok":   ("容器自动更新完成", 0),
    "update_fail": ("容器更新失败", 0),
    "prune":       ("镜像清理完成", 0),
    "node_down":   ("远程节点不通", 600),
    "node_up":     ("远程节点恢复", 300),
    "frp_down":    ("frp 掉线", 300),
    "frp_up":      ("frp 重启/重连完成", 120),
    "cert_expiring": ("证书即将过期", 43200),
}


def event_list() -> list[dict]:
    return [{"key": k, "label": v[0], "cooldown": v[1]} for k, v in EVENTS.items()]


def _now() -> int:
    return int(time.time())


def _hist_read() -> list:
    try:
        if os.path.exists(HIST_FILE):
            with open(HIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or []
    except Exception:
        pass
    return []


def _hist_add(rec: dict):
    try:
        with _LOCK:
            arr = _hist_read()
            arr.insert(0, rec)
            with open(HIST_FILE, "w", encoding="utf-8") as f:
                json.dump(arr[:30], f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def history() -> list:
    return _hist_read()


def _wecom_post(webhook: str, payload: dict, timeout: float = 8.0) -> tuple[bool, str]:
    """企业微信群机器人 webhook。errcode=0 才算成功（HTTP 200 但 errcode 非 0 很常见）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    try:
        j = json.loads(raw)
    except Exception:
        return False, f"返回非 JSON：{raw[:160]}"
    if int(j.get("errcode") or 0) != 0:
        return False, f"errcode={j.get('errcode')} errmsg={j.get('errmsg')}"
    return True, raw[:200]


def _news_payload(title: str, desc: str, url: str = "") -> dict:
    """图文卡片：article 的 description 只吃纯文本，换行用 \\n。"""
    article = {
        "title": (title or "导航页通知")[:128],
        "description": (desc or "")[:512],
    }
    if url:                      # 没配面板地址就别塞 localhost，卡片不给链接即可
        article["url"] = url
    return {"msgtype": "news", "news": {"articles": [article]}}


def _do_send(text_title: str, text_desc: str, url: str, event: str, sync: bool = False):
    s = db.get_settings()
    webhook = (s.get("notify_wecom_webhook") or "").strip()
    if not webhook:
        return False, "没有配置企业微信 webhook"

    def _worker():
        ok, msg = _wecom_post(webhook, _news_payload(text_title, text_desc, url))
        _hist_add({"ts": _now(), "event": event, "title": text_title,
                   "desc": text_desc, "url": url, "ok": bool(ok), "msg": "" if ok else msg})

    if sync:
        return _wecom_post(webhook, _news_payload(text_title, text_desc, url))
    threading.Thread(target=_worker, name="navpage-notify", daemon=True).start()
    return True, "已入队"


def send(event: str, title: str, desc: str = "", url: str = "", key: str = "") -> bool:
    """按事件开关 + 冷却时间发一条通知。任何异常都吞掉，绝不影响调用方。"""
    try:
        s = db.get_settings()
        if not s.get("notify_enabled"):
            return False
        if not (s.get("notify_wecom_webhook") or "").strip():
            return False
        on = s.get("notify_events")
        if isinstance(on, list) and event not in on:
            return False
        if event not in EVENTS:
            return False
        cooldown = int(s.get("notify_cooldown") or 0) if event.startswith(("node_", "frp_")) \
            else int(EVENTS[event][1])
        if cooldown > 0:
            k = (event, key or "")
            now = time.time()
            with _LOCK:
                if now - _LAST.get(k, 0.0) < cooldown:
                    return False
                _LAST[k] = now
        base = (s.get("notify_panel_url") or "").strip()
        if base:
            if not url:
                url = base + "/"          # 没指定跳转就落到面板首页，别给企微一个 localhost
            elif not url.startswith("http"):
                url = base.rstrip("/") + url
        _do_send(title, desc, url, event)
        return True
    except Exception:
        return False


def test() -> dict:
    """设置页「发送测试」按钮：同步发一条，直接把企业微信的原始返回给前端。"""
    s = db.get_settings()
    webhook = (s.get("notify_wecom_webhook") or "").strip()
    if not webhook:
        return {"ok": False, "error": "先填企业微信机器人 webhook"}
    title = "【测试】导航页通知已接通"
    desc = ("这条是设置页的测试消息。\n"
            "收到说明 webhook 正确；之后容器更新、镜像清理、节点异常、frp 重连都会按勾选的事件推送。")
    ok, msg = _wecom_post(webhook, _news_payload(title, desc, url=(s.get("notify_panel_url") or "")))
    _hist_add({"ts": _now(), "event": "test", "title": title, "desc": desc, "url": "",
               "ok": bool(ok), "msg": "" if ok else msg})
    return {"ok": bool(ok), "error": "" if ok else msg}
