"""UI 图标构建工具（开发期，运行时不用）：lucide SVG → 16/22px 白线 PNG。

GUI 的 ``gui/assets/ui-*.png`` 全部由本脚本从 lucide（ISC 许可，与 cc-switch 同一图标库）
生成，文件名按「角色」命名、内容按下方 ``ICONS`` 映射——换图标只改映射表重跑，
代码里的 ``ui_icon(master, "<角色>")`` 不动。Tk 8.6 不读 SVG，故构建期光栅化；
运行时仍零依赖。需要 Pillow 与联网（从 unpkg 取固定版本的 lucide-static）。

用法::

    python scripts/build_icons.py            # 生成全部
    python scripts/build_icons.py copy x     # 只生成指定角色

光栅化支持 lucide 用到的 SVG 子集（path 全部命令、line、circle、ellipse、rect、
polyline、polygon；fill=none、圆头圆角描边）：超采样画布上把子路径展平成折线，
按笔宽画线段 + 顶点圆盘，再 LANCZOS 缩到目标尺寸。
"""
from __future__ import annotations

import math
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw

LUCIDE_VERSION = "0.542.0"
_CDN = "https://unpkg.com/lucide-static@{v}/icons/{name}.svg"
ASSETS = Path(__file__).resolve().parent.parent / "TCER" / "gui" / "assets"
SIZES = (16, 22)
WHITE = (255, 255, 255)
FLAG_ON = (216, 80, 80)    # 红旗选中态（与旧图标同色）
PIN_ON = (53, 85, 180)     # 置顶选中态

# 角色 → (lucide 图标名, 颜色)
ICONS: dict[str, tuple[str, tuple[int, int, int]]] = {
    "book": ("book-open", WHITE),
    "calendar": ("calendar", WHITE),
    "chat": ("messages-square", WHITE),        # 用户消息（双气泡）
    "session": ("message-square", WHITE),      # 会话（单气泡）
    "view-session": ("messages-square", WHITE),
    "view-project": ("folder-code", WHITE),
    "project": ("code-xml", WHITE),
    "compare": ("arrow-left-right", WHITE),
    "copy": ("copy", WHITE),
    "cost": ("circle-dollar-sign", WHITE),
    "crosscheck": ("check-check", WHITE),
    "dashboard": ("gauge", WHITE),
    "empty": ("inbox", WHITE),
    "export": ("download", WHITE),
    "upload": ("cloud-upload", WHITE),
    "flag": ("flag", WHITE),
    "flag-on": ("flag", FLAG_ON),
    "pin": ("pin", WHITE),
    "pin-on": ("pin", PIN_ON),
    "folder": ("folder-open", WHITE),
    "grid": ("layout-grid", WHITE),
    "heatmap": ("grid-3x3", WHITE),
    "scatter": ("chart-scatter", WHITE),
    "layers": ("layers", WHITE),
    "model": ("bot", WHITE),
    "plus": ("circle-plus", WHITE),
    "rank": ("trophy", WHITE),
    "refresh": ("refresh-cw", WHITE),
    "save": ("save", WHITE),
    "search": ("search", WHITE),
    "settings": ("settings", WHITE),
    "sparkle": ("sparkles", WHITE),
    "stop": ("circle-stop", WHITE),
    "target": ("target", WHITE),
    "tools": ("hammer", WHITE),
    "wrench": ("wrench", WHITE),
    "trash": ("trash-2", WHITE),
    "trend": ("trending-up", WHITE),
    "zoom": ("zoom-in", WHITE),
    # 会话记录阅读器
    "chevron-left": ("chevron-left", WHITE),
    "chevron-right": ("chevron-right", WHITE),
    "chevron-down": ("chevron-down", WHITE),
    "arrow-up": ("arrow-up", WHITE),
    "arrow-down": ("arrow-down", WHITE),
    "latest": ("arrow-down-to-line", WHITE),
    "panel-right": ("panel-right", WHITE),
    "sliders": ("sliders-horizontal", WHITE),
    "close": ("x", WHITE),
    "clock": ("clock", WHITE),
    "terminal": ("terminal", WHITE),
    "diff": ("file-diff", WHITE),
    "more": ("ellipsis", WHITE),
    "play": ("play", WHITE),
    "filter": ("filter", WHITE),
    "outline": ("list-tree", WHITE),
    "alert": ("circle-alert", WHITE),
    "expand-all": ("chevrons-up-down", WHITE),
    "collapse-all": ("chevrons-down-up", WHITE),
    "external": ("external-link", WHITE),
    "info": ("info", WHITE),
}

SS = 16  # 超采样倍数
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_TOK = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _bez(p0, p1, p2, p3, n=24):
    out = []
    for i in range(1, n + 1):
        t = i / n
        mt = 1 - t
        out.append((mt**3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t**3 * p3[0],
                    mt**3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t**3 * p3[1]))
    return out


def _quad(p0, p1, p2, n=20):
    out = []
    for i in range(1, n + 1):
        t = i / n
        mt = 1 - t
        out.append((mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0],
                    mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1]))
    return out


def _arc(p0, rx, ry, phi, large, sweep, p1):
    if rx == 0 or ry == 0:
        return [p1]
    x1, y1 = p0
    x2, y2 = p1
    cp, sp = math.cos(math.radians(phi)), math.sin(math.radians(phi))
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    rx, ry = abs(rx), abs(ry)
    lam = x1p**2 / rx**2 + y1p**2 / ry**2
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        co = -co
    cxp, cyp = co * rx * y1p / ry, -co * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (x1 + x2) / 2
    cy = sp * cxp + cp * cyp + (y1 + y2) / 2

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a
    t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    n = max(8, int(abs(dt) / (math.pi / 32)))
    out = []
    for i in range(1, n + 1):
        t = t1 + dt * i / n
        x, y = rx * math.cos(t), ry * math.sin(t)
        out.append((cp * x - sp * y + cx, sp * x + cp * y + cy))
    return out


def parse_path(d):
    toks = _TOK.findall(d)
    i = 0
    subs = []      # list of (points, closed)
    cur = None
    pos = (0.0, 0.0)
    start = pos
    cmd = None
    last_ctrl = None
    last_cmd = None

    def nums(k):
        nonlocal i
        vals = [float(toks[i + j]) for j in range(k)]
        i += k
        return vals
    while i < len(toks):
        if re.match(r"[A-Za-z]", toks[i]):
            cmd = toks[i]
            i += 1
            if cmd in "Zz":
                if cur:
                    subs.append((cur, True))
                cur = None
                pos = start
                last_cmd = cmd
                continue
        rel = cmd.islower()
        C = cmd.upper()
        ox, oy = pos if rel else (0.0, 0.0)
        if C == "M":
            x, y = nums(2)
            pos = (ox + x, oy + y)
            if cur:
                subs.append((cur, False))
            cur = [pos]
            start = pos
            cmd = "l" if rel else "L"
            last_ctrl = None
        elif C == "L":
            x, y = nums(2)
            pos = (ox + x, oy + y)
            cur.append(pos)
            last_ctrl = None
        elif C == "H":
            (x,) = nums(1)
            pos = ((pos[0] + x) if rel else x, pos[1])
            cur.append(pos)
            last_ctrl = None
        elif C == "V":
            (y,) = nums(1)
            pos = (pos[0], (pos[1] + y) if rel else y)
            cur.append(pos)
            last_ctrl = None
        elif C == "C":
            a = nums(6)
            p1 = (ox + a[0], oy + a[1]); p2 = (ox + a[2], oy + a[3]); p3 = (ox + a[4], oy + a[5])
            cur.extend(_bez(pos, p1, p2, p3))
            last_ctrl = ("c", p2)
            pos = p3
        elif C == "S":
            a = nums(4)
            p1 = (2 * pos[0] - last_ctrl[1][0], 2 * pos[1] - last_ctrl[1][1]) if last_ctrl and last_ctrl[0] == "c" else pos
            p2 = (ox + a[0], oy + a[1]); p3 = (ox + a[2], oy + a[3])
            cur.extend(_bez(pos, p1, p2, p3))
            last_ctrl = ("c", p2)
            pos = p3
        elif C == "Q":
            a = nums(4)
            p1 = (ox + a[0], oy + a[1]); p2 = (ox + a[2], oy + a[3])
            cur.extend(_quad(pos, p1, p2))
            last_ctrl = ("q", p1)
            pos = p2
        elif C == "T":
            a = nums(2)
            p1 = (2 * pos[0] - last_ctrl[1][0], 2 * pos[1] - last_ctrl[1][1]) if last_ctrl and last_ctrl[0] == "q" else pos
            p2 = (ox + a[0], oy + a[1])
            cur.extend(_quad(pos, p1, p2))
            last_ctrl = ("q", p1)
            pos = p2
        elif C == "A":
            # 旗标可能无分隔紧挨（如 "0 0 1"/"011"）——lucide 输出均有空格，按数字取
            rx, ry, phi, la, sw, x, y = nums(7)
            p1 = (ox + x, oy + y)
            cur.extend(_arc(pos, rx, ry, phi, int(la), int(sw), p1))
            pos = p1
            last_ctrl = None
        last_cmd = C
    if cur:
        subs.append((cur, False))
    return subs


def _f(el, k, d=0.0):
    v = el.get(k)
    return float(v) if v not in (None, "") else d


def shapes(svg_text):
    root = ET.fromstring(svg_text)
    out = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag == "path":
            out.extend(parse_path(el.get("d", "")))
        elif tag == "line":
            out.append(([(_f(el, "x1"), _f(el, "y1")), (_f(el, "x2"), _f(el, "y2"))], False))
        elif tag in ("circle", "ellipse"):
            cx, cy = _f(el, "cx"), _f(el, "cy")
            rx = _f(el, "r") if tag == "circle" else _f(el, "rx")
            ry = _f(el, "r") if tag == "circle" else _f(el, "ry")
            n = 96
            out.append(([(cx + rx * math.cos(2 * math.pi * k / n), cy + ry * math.sin(2 * math.pi * k / n)) for k in range(n)], True))
        elif tag == "rect":
            x, y, w, h = _f(el, "x"), _f(el, "y"), _f(el, "width"), _f(el, "height")
            rx = _f(el, "rx", _f(el, "ry", 0.0))
            ry = _f(el, "ry", rx)
            rx, ry = min(rx, w / 2), min(ry, h / 2)
            if rx <= 0:
                out.append(([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], True))
            else:
                pts = []
                for (cx, cy, a0) in ((x + w - rx, y + ry, -90), (x + w - rx, y + h - ry, 0),
                                     (x + rx, y + h - ry, 90), (x + rx, y + ry, 180)):
                    for k in range(13):
                        t = math.radians(a0 + 90 * k / 12)
                        pts.append((cx + rx * math.cos(t), cy + ry * math.sin(t)))
                out.append((pts, True))
        elif tag in ("polyline", "polygon"):
            v = [float(n) for n in _NUM.findall(el.get("points", ""))]
            pts = list(zip(v[0::2], v[1::2]))
            out.append((pts, tag == "polygon"))
    return out


def render(svg_text, size, stroke_scale=1.0, color=(255, 255, 255)):
    big = size * SS
    k = big / 24.0
    w = 2.0 * stroke_scale * k
    img = Image.new("L", (big, big), 0)
    dr = ImageDraw.Draw(img)
    r = w / 2
    for pts, closed in shapes(svg_text):
        P = [(x * k, y * k) for x, y in pts]
        if closed and P:
            P = P + [P[0]]
        if len(P) == 1:
            x, y = P[0]
            dr.ellipse((x - r, y - r, x + r, y + r), fill=255)
            continue
        for a, b in zip(P, P[1:]):
            dr.line([a, b], fill=255, width=max(1, int(round(w))))
        for x, y in P:
            dr.ellipse((x - r, y - r, x + r, y + r), fill=255)
    alpha = img.resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), color + (0,))
    out.putalpha(alpha)
    return out



def fetch(name: str) -> str:
    url = _CDN.format(v=LUCIDE_VERSION, name=name)
    with urllib.request.urlopen(url, timeout=30) as resp:
        text = resp.read().decode("utf-8")
    if "<svg" not in text:
        raise ValueError(f"{name}: 非 SVG 响应")
    return text


def main(argv: list[str]) -> int:
    roles = argv or list(ICONS)
    cache: dict[str, str] = {}
    for role in roles:
        name, color = ICONS[role]
        svg = cache.get(name) or cache.setdefault(name, fetch(name))
        for size in SIZES:
            suffix = "" if size == 16 else f"-{size}"
            render(svg, size, color=color).save(ASSETS / f"ui-{role}{suffix}.png")
        print(f"ui-{role}  <-  lucide/{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
