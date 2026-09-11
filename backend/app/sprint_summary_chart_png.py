# -*- coding: utf-8 -*-
"""PNG charts for Sprint 总结邮件。飞书等客户端会剥掉内嵌 SVG。"""
from __future__ import annotations

from io import BytesIO
from typing import Any

from .sprint_summary_charts import (
    _COL_MID,
    _LINE_X,
    _Y0,
    _fmt_n,
    _fmt_pct,
    _labels,
    _num,
    _y,
)

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


def _hex(color: str) -> tuple[int, int, int]:
    c = (color or "#000000").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _load_font(size: int):
    from PIL import ImageFont

    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


class _Png:
    def __init__(self, width: int, height: int, scale: int = 2) -> None:
        from PIL import Image, ImageDraw

        self.s = scale
        self.im = Image.new("RGB", (width * scale, height * scale), (255, 255, 255))
        self.d = ImageDraw.Draw(self.im)
        self._fonts: dict[int, Any] = {}

    def _xy(self, x: float, y: float) -> tuple[float, float]:
        return (x * self.s, y * self.s)

    def font(self, size: int):
        px = max(10, int(round(size * self.s)))
        if px not in self._fonts:
            self._fonts[px] = _load_font(px)
        return self._fonts[px]

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        color: str,
        width: float = 1,
        dash: tuple[int, int] | None = None,
    ) -> None:
        w = max(1, int(round(width * self.s)))
        fill = _hex(color)
        if dash and abs(y2 - y1) < 0.5:
            on, off = dash
            x = x1
            while x < x2:
                x_end = min(x2, x + on)
                self.d.line([self._xy(x, y1), self._xy(x_end, y1)], fill=fill, width=w)
                x += on + off
            return
        self.d.line([self._xy(x1, y1), self._xy(x2, y2)], fill=fill, width=w)

    def polyline(self, pts: list[tuple[float, float]], color: str, width: float = 2) -> None:
        if len(pts) < 2:
            return
        xy = [self._xy(x, y) for x, y in pts]
        self.d.line(xy, fill=_hex(color), width=max(1, int(round(width * self.s))))

    def circle(self, x: float, y: float, r: float, color: str) -> None:
        cx, cy = self._xy(x, y)
        rr = r * self.s
        self.d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=_hex(color))

    def rect(
        self, x: float, y: float, w: float, h: float, color: str, radius: float = 3
    ) -> None:
        x0, y0 = self._xy(x, y)
        x1, y1 = self._xy(x + w, y + h)
        rad = max(1, int(round(radius * self.s)))
        self.d.rounded_rectangle((x0, y0, x1, y1), radius=rad, fill=_hex(color))

    def text(
        self,
        x: float,
        y: float,
        s: str,
        color: str,
        size: int = 10,
        align: str = "middle",
    ) -> None:
        anchor = {"middle": "ms", "end": "rs", "start": "ls"}.get(align, "ls")
        self.d.text(
            self._xy(x, y),
            s,
            fill=_hex(color),
            font=self.font(size),
            anchor=anchor,
        )

    def png(self) -> bytes:
        buf = BytesIO()
        self.im.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


def _axis(p: _Png) -> None:
    p.line(44, 204, 464, 204, "#e5e5e5")
    p.line(44, 16, 44, 204, "#e5e5e5")


def delivery_trend_png(history: list[dict[str, Any]]) -> bytes:
    if len(history) < 2:
        return b""
    xs = _LINE_X[-len(history) :]
    labels = _labels(history)
    acc = [_num(h.get("acceptRate")) or 0.0 for h in history]
    pts = [_num(h.get("pointRate")) or 0.0 for h in history]
    y80 = _y(80, 100)
    p = _Png(480, 240)
    _axis(p)
    p.line(44, y80, 464, y80, "#94a3b8", dash=(4, 4))
    p.text(468, y80 + 4, "80%", "#64748b", 10, "start")
    p.text(36, 208, "0", "#64748b", 10, "end")
    p.text(36, 20, "100", "#64748b", 10, "end")
    for x, lab in zip(xs, labels):
        p.text(x, 222, lab, "#64748b", 11)
    acc_xy = [(x, _y(v, 100)) for x, v in zip(xs, acc)]
    pt_xy = [(x, _y(v, 100)) for x, v in zip(xs, pts)]
    p.polyline(acc_xy, "#d97706", 2)
    p.polyline(pt_xy, "#059669", 2)
    for (x, y), v in zip(acc_xy, acc):
        ty = y - 8 if y > 28 else y + 14
        p.circle(x, y, 3.5, "#d97706")
        p.text(x, ty, _fmt_pct(v), "#d97706", 10)
    for (x, y), v in zip(pt_xy, pts):
        ty = y - 8 if y > 28 else y + 14
        p.circle(x, y, 3.5, "#059669")
        p.text(x, ty, _fmt_pct(v), "#059669", 10)
    return p.png()


def bug_found_fixed_png(history: list[dict[str, Any]]) -> bytes:
    if len(history) < 2:
        return b""
    mids = _COL_MID[-len(history) :]
    labels = _labels(history)
    found = [_num(h.get("bugFound")) or 0.0 for h in history]
    fixed = [_num(h.get("bugFixed")) or 0.0 for h in history]
    rates = [_num(h.get("bugFixRate")) or 0.0 for h in history]
    vmax = max(250.0, max(found + fixed + [1.0]) * 1.05)
    p = _Png(510, 240)
    p.line(44, 204, 464, 204, "#e5e5e5")
    p.line(44, 16, 44, 204, "#e5e5e5")
    p.line(464, 16, 464, 204, "#e5e5e5")
    p.text(36, 208, "0", "#737373", 10, "end")
    p.text(36, 116, str(int(round(vmax / 2))), "#737373", 10, "end")
    p.text(36, 20, str(int(round(vmax))), "#737373", 10, "end")
    p.text(470, 20, "100%", "#d97706", 10, "start")
    p.text(470, 116, "50%", "#d97706", 10, "start")
    p.text(470, 208, "0%", "#d97706", 10, "start")
    rate_xy: list[tuple[float, float]] = []
    for mid, lab, f, d, r in zip(mids, labels, found, fixed, rates):
        yf = _y(f, vmax)
        yd = _y(d, vmax)
        p.rect(mid - 30, yf, 28, max(2.0, _Y0 - yf), "#3b82f6")
        p.rect(mid + 2, yd, 28, max(2.0, _Y0 - yd), "#059669")
        p.text(mid - 16, max(20, yf - 6), _fmt_n(f, 0), "#737373", 10)
        p.text(mid + 16, max(20, yd - 6), _fmt_n(d, 0), "#737373", 10)
        p.text(mid, 222, lab, "#737373", 11)
        rate_xy.append((mid, _y(r, 100)))
    p.polyline(rate_xy, "#d97706", 2)
    for (mid, ry), r in zip(rate_xy, rates):
        p.circle(mid, ry, 3.5, "#d97706")
        p.text(mid, max(20, ry - 8), _fmt_pct(r, 2), "#d97706", 10)
    return p.png()


def role_points_png(history: list[dict[str, Any]]) -> bytes:
    if len(history) < 2:
        return b""
    mids = _COL_MID[-len(history) :]
    labels = _labels(history)
    dev = [_num(h.get("doneDev")) or 0.0 for h in history]
    qc = [_num(h.get("doneQc")) or 0.0 for h in history]
    vmax = max(100.0, max(dev + qc + [1.0]) * 1.15)
    p = _Png(480, 240)
    _axis(p)
    p.text(36, 208, "0", "#737373", 10, "end")
    p.text(36, 116, _fmt_n(vmax / 2, 0), "#737373", 10, "end")
    p.text(36, 20, _fmt_n(vmax, 0), "#737373", 10, "end")
    for mid, lab, d, q in zip(mids, labels, dev, qc):
        yd = _y(d, vmax)
        yq = _y(q, vmax)
        p.rect(mid - 30, yd, 28, max(2.0, _Y0 - yd), "#3b82f6")
        p.rect(mid + 2, yq, 28, max(2.0, _Y0 - yq), "#059669")
        p.text(mid - 16, max(20, yd - 6), _fmt_n(d), "#737373", 10)
        p.text(mid + 16, max(20, yq - 6), _fmt_n(q), "#737373", 10)
        p.text(mid, 222, lab, "#737373", 11)
    return p.png()


def per_capita_png(history: list[dict[str, Any]]) -> bytes:
    if len(history) < 2:
        return b""
    mids = _COL_MID[-len(history) :]
    labels = _labels(history)
    dev_pc: list[float] = []
    qc_pc: list[float] = []
    for h in history:
        d = _num(h.get("doneDev")) or 0.0
        q = _num(h.get("doneQc")) or 0.0
        pd = _num(h.get("peopleDev")) or 0.0
        pq = _num(h.get("peopleQc")) or 0.0
        dev_pc.append(round(d / pd, 2) if pd else 0.0)
        qc_pc.append(round(q / pq, 2) if pq else 0.0)
    vmax = max(6.0, max(dev_pc + qc_pc + [0.1]) * 1.15)
    p = _Png(480, 240)
    _axis(p)
    p.text(36, 208, "0", "#737373", 10, "end")
    p.text(36, 110, _fmt_n(vmax / 2), "#737373", 10, "end")
    p.text(36, 20, _fmt_n(vmax), "#737373", 10, "end")
    for mid, lab, d, q in zip(mids, labels, dev_pc, qc_pc):
        yd = _y(d, vmax)
        yq = _y(q, vmax)
        p.rect(mid - 30, yd, 28, max(2.0, _Y0 - yd), "#3b82f6")
        p.rect(mid + 2, yq, 28, max(2.0, _Y0 - yq), "#059669")
        p.text(mid - 16, max(20, yd - 6), _fmt_n(d, 2), "#737373", 10)
        p.text(mid + 16, max(20, yq - 6), _fmt_n(q, 2), "#737373", 10)
        p.text(mid, 222, lab, "#737373", 11)
    return p.png()


def history_chart_pngs(history: list[dict[str, Any]]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for key, fn in (
        ("delivery", delivery_trend_png),
        ("bugs", bug_found_fixed_png),
        ("points", role_points_png),
        ("capita", per_capita_png),
    ):
        blob = fn(history)
        if blob:
            out[key] = blob
    return out
