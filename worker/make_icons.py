#!/usr/bin/env python3
"""Genera los íconos PWA (blanco sobre negro puro, alto contraste) sin dependencias externas."""
import math
import pathlib
import struct
import zlib

OUT = pathlib.Path(__file__).resolve().parent.parent / "public" / "icons"

# Línea de pulso (coordenadas normalizadas 0..1)
PULSE = [(0.14, 0.52), (0.34, 0.52), (0.42, 0.28), (0.52, 0.76), (0.60, 0.40), (0.66, 0.52), (0.86, 0.52)]


def dist_to_segment(px, py, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def render(size):
    half_w = 0.035 * size
    ring_r, ring_w = 0.42 * size, 0.018 * size
    cx = cy = size / 2
    pts = [(x * size, y * size) for x, y in PULSE]
    rows = []
    for y in range(size):
        row = bytearray([0])  # filtro PNG: none
        for x in range(size):
            d_ring = abs(math.hypot(x - cx, y - cy) - ring_r)
            on = d_ring <= ring_w
            if not on:
                on = any(dist_to_segment(x, y, pts[i], pts[i + 1]) <= half_w for i in range(len(pts) - 1))
            row.append(255 if on else 0)
        rows.append(bytes(row))
    return b"".join(rows)


def png(size):
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)  # escala de grises 8 bit
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(render(size), 9)) + chunk(b"IEND", b"")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for s in (192, 512):
        (OUT / f"icon-{s}.png").write_bytes(png(s))
        print(f"icon-{s}.png")
