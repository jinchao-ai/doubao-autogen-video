"""verifier.py — 程序化结果校验（零 LLM）。

移植自已验证的 PIL 分区色板统计：
- 尺寸 / 9:16 比例带
- 上/中/下三分区 warm/dark/blue/green 占比
- 可选镜头期望阈值 expect: {"max_dark_top":0.9, "min_warm_bottom":0.2, ...}
"""
from __future__ import annotations

import colorsys
from pathlib import Path

from PIL import Image


def _zone_stats(img: Image.Image, box) -> dict:
    z = img.crop(box).resize((32, 32))
    warm = dark = blue = green = 0
    total = 32 * 32
    for p in list(z.getdata()):
        r, g, b = p[:3]
        mx, mn = max(p[:3]), min(p[:3])
        v = mx / 255
        s = (mx - mn) / mx if mx else 0
        h = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)[0] * 360
        if v < 0.3:
            dark += 1
        elif h < 70 and s > 0.25:
            warm += 1
        elif 160 < h < 260 and s > 0.15:
            blue += 1
        elif 70 <= h <= 160 and s > 0.15:
            green += 1
    return {"warm": round(warm / total, 2), "dark": round(dark / total, 2),
            "blue": round(blue / total, 2), "green": round(green / total, 2)}


def verify_image(path: str, expect: dict | None = None) -> dict:
    """返回 {"pass": bool, "report": {...}, "reasons": [...]}"""
    reasons = []
    fp = Path(path)
    if not fp.exists() or fp.stat().st_size < 10_000:
        return {"pass": False, "report": {"size": 0},
                "reasons": ["file missing or too small"]}

    img = Image.open(fp).convert("RGB")
    w, h = img.size
    ratio = w / h
    if not (0.45 <= ratio <= 0.65):  # 9:16 = 0.5625，留容差带
        reasons.append(f"aspect ratio {ratio:.2f} out of 9:16 band")

    zones = {}
    for r in range(3):
        box = (0, r * h // 3, w, (r + 1) * h // 3 if r < 2 else h)
        zones[f"row{r}"] = _zone_stats(img, box)

    expect = expect or {}
    for key, threshold in expect.items():
        # key 形如 max_dark_top / min_warm_bottom → 方向_指标_区
        direction, metric, row = key.split("_", 2)
        actual = zones[_row_name(row)][metric]
        if direction == "max" and actual > threshold:
            reasons.append(f"{key}: {actual} > {threshold}")
        if direction == "min" and actual < threshold:
            reasons.append(f"{key}: {actual} < {threshold}")

    return {"pass": not reasons, "report": {"size": [w, h], "zones": zones},
            "reasons": reasons}


def _row_name(row: str) -> str:
    return {"top": "row0", "mid": "row1", "bottom": "row2"}.get(row, row)


def verify_video(path: str) -> dict:
    """视频基础校验：落盘 + 大小（结构化校验 M3 再加）。"""
    fp = Path(path)
    ok = fp.exists() and fp.stat().st_size > 200_000
    return {"pass": bool(ok), "report": {"bytes": fp.stat().st_size if fp.exists() else 0},
            "reasons": [] if ok else ["video file missing or too small"]}
