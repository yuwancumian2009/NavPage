"""极简 5 段 cron 解析（分 时 日 月 周），只服务「镜像定时自动检查」。

为什么不引第三方库：镜像里没有 croniter，为一个定时检查多装一个依赖不划算。
支持的面板够用的语法：`*`、`5`、`1-30`、`*/6`、`1-30/5`、逗号列表、周日写 0 或 7。
不支持 `@daily` 这类别名 —— 前端给的是预设下拉 + 自定义表达式，写错就明确报错，
不猜。
"""
import time

# (最小值, 最大值)
FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))

PRESETS = [
    ("0 3 * * *", "每天 03:00"),
    ("0 */6 * * *", "每 6 小时"),
    ("0 3 * * 0", "每周日 03:00"),
    ("0 3 1 * *", "每月 1 号 03:00"),
]


def _parse_field(spec, lo, hi):
    """单个字段 → 允许值的集合；非法返回 None。"""
    out = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            return None
        step = 1
        if "/" in part:
            part, _, st = part.partition("/")
            if not st.strip().isdigit() or int(st) < 1:
                return None
            step = int(st.strip())
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            a, b = a.strip(), b.strip()
            if not (a.isdigit() and b.isdigit()):
                return None
            a, b = int(a), int(b)
        elif part.isdigit():
            a = b = int(part)
        else:
            return None
        if a > b:
            return None
        # 周日：cron 允许 0 和 7 两种写法，内部统一成 0（周一=1…周六=6、周日=0）。
        # 注意 7 是**周日**不是周六 —— 第一版把 7 折成 6，实测「每周日 0 点」算成了周六。
        if hi == 6:
            if a == 7:                      # 只可能是 a==b==7（a>b 已在上面挡掉）
                a = b = 0
            elif b == 7:                    # 5-7 这种跨周日的范围：range 表达不了 0 在末尾
                out.update(range(a, 7, step))
                out.add(0)
                continue
        if a < lo or b > hi:
            return None
        out.update(range(a, b + 1, step))
    return out or None


def parse(expr):
    """→ (分钟, 小时, 日, 月, 周) 五个集合；非法返回 None。"""
    parts = (expr or "").split()
    if len(parts) != 5:
        return None
    out = []
    for spec, (lo, hi) in zip(parts, FIELDS):
        s = _parse_field(spec, lo, hi)
        if not s:
            return None
        out.append(s)
    return tuple(out)


def ok(expr):
    return parse(expr) is not None


def matches(expr, tm=None):
    """这个时刻该触发吗（分钟级）。"""
    p = parse(expr)
    if not p:
        return False
    tm = tm or time.localtime()
    mi, ho, dom, mo, dow = p
    if tm.tm_min not in mi or tm.tm_hour not in ho or tm.tm_mon not in mo:
        return False
    wd = (tm.tm_wday + 1) % 7          # 周一=0 → cron 的周日=0
    dom_any, dow_any = len(dom) == 31, len(dow) == 7
    if dom_any and dow_any:
        return True
    if dom_any:
        return wd in dow
    if dow_any:
        return tm.tm_mday in dom
    # 日和星期都限定：cron 的语义是「或」（不是「且」）
    return (tm.tm_mday in dom) or (wd in dow)


def next_run(expr, now=None, limit_days=400):
    """下次触发的时间戳；表达式非法或 400 天内不会触发返回 None。

    按「天 → 时 → 分」推进，不做逐分钟扫描（逐分钟扫 400 天要 57 万次循环）。
    每天的起点必须用 **mktime 算当地时间零点**：`t - t % 86400` 得到的是
    UTC 零点，在东八区会整体偏 8 小时（实测踩到：「每天 3 点」算成了 11 点）。
    """
    p = parse(expr)
    if not p:
        return None
    mi, ho, dom, mo, dow = p
    t = int(now or time.time())
    tm = time.localtime(t)
    day = int(time.mktime((tm.tm_year, tm.tm_mon, tm.tm_mday, 0, 0, 0, 0, 0, -1)))
    for _ in range(limit_days):
        dt = time.localtime(day)
        wd = (dt.tm_wday + 1) % 7
        dom_any, dow_any = len(dom) == 31, len(dow) == 7
        if dom_any and dow_any:
            day_ok = True
        elif dom_any:
            day_ok = wd in dow
        elif dow_any:
            day_ok = dt.tm_mday in dom
        else:
            day_ok = (dt.tm_mday in dom) or (wd in dow)
        if day_ok and dt.tm_mon in mo:
            for h in sorted(ho):
                for m in sorted(mi):
                    cand = day + h * 3600 + m * 60
                    if cand > t:
                        return cand
        nxt = time.localtime(day + 86400)   # 每天重新用 mktime 归一化，跨夏令时不跑偏
        day = int(time.mktime((nxt.tm_year, nxt.tm_mon, nxt.tm_mday, 0, 0, 0, 0, 0, -1)))
    return None


def describe(expr):
    """给前端用的一句人话（认不出来就原样回表达式）。"""
    e = (expr or "").strip()
    for pat, label in PRESETS:
        if e == pat:
            return label
    return e or "未设置"
