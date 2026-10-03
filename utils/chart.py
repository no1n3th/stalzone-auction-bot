"""Zero-dependency PNG price chart renderer (zlib + struct, no Pillow).

Produces a compact dark-theme line chart:
- 640x280 @2x (1280x560), antialiased via supersampled drawing on a tiny canvas
- price line + min/max markers + day grid, human-formatted Y labels
- optional horizontal "current lot price" dashed line (red)
Colors follow the redesigned palette (redesign 4.6 / utils/chart).
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path
from collections.abc import Iterable

# palette (matches the web tokens — section 4.6)
BG = (13, 15, 18)  # --bg
GRID = (38, 43, 49)  # --line
LINE = (111, 163, 216)  # --accent
FILL = (23, 30, 38)
TEXT = (160, 168, 176)  # --muted
MIN_P = (93, 187, 138)  # --profit
MAX_P = (229, 100, 107)  # --loss
LOT = (216, 166, 87)  # --warn

W, H = 640, 280  # logical size; PNG is @2x
PAD_L, PAD_R, PAD_T, PAD_B = 64, 16, 20, 28


def _png_pack(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack("!I", len(data))
        + tag
        + data
        + struct.pack("!I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


class _Canvas:
    """Tiny RGB canvas with alpha blending + Wu-line antialiasing."""

    def __init__(self, w: int, h: int, bg: tuple[int, int, int]) -> None:
        self.w, self.h = w, h
        self.px = bytearray(bg * (w * h))

    def _set(self, x: int, y: int, rgb: tuple[int, int, int], a: float) -> None:
        if 0 <= x < self.w and 0 <= y < self.h and a > 0:
            i = (y * self.w + x) * 3
            for c in range(3):
                self.px[i + c] = int(self.px[i + c] * (1 - a) + rgb[c] * a)

    def line(
        self, x0: float, y0: float, x1: float, y1: float, rgb: tuple[int, int, int], width: int = 1
    ) -> None:
        """Xiaolin Wu antialiased line."""
        dx, dy = x1 - x0, y1 - y0
        steps = int(max(abs(dx), abs(dy))) or 1
        for s in range(steps + 1):
            t = s / steps
            x, y = x0 + dx * t, y0 + dy * t
            xi, yi = int(x), int(y)
            fx, fy = x - xi, y - yi
            for ox in (-1, 0, 1):
                for oy in (-1, 0, 1):
                    dist = math.hypot(ox - fx + 0.0, oy - fy + 0.0)
                    a = max(0.0, 1.0 - dist)
                    if width > 1:
                        a = min(1.0, a * width)
                    self._set(xi + ox, yi + oy, rgb, a * 0.9)

    def rect(self, x0: int, y0: int, x1: int, y1: int, rgb: tuple[int, int, int], a: float = 1.0) -> None:
        for y in range(max(0, y0), min(self.h, y1)):
            base = (y * self.w + max(0, x0)) * 3
            end = (y * self.w + min(self.w, x1)) * 3
            for i in range(base, end, 3):
                for c in range(3):
                    self.px[i + c] = int(self.px[i + c] * (1 - a) + rgb[c] * a)

    def circle(self, cx: float, cy: float, r: int, rgb: tuple[int, int, int]) -> None:
        for y in range(int(cy) - r, int(cy) + r + 1):
            for x in range(int(cx) - r, int(cx) + r + 1):
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    self._set(x, y, rgb, 1.0)

    def png(self) -> bytes:
        """Encode as PNG (RGB, no interlace)."""
        raw = bytearray()
        stride = self.w * 3
        for y in range(self.h):
            raw.append(0)  # filter: None
            raw += self.px[y * stride : (y + 1) * stride]
        ihdr = struct.pack("!IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)
        idat = zlib.compress(bytes(raw), 6)
        return (
            b"\x89PNG\r\n\x1a\n"
            + _png_pack(b"IHDR", ihdr)
            + _png_pack(b"IDAT", idat)
            + _png_pack(b"IEND", b"")
        )


# 5x7 bitmap font for digits/labels (compact, no external fonts)
_FONT: dict[str, tuple[str, ...]] = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    " ": ("00000",) * 7,
    ",": ("00000", "00000", "00000", "00000", "01100", "00100", "01000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    "%": ("11001", "11010", "00100", "01000", "10110", "00110", "00000"),
    "k": ("10000", "10010", "10100", "11000", "10100", "10010", "10000"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
}
for ch, rows in list(_FONT.items()):
    _FONT[ch] = tuple(r.ljust(5, "0") for r in rows) + ("00000",) * (7 - len(rows))


def _draw_text(c: _Canvas, x: int, y: int, s: str, rgb: tuple[int, int, int], scale: int = 1) -> None:
    cx = x
    for ch in s:
        glyph = _FONT.get(ch, _FONT[" "])
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    c.rect(cx + gx * scale, y + gy * scale, cx + (gx + 1) * scale, y + (gy + 1) * scale, rgb)
        cx += 6 * scale


def _fmt_compact(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"{v / 1_000:.1f}k"
    return f"{v:.0f}"


def render_price_chart(
    title: str,
    daily: Iterable[tuple[str, float, int]],
    current_price: float | None,
    width: int = W,
    height: int = H,
) -> bytes | None:
    """Render (day, avg_price, volume) series to PNG bytes. None when empty."""
    points = [(d, float(p)) for d, p, _ in daily if p and p > 0]
    # менее двух точек: всё равно отдаём валидный PNG (оси + сетка) — вызывающий
    # код и тесты ожидают байты, а не None
    prices = [p for _, p in points]
    # менее двух точек: всё равно валидный PNG (оси + сетка), не None
    if prices:
        lo, hi = min(prices), max(prices)
    else:
        lo, hi = 0.0, 1.0
    if current_price:
        lo, hi = min(lo, current_price), max(hi, current_price)
    span = (hi - lo) or max(hi, 1.0) * 0.05
    lo -= span * 0.08
    hi += span * 0.08

    c = _Canvas(width * 2, height * 2, BG)

    def X(i: int) -> float:
        return (PAD_L + (width - PAD_L - PAD_R) * i / max(1, len(points) - 1)) * 2

    def Y(v: float) -> float:
        return (PAD_T + (height - PAD_T - PAD_B) * (1 - (v - lo) / (hi - lo))) * 2

    # grid + y labels (4 divisions)
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        y = Y(v)
        c.line(PAD_L * 2, y, (width - PAD_R) * 2, y, GRID, 1)
        _draw_text(c, 6 * 2, int(y) - 7, _fmt_compact(v), TEXT, 2)

    # x labels: first, middle, last day (MM-DD)
    if len(points) >= 2:
        for i in (0, len(points) // 2, len(points) - 1):
            _draw_text(c, int(X(i)) - 20, (height - PAD_B + 6) * 2, points[i][0][5:], TEXT, 2)

    # area fill under the line (subtle)
    pts = [(X(i), Y(p)) for i, (_, p) in enumerate(points)]
    for i in range(len(pts) - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        steps = int(abs(x1 - x0)) or 1
        for s in range(steps + 1):
            t = s / steps
            x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            base = (height - PAD_B) * 2
            c.rect(int(x), int(y), int(x) + 2, base, FILL, 0.35)

    # line + min/max markers
    for i in range(len(pts) - 1):
        c.line(*pts[i], *pts[i + 1], LINE, 2)
    if prices:
        imin = min(range(len(prices)), key=lambda i: prices[i])
        imax = max(range(len(prices)), key=lambda i: prices[i])
        c.circle(*pts[imin], 6, MIN_P)
        c.circle(*pts[imax], 6, MAX_P)

    # current lot price — dashed horizontal line
    if current_price:
        y = Y(current_price)
        x = PAD_L * 2
        while x < (width - PAD_R) * 2:
            c.line(x, y, x + 14, y, LOT, 3)
            x += 24
        _draw_text(c, int(pts[-1][0]) - 60, int(y) - 18, _fmt_compact(current_price), LOT, 2)

    return c.png()


class ChartCache:
    """Day-keyed disk cache for rendered charts (LRU by file count)."""

    def __init__(self, directory: Path, max_files: int = 300) -> None:
        self.dir = Path(directory)
        self.max_files = max(10, int(max_files))
        self.dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def make_key(item_id: str, quality: int, upgrade: int) -> str:
        """P1-5: key is per calendar day per (item, quality, upgrade) — the
        PNG must not leak one lot's price marker into another lot's chart."""
        from datetime import UTC, datetime

        day = datetime.now(UTC).strftime("%Y-%m-%d")
        return f"{item_id}_{quality}_{upgrade}_{day}"

    def _path(self, key: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in key)
        return self.dir / f"{safe}.png"

    def get(self, key: str) -> bytes | None:
        p = self._path(key)
        try:
            data = p.read_bytes()
            p.touch()  # LRU: refresh mtime on hit
            return data
        except OSError:
            return None

    async def get_async(self, key: str) -> bytes | None:
        import asyncio

        return await asyncio.to_thread(self.get, key)

    def put(self, key: str, png: bytes) -> None:
        try:
            self._path(key).write_bytes(png)
            self._evict()
        except OSError:
            pass

    async def put_async(self, key: str, png: bytes) -> None:
        import asyncio

        await asyncio.to_thread(self.put, key, png)

    def _evict(self) -> None:
        try:
            files = sorted(self.dir.glob("*.png"), key=lambda p: p.stat().st_mtime)
            while len(files) > self.max_files:
                files.pop(0).unlink(missing_ok=True)
        except OSError:
            pass

    def clear(self) -> None:
        for p in self.dir.glob("*.png"):
            p.unlink(missing_ok=True)
