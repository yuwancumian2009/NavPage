#!/usr/bin/env python3
"""生成通知卡片配图（静态资源）。

为什么用脚本生成而不是找网图：
  * 企业微信图文消息的 picurl 必须是**能从公网访问**的图片，放本地面板静态目录最省事；
  * 每类事件要一眼区分（颜色 + 图形 + 一行中文），生成脚本改色改字都方便，也能进仓库复现。

用法：python3 tools/gen_notify_icons.py     # 输出到 static/notify/*.png (640x320)
字体：优先 Noto Sans CJK（容器里通常没有，开发机上有），退回文泉驿正黑。
"""
import os
from PIL import Image, ImageDraw, ImageFont

W, H = 640, 320
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "notify")

FONT_CANDIDATES = [
    "/home/hermes/.hermes/assets/fonts/NotoSansCJKsc-Bold.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]


def font(size):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def gradient(c1, c2):
    img = Image.new("RGB", (W, H), c1)
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        d.line([(0, y), (W, y)], fill=tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)))
    return img


def stripes(img):
    """右上角几条斜线，纯装饰，避免大片纯色显得空。"""
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for i in range(6):
        x = W - 40 - i * 46
        d.line([(x, 0), (x + 150, H)], fill=(255, 255, 255, 16), width=18)
    return Image.alpha_composite(img.convert("RGBA"), ov)


# ---------------------------------------------------------------- 图形（都以 (cx, cy) 为中心，白色描边）
def g_check(d, cx, cy, s=1.0):
    d.line([(cx - 34 * s, cy + 2 * s), (cx - 8 * s, cy + 28 * s), (cx + 36 * s, cy - 30 * s)],
           fill="white", width=int(16 * s), joint="curve")


def g_cross(d, cx, cy, s=1.0):
    for a, b in (((cx - 26 * s, cy - 26 * s), (cx + 26 * s, cy + 26 * s)),
                 ((cx + 26 * s, cy - 26 * s), (cx - 26 * s, cy + 26 * s))):
        d.line([a, b], fill="white", width=int(16 * s))


def g_trash(d, cx, cy, s=1.0):
    d.rounded_rectangle([cx - 30 * s, cy - 22 * s, cx + 30 * s, cy + 38 * s], radius=8 * s,
                        outline="white", width=int(10 * s))
    d.line([(cx - 40 * s, cy - 30 * s), (cx + 40 * s, cy - 30 * s)], fill="white", width=int(10 * s))
    d.line([(cx - 12 * s, cy - 40 * s), (cx + 12 * s, cy - 40 * s)], fill="white", width=int(10 * s))
    for dx in (-14, 0, 14):
        d.line([(cx + dx * s, cy - 4 * s), (cx + dx * s, cy + 22 * s)], fill="white", width=int(7 * s))


def g_link(d, cx, cy, broken=False, s=1.0):
    """链环：broken=True 时中间留缺口并画个断点。"""
    gap = 16 if broken else 4
    d.rounded_rectangle([cx - 52 * s, cy - 14 * s, cx - gap * s, cy + 14 * s], radius=14 * s,
                        outline="white", width=int(11 * s))
    d.rounded_rectangle([cx + gap * s, cy - 14 * s, cx + 52 * s, cy + 14 * s], radius=14 * s,
                        outline="white", width=int(11 * s))
    if broken:
        d.line([(cx - 4 * s, cy - 26 * s), (cx + 10 * s, cy - 10 * s)], fill="white", width=int(8 * s))
        d.line([(cx + 10 * s, cy + 26 * s), (cx - 4 * s, cy + 10 * s)], fill="white", width=int(8 * s))


def g_tunnel(d, cx, cy, down=False, s=1.0):
    """隧道/通道：三条横向管道。

    正常 → 管道连贯、末端箭头（数据流出去）；
    掉线 → 管道中间断开、加一道斜杠（明确的「断开」语义，比原来的抽象符号好认）。
    """
    for i, dy in enumerate((-24, 0, 24)):
        y = cy + dy * s
        x0, x1 = cx - 54 * s, cx + 44 * s
        w = int(9 * s)
        if down:
            d.line([(x0, y), (cx - 16 * s, y)], fill="white", width=w)
            d.line([(cx + 16 * s, y), (x1, y)], fill="white", width=w)
        else:
            d.line([(x0, y), (x1, y)], fill="white", width=w)
    if down:
        d.line([(cx - 26 * s, cy + 34 * s), (cx + 26 * s, cy - 34 * s)], fill="white", width=int(11 * s))
    else:
        d.polygon([(cx + 60 * s, cy), (cx + 40 * s, cy - 15 * s), (cx + 40 * s, cy + 15 * s)], fill="white")


def g_cert(d, cx, cy, s=1.0):
    d.rounded_rectangle([cx - 40 * s, cy - 44 * s, cx + 40 * s, cy + 22 * s], radius=8 * s,
                        outline="white", width=int(10 * s))
    d.line([(cx - 24 * s, cy - 24 * s), (cx + 24 * s, cy - 24 * s)], fill="white", width=int(7 * s))
    d.line([(cx - 24 * s, cy - 6 * s), (cx + 6 * s, cy - 6 * s)], fill="white", width=int(7 * s))
    d.ellipse([cx - 14 * s, cy + 14 * s, cx + 14 * s, cy + 42 * s], fill="white")
    d.line([(cx - 8 * s, cy + 36 * s), (cx - 14 * s, cy + 52 * s)], fill="white", width=int(7 * s))
    d.line([(cx + 8 * s, cy + 36 * s), (cx + 14 * s, cy + 52 * s)], fill="white", width=int(7 * s))


def g_plane(d, cx, cy, s=1.0):
    d.polygon([(cx - 44 * s, cy - 24 * s), (cx + 46 * s, cy), (cx - 44 * s, cy + 24 * s),
               (cx - 28 * s, cy), ], fill="white")


CARDS = [
    # key, 主色, 副色, 大标题, 副标题, 图形
    ("update_ok",     (21, 128, 61),  (34, 197, 94),  "容器更新成功", "镜像已重建并启动",      g_check),
    ("update_fail",   (185, 28, 28),  (239, 68, 68),  "容器更新失败", "需要人工看一眼",        g_cross),
    ("prune",         (12, 74, 110),  (14, 165, 233), "镜像清理完成", "磁盘空间已回收",        g_trash),
    ("node_down",     (154, 52, 18),  (249, 115, 22), "远程节点不通", "frp 服务端连不上",      lambda d, x, y: g_link(d, x, y, broken=True)),
    ("node_up",       (6, 95, 70),    (16, 185, 129), "远程节点恢复", "隧道端口重新可达",      g_link),
    ("frp_down",      (136, 19, 55),  (225, 29, 72),  "frp 掉线",     "外网地址暂时不可用",    lambda d, x, y: g_tunnel(d, x, y, down=True)),
    ("frp_up",        (15, 118, 110), (13, 148, 136), "frp 已恢复",   "隧道重新注册完成",      g_tunnel),
    ("cert_expiring", (146, 64, 14),  (245, 158, 11), "证书即将过期", "尽快确认续期",          g_cert),
    ("test",          (51, 65, 85),   (100, 116, 139), "通知测试",    "企业微信应用已接通",    g_plane),
]


def main():
    os.makedirs(OUT, exist_ok=True)
    for key, c1, c2, title, sub, glyph in CARDS:
        img = stripes(gradient(c1, c2))
        d = ImageDraw.Draw(img)
        # 左侧圆底：必须先 alpha_composite 再画白色图形，
        # 否则 ImageDraw 在半透明像素上直接覆盖，转 RGB 后圆变纯白、白图形就看不见了
        cx, cy = 118, H // 2
        ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(ov).ellipse([cx - 78, cy - 78, cx + 78, cy + 78], fill=(255, 255, 255, 46))
        img = Image.alpha_composite(img, ov)
        d = ImageDraw.Draw(img)
        glyph(d, cx, cy)
        # 文字
        d.text((232, 120), title, font=font(46), fill="white")
        d.text((234, 186), sub, font=font(24), fill=(255, 255, 255, 215))
        # 底部细条：同样先合成再画，避免 alpha 被吞
        ov2 = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(ov2).rectangle([0, H - 10, W, H], fill=(255, 255, 255, 70))
        img = Image.alpha_composite(img, ov2)
        path = os.path.join(OUT, key + ".png")
        img.convert("RGB").save(path, "PNG", optimize=True)
        print("wrote", path)


if __name__ == "__main__":
    main()
