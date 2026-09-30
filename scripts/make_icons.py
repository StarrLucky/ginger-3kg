#!/usr/bin/env python3
"""Иконки приложения без внешних зависимостей.

PNG пишется руками (zlib + struct), потому что тащить Pillow ради трёх
квадратов несоразмерно, а проект принципиально живёт без сборки. Запускать
редко и вручную: `python3 scripts/make_icons.py`.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "food_api" / "webapp" / "icons"

BG = (224, 122, 31)  # янтарь — имбирь в названии
FG = (255, 251, 245)  # тёплый белый


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def write_png(path: Path, size: int, pixels: list[list[tuple[int, int, int]]]) -> None:
    raw = b"".join(b"\x00" + b"".join(struct.pack("BBB", *px) for px in row) for row in pixels)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def render(size: int) -> list[list[tuple[int, int, int]]]:
    """Миска с паром: читается и на 180 px, и на 192.

    Сглаживание — суперсэмплингом 3x3: без него дуги на мелком размере
    выглядят рваными.
    """
    ss = 3
    cx = cy = size / 2
    bowl_r = size * 0.30
    bowl_top = cy + size * 0.02
    rim_h = max(1.0, size * 0.035)
    steam_r = size * 0.075

    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            hits = 0
            for sy in range(ss):
                for sx in range(ss):
                    px = x + (sx + 0.5) / ss
                    py = y + (sy + 0.5) / ss
                    dx, dy = px - cx, py - bowl_top
                    in_bowl = dy >= 0 and dx * dx + dy * dy <= bowl_r * bowl_r
                    in_rim = abs(py - bowl_top) <= rim_h / 2 and abs(dx) <= bowl_r + size * 0.045
                    sdx = px - cx
                    sdy = py - (bowl_top - size * 0.16)
                    in_steam = sdx * sdx + sdy * sdy <= steam_r * steam_r
                    if in_bowl or in_rim or in_steam:
                        hits += 1
            t = hits / (ss * ss)
            row.append(tuple(round(BG[i] + (FG[i] - BG[i]) * t) for i in range(3)))
        rows.append(row)
    return rows


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size, name in ((180, "apple-touch-icon.png"), (192, "icon-192.png"), (512, "icon-512.png")):
        write_png(OUT / name, size, render(size))
        print(f"{name}: {size}x{size}")


if __name__ == "__main__":
    main()
