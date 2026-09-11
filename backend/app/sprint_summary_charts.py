# -*- coding: utf-8 -*-
"""SVG charts for Sprint 总结报告（口径对齐 Demo 近 4 期图）。"""
from __future__ import annotations

from typing import Any

_LINE_X = (44, 184, 324, 464)
_COL_MID = (96, 206, 316, 426)
_Y0, _Y1 = 204, 16


def _esc(text: Any) -> str:
    return (
        str(text if text is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _y(value: float, vmax: float) -> float:
    if vmax <= 0:
        return _Y0
    ratio = max(0.0, min(1.0, float(value) / vmax))
    return _Y0 - ratio * (_Y0 - _Y1)


def _labels(history: list[dict[str, Any]]) -> list[str]:
    n = len(history)
    out: list[str] = []
    for i in range(n):
        if i == n - 1:
            out.append("本期")
        else:
            out.append(f"−{n - 1 - i}")
    return out


def _num(v: Any) -> float | None:
    if v is None or v == "" or v == "—":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _fmt_pct(v: float | None, digits: int = 1) -> str:
    if v is None:
        return "—"
    if abs(v - round(v)) < 0.05:
        return f"{int(round(v))}%"
    return f"{round(v, digits)}%"


def _fmt_n(v: float | None, digits: int = 1) -> str:
    if v is None:
        return "—"
    if abs(v - round(v)) < 0.05:
        return str(int(round(v)))
    return str(round(v, digits))


def delivery_trend_svg(history: list[dict[str, Any]]) -> str:
    if len(history) < 2:
        return ""
    xs = _LINE_X[-len(history) :]
    labels = _labels(history)
    acc = [_num(h.get("acceptRate")) or 0.0 for h in history]
    pts = [_num(h.get("pointRate")) or 0.0 for h in history]
    y_acc = [_y(v, 100) for v in acc]
    y_pts = [_y(v, 100) for v in pts]
    y80 = _y(80, 100)
    acc_pts = " ".join(f"{x},{y:.1f}" for x, y in zip(xs, y_acc))
    pt_pts = " ".join(f"{x},{y:.1f}" for x, y in zip(xs, y_pts))
    parts = [
        '<svg class="line" width="480" height="240" viewBox="0 0 480 240" role="img" aria-label="近 4 期 Sprint 完成率趋势">',
        '<line x1="44" y1="204" x2="464" y2="204" stroke="#dce6f0" />',
        '<line x1="44" y1="16" x2="44" y2="204" stroke="#dce6f0" />',
        f'<line x1="44" y1="{y80:.1f}" x2="464" y2="{y80:.1f}" stroke="#94a3b8" stroke-dasharray="4 4" />',
        f'<text x="468" y="{y80 + 4:.1f}" font-size="10" fill="#64748b">80%</text>',
        '<text x="36" y="208" font-size="10" fill="#64748b" text-anchor="end">0</text>',
        '<text x="36" y="20" font-size="10" fill="#64748b" text-anchor="end">100</text>',
    ]
    for x, lab in zip(xs, labels):
        parts.append(
            f'<text x="{x}" y="222" font-size="11" fill="#64748b" text-anchor="middle">{_esc(lab)}</text>'
        )
    parts.append(
        f'<polyline fill="none" stroke="#d97706" stroke-width="2" points="{acc_pts}" />'
    )
    for x, y, v in zip(xs, y_acc, acc):
        ty = y - 8 if y > 28 else y + 14
        parts.append(f'<circle cx="{x}" cy="{y:.1f}" r="3.5" fill="#d97706" />')
        parts.append(
            f'<text x="{x}" y="{ty:.1f}" font-size="10" fill="#d97706" text-anchor="middle">{_fmt_pct(v)}</text>'
        )
    parts.append(
        f'<polyline fill="none" stroke="#059669" stroke-width="2" points="{pt_pts}" />'
    )
    for x, y, v in zip(xs, y_pts, pts):
        ty = y - 8 if y > 28 else y + 14
        parts.append(f'<circle cx="{x}" cy="{y:.1f}" r="3.5" fill="#059669" />')
        parts.append(
            f'<text x="{x}" y="{ty:.1f}" font-size="10" fill="#059669" text-anchor="middle">{_fmt_pct(v)}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


def bug_found_fixed_svg(history: list[dict[str, Any]]) -> str:
    if len(history) < 2:
        return ""
    mids = _COL_MID[-len(history) :]
    labels = _labels(history)
    found = [_num(h.get("bugFound")) or 0.0 for h in history]
    fixed = [_num(h.get("bugFixed")) or 0.0 for h in history]
    rates = [_num(h.get("bugFixRate")) or 0.0 for h in history]
    vmax = max(250.0, max(found + fixed + [1.0]) * 1.05)
    parts = [
        '<svg class="col" width="510" height="240" viewBox="0 0 510 240" role="img" aria-label="近 4 期缺陷发现、修复与修复率">',
        '<line x1="44" y1="204" x2="464" y2="204" stroke="#e5e5e5" />',
        '<line x1="44" y1="16" x2="44" y2="204" stroke="#e5e5e5" />',
        '<line x1="464" y1="16" x2="464" y2="204" stroke="#e5e5e5" />',
        '<text x="36" y="208" font-size="10" fill="#737373" text-anchor="end">0</text>',
        f'<text x="36" y="116" font-size="10" fill="#737373" text-anchor="end">{int(round(vmax / 2))}</text>',
        f'<text x="36" y="20" font-size="10" fill="#737373" text-anchor="end">{int(round(vmax))}</text>',
        '<text x="470" y="20" font-size="10" fill="#d97706">100%</text>',
        '<text x="470" y="116" font-size="10" fill="#d97706">50%</text>',
        '<text x="470" y="208" font-size="10" fill="#d97706">0%</text>',
    ]
    rate_pts: list[str] = []
    for mid, lab, f, d, r in zip(mids, labels, found, fixed, rates):
        yf = _y(f, vmax)
        yd = _y(d, vmax)
        hf = max(2.0, _Y0 - yf)
        hd = max(2.0, _Y0 - yd)
        parts.append(
            f'<rect x="{mid - 30}" y="{yf:.1f}" width="28" height="{hf:.1f}" rx="3" fill="#3b82f6" />'
        )
        parts.append(
            f'<rect x="{mid + 2}" y="{yd:.1f}" width="28" height="{hd:.1f}" rx="3" fill="#059669" />'
        )
        parts.append(
            f'<text x="{mid - 16}" y="{max(20, yf - 6):.1f}" font-size="10" fill="#737373" text-anchor="middle">{_fmt_n(f, 0)}</text>'
        )
        parts.append(
            f'<text x="{mid + 16}" y="{max(20, yd - 6):.1f}" font-size="10" fill="#737373" text-anchor="middle">{_fmt_n(d, 0)}</text>'
        )
        parts.append(
            f'<text x="{mid}" y="222" font-size="11" fill="#737373" text-anchor="middle">{_esc(lab)}</text>'
        )
        ry = _y(r, 100)
        rate_pts.append(f"{mid},{ry:.1f}")
    parts.append(
        f'<polyline fill="none" stroke="#d97706" stroke-width="2" points="{" ".join(rate_pts)}" />'
    )
    for mid, r in zip(mids, rates):
        ry = _y(r, 100)
        parts.append(f'<circle cx="{mid}" cy="{ry:.1f}" r="3.5" fill="#d97706" />')
        parts.append(
            f'<text x="{mid}" y="{max(20, ry - 8):.1f}" font-size="10" fill="#d97706" text-anchor="middle">{_fmt_pct(r, 2)}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


def role_points_svg(history: list[dict[str, Any]]) -> str:
    if len(history) < 2:
        return ""
    mids = _COL_MID[-len(history) :]
    labels = _labels(history)
    dev = [_num(h.get("doneDev")) or 0.0 for h in history]
    qc = [_num(h.get("doneQc")) or 0.0 for h in history]
    vmax = max(100.0, max(dev + qc + [1.0]) * 1.15)
    parts = [
        '<svg class="col" width="480" height="240" viewBox="0 0 480 240" role="img" aria-label="近 4 期 DEV/QC 完成故事点">',
        '<line x1="44" y1="204" x2="464" y2="204" stroke="#e5e5e5" />',
        '<line x1="44" y1="16" x2="44" y2="204" stroke="#e5e5e5" />',
        '<text x="36" y="208" font-size="10" fill="#737373" text-anchor="end">0</text>',
        f'<text x="36" y="116" font-size="10" fill="#737373" text-anchor="end">{_fmt_n(vmax / 2, 0)}</text>',
        f'<text x="36" y="20" font-size="10" fill="#737373" text-anchor="end">{_fmt_n(vmax, 0)}</text>',
    ]
    for mid, lab, d, q in zip(mids, labels, dev, qc):
        yd = _y(d, vmax)
        yq = _y(q, vmax)
        parts.append(
            f'<rect x="{mid - 30}" y="{yd:.1f}" width="28" height="{max(2.0, _Y0 - yd):.1f}" rx="3" fill="#3b82f6" />'
        )
        parts.append(
            f'<rect x="{mid + 2}" y="{yq:.1f}" width="28" height="{max(2.0, _Y0 - yq):.1f}" rx="3" fill="#059669" />'
        )
        parts.append(
            f'<text x="{mid - 16}" y="{max(20, yd - 6):.1f}" font-size="10" fill="#737373" text-anchor="middle">{_fmt_n(d)}</text>'
        )
        parts.append(
            f'<text x="{mid + 16}" y="{max(20, yq - 6):.1f}" font-size="10" fill="#737373" text-anchor="middle">{_fmt_n(q)}</text>'
        )
        parts.append(
            f'<text x="{mid}" y="222" font-size="11" fill="#737373" text-anchor="middle">{_esc(lab)}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


def type_trend_svg(history: list[dict[str, Any]]) -> str:
    if len(history) < 2:
        return ""
    xs = _LINE_X[-len(history) :]
    labels = _labels(history)
    series = (
        ("User Story", "#d97706", [_num((h.get("us") or {}).get("doneRate")) for h in history]),
        ("Task", "#3b82f6", [_num((h.get("task") or {}).get("doneRate")) for h in history]),
        ("Tech Improvement", "#0d9488", [_num((h.get("ti") or {}).get("doneRate")) for h in history]),
    )
    y80 = _y(80, 100)
    parts = [
        '<svg class="line" width="480" height="240" viewBox="0 0 480 240" role="img" aria-label="近 4 期按类型 Item 完成率趋势">',
        '<line x1="44" y1="204" x2="464" y2="204" stroke="#dce6f0" />',
        '<line x1="44" y1="16" x2="44" y2="204" stroke="#dce6f0" />',
        f'<line x1="44" y1="{y80:.1f}" x2="464" y2="{y80:.1f}" stroke="#94a3b8" stroke-dasharray="4 4" />',
        f'<text x="468" y="{y80 + 4:.1f}" font-size="10" fill="#64748b">80%</text>',
        '<text x="36" y="208" font-size="10" fill="#64748b" text-anchor="end">0</text>',
        '<text x="36" y="20" font-size="10" fill="#64748b" text-anchor="end">100</text>',
    ]
    for x, lab in zip(xs, labels):
        parts.append(
            f'<text x="{x}" y="222" font-size="11" fill="#64748b" text-anchor="middle">{_esc(lab)}</text>'
        )
    for _name, color, values in series:
        run: list[tuple[float, float, float]] = []
        for x, v in zip(xs, values):
            if v is None:
                if len(run) >= 2:
                    pts = " ".join(f"{px},{py:.1f}" for px, py, _pv in run)
                    parts.append(
                        f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{pts}" />'
                    )
                for px, py, pv in run:
                    parts.append(f'<circle cx="{px}" cy="{py:.1f}" r="3.5" fill="{color}" />')
                    parts.append(
                        f'<text x="{px}" y="{max(14, py - 8):.1f}" font-size="10" fill="{color}" text-anchor="middle">{_fmt_pct(pv)}</text>'
                    )
                run = []
                continue
            run.append((x, _y(v, 100), v))
        if len(run) >= 2:
            pts = " ".join(f"{px},{py:.1f}" for px, py, _pv in run)
            parts.append(
                f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{pts}" />'
            )
        for px, py, pv in run:
            parts.append(f'<circle cx="{px}" cy="{py:.1f}" r="3.5" fill="{color}" />')
            parts.append(
                f'<text x="{px}" y="{max(14, py - 8):.1f}" font-size="10" fill="{color}" text-anchor="middle">{_fmt_pct(pv)}</text>'
            )
    parts.append("</svg>")
    return "\n".join(parts)


def per_capita_svg(history: list[dict[str, Any]]) -> str:
    if len(history) < 2:
        return ""
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
    parts = [
        '<svg class="col" width="480" height="240" viewBox="0 0 480 240" role="img" aria-label="近 4 期 DEV/QC 人均故事点">',
        '<line x1="44" y1="204" x2="464" y2="204" stroke="#e5e5e5" />',
        '<line x1="44" y1="16" x2="44" y2="204" stroke="#e5e5e5" />',
        '<text x="36" y="208" font-size="10" fill="#737373" text-anchor="end">0</text>',
        f'<text x="36" y="110" font-size="10" fill="#737373" text-anchor="end">{_fmt_n(vmax / 2)}</text>',
        f'<text x="36" y="20" font-size="10" fill="#737373" text-anchor="end">{_fmt_n(vmax)}</text>',
    ]
    for mid, lab, d, q in zip(mids, labels, dev_pc, qc_pc):
        yd = _y(d, vmax)
        yq = _y(q, vmax)
        parts.append(
            f'<rect x="{mid - 30}" y="{yd:.1f}" width="28" height="{max(2.0, _Y0 - yd):.1f}" rx="3" fill="#3b82f6" />'
        )
        parts.append(
            f'<rect x="{mid + 2}" y="{yq:.1f}" width="28" height="{max(2.0, _Y0 - yq):.1f}" rx="3" fill="#059669" />'
        )
        parts.append(
            f'<text x="{mid - 16}" y="{max(20, yd - 6):.1f}" font-size="10" fill="#737373" text-anchor="middle">{_fmt_n(d, 2)}</text>'
        )
        parts.append(
            f'<text x="{mid + 16}" y="{max(20, yq - 6):.1f}" font-size="10" fill="#737373" text-anchor="middle">{_fmt_n(q, 2)}</text>'
        )
        parts.append(
            f'<text x="{mid}" y="222" font-size="11" fill="#737373" text-anchor="middle">{_esc(lab)}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts)
