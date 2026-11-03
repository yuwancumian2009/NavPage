"""通知中心：把面板里值得知道的事推到企业微信「自建应用」（应用消息 · 图文卡片）。

设计要点（都是踩过的坑）：
  * 更新/清理/节点/frp 全在后台线程里跑，通知必须「永不抛异常、永不阻塞」——
    一次接口超时不能拖住定时任务，所以发送一律丢到 daemon 线程里做。
  * 走的是应用消息通道：CorpId + Secret 换 access_token，再按 agentid 发给应用可见范围内的人。
    接口地址可配（默认 qyapi.weixin.qq.com，海外/受限网络可填自建反代）。token 缓存在进程内，
    失效（40014/42001）时自动清缓存重试一次。
  * 事件按 (事件, key) 去抖：节点抖动、frp 反复重连时只报一次，冷却期内不重复。
  * 发送结果落 /data/notify_history.json，UI 里能看最近 30 条，出问题不用翻日志。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

from . import db

HIST_FILE = os.path.join(db.DATA_DIR, "notify_history.json")
_LOCK = threading.Lock()
_LAST: dict[tuple[str, str], float] = {}
_TOK = {"key": "", "token": "", "exp": 0.0}
API_DEFAULT = "https://qyapi.weixin.qq.com"

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


def api_base(s: dict | None = None) -> str:
    s = s or db.get_settings()
    return ((s.get("notify_wecom_api") or "").strip() or API_DEFAULT).rstrip("/")


def creds(s: dict | None = None) -> tuple[str, str, str]:
    s = s or db.get_settings()
    return ((s.get("notify_wecom_corpid") or "").strip(),
            (s.get("notify_wecom_secret") or "").strip(),
            str(s.get("notify_wecom_agentid") or "").strip())


def configured(s: dict | None = None) -> bool:
    cid, sec, aid = creds(s)
    return bool(cid and sec and aid)


def _http_json(url: str, payload: dict | None = None, timeout: float = 10.0) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}")
    try:
        return json.loads(raw)
    except Exception:
        raise RuntimeError(f"返回非 JSON：{raw[:160]}")


def _access_token(s: dict) -> str:
    cid, sec, _aid = creds(s)
    base = api_base(s)
    key = f"{base}|{cid}|{sec[-8:]}"
    now = time.time()
    with _LOCK:
        if _TOK["key"] == key and float(_TOK["exp"]) > now + 120:
            return str(_TOK["token"])
    j = _http_json(f"{base}/cgi-bin/gettoken?corpid={quote(cid)}&corpsecret={quote(sec)}")
    if int(j.get("errcode") or 0) != 0:
        raise RuntimeError(f"取 access_token 失败：errcode={j.get('errcode')} {j.get('errmsg')}")
    tok = str(j.get("access_token") or "")
    if not tok:
        raise RuntimeError("取 access_token 失败：返回里没有 access_token")
    with _LOCK:
        _TOK.update({"key": key, "token": tok, "exp": now + int(j.get("expires_in") or 7200)})
    return tok


def _article(title: str, desc: str, url: str = "", picurl: str = "") -> dict:
    a = {"title": (title or "导航页通知")[:128], "description": (desc or "")[:512]}
    if url:
        a["url"] = url
    if picurl:
        a["picurl"] = picurl
    return a


def app_send(article: dict, s: dict | None = None) -> tuple[bool, str]:
    """msgtype=news 的应用消息。返回 (是否成功, 可读结果)。"""
    s = s or db.get_settings()
    cid, sec, aid = creds(s)
    if not (cid and sec and aid):
        return False, "企业微信应用凭证不完整（CorpId / Secret / AgentId 都要填）"
    try:
        agentid = int(aid)
    except Exception:
        return False, f"AgentId 不是数字：{aid}"
    base = api_base(s)
    touser = (s.get("notify_wecom_touser") or "@all").strip() or "@all"
    payload = {
        "touser": touser,
        "msgtype": "news",
        "agentid": agentid,
        "news": {"articles": [article]},
        "safe": 0,
    }
    for attempt in (1, 2):
        try:
            tok = _access_token(s)
            j = _http_json(f"{base}/cgi-bin/message/send?access_token={quote(tok)}", payload)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        code = int(j.get("errcode") or 0)
        if code == 0:
            bad = (j.get("invaliduser") or "").strip()
            return True, ("发送成功" + (f"（无效接收人：{bad}）" if bad else ""))
        if code in (40014, 42001) and attempt == 1:
            with _LOCK:
                _TOK.update({"key": "", "token": "", "exp": 0.0})
            continue
        return False, f"errcode={code} errmsg={j.get('errmsg')}"
    return False, "发送失败（未知原因）"


def _dispatch(title: str, desc: str, url: str, event: str):
    ok, msg = app_send(_article(title, desc, url))
    _hist_add({"ts": _now(), "event": event, "title": title, "desc": desc,
               "url": url, "ok": bool(ok), "msg": "" if ok else msg})


def send(event: str, title: str, desc: str = "", url: str = "", key: str = "") -> bool:
    """按事件开关 + 冷却时间发一条通知。任何异常都吞掉，绝不影响调用方。"""
    try:
        s = db.get_settings()
        if not s.get("notify_enabled"):
            return False
        if not configured(s):
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
                url = base + "/"
            elif not url.startswith("http"):
                url = base.rstrip("/") + url
        threading.Thread(target=_dispatch, args=(title, desc, url, event),
                         name="navpage-notify", daemon=True).start()
        return True
    except Exception:
        return False


def test() -> dict:
    """设置页「发送测试」：同步发一条，把企业微信的原始返回摊给前端。"""
    s = db.get_settings()
    if not configured(s):
        return {"ok": False, "error": "先填 CorpId / 应用 Secret / AgentId"}
    title = "【测试】导航页通知已接通"
    desc = ("这条是设置页的测试消息（企业微信应用 · 图文消息）。\n"
            "收到说明应用凭证正确；之后容器更新、镜像清理、节点异常、frp 重连都会按勾选的事件推送。")
    url = (s.get("notify_panel_url") or "").strip()
    ok, msg = app_send(_article(title, desc, url))
    _hist_add({"ts": _now(), "event": "test", "title": title, "desc": desc,
               "url": url, "ok": bool(ok), "msg": "" if ok else msg})
    return {"ok": bool(ok), "error": "" if ok else msg,
            "detail": {"api": api_base(s), "agentid": creds(s)[2],
                       "touser": (s.get("notify_wecom_touser") or "@all")}}
