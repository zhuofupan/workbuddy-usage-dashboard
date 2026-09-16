#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把"官方图标"和"本看板图标"抽出来拼成一张并排对比图（纯 Python，无第三方依赖）。

为什么要这个：说"颜色贴近了"不能靠肉眼，也不能只报两个 hex ——
把两张图摆在一起，人和脚本都能直接看/量。

用法：python tools/compare_icons.py [输出路径]
默认对比 D:/AI/WorkBuddy/WorkBuddy.exe 与 本目录 icon.ico，输出 _icon_compare.png
"""

import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sample_icon import load_icon_pixels          # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OFFICIAL = r"D:\AI\WorkBuddy\WorkBuddy.exe"
MINE = os.path.join(ROOT, "icon.ico")
SIZE = 256


def png_rgba_from_ico(path, want=256):
    """ICO 里 256px 存的是 PNG 条目，直接解出来。"""
    d = open(path, "rb").read()
    n = struct.unpack("<H", d[4:6])[0]
    for i in range(n):
        w, h = d[6 + i * 16], d[7 + i * 16]
        if (w or 256) != want:
            continue
        ln, off = struct.unpack("<II", d[6 + i * 16 + 8:6 + i * 16 + 16])
        blob = d[off:off + ln]
        pos, idat = 8, b""
        while pos < len(blob):
            l = struct.unpack(">I", blob[pos:pos + 4])[0]
            tag = blob[pos + 4:pos + 8]
            if tag == b"IDAT":
                idat += blob[pos + 8:pos + 8 + l]
            pos += 12 + l
        raw = zlib.decompress(idat)
        stride = want * 4
        out = bytearray()
        for y in range(want):
            row = raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)]
            for x in range(want):
                r, g, b, a = row[x * 4:x * 4 + 4]     # PNG 字节序本来就是 RGBA
                out += bytes((r, g, b, a))
        return want, want, bytes(out)
    raise RuntimeError('ICO 里没有 %dpx 条目' % want)


def write_png(path, w, h, rgba):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgba[y * w * 4:(y + 1) * w * 4]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n"
                 + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
                 + chunk(b"IEND", b""))


def bgra_to_rgba(px):
    """GetDIBits 读出来是 BGRA；PNG 那条路是 RGBA。统一成 RGBA 再比，否则会看错颜色。"""
    out = bytearray(len(px))
    for i in range(0, len(px), 4):
        out[i] = px[i + 2]
        out[i + 1] = px[i + 1]
        out[i + 2] = px[i]
        out[i + 3] = px[i + 3]
    return bytes(out)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "icon_compare.png")
    ow, oh, opx = load_icon_pixels(OFFICIAL, SIZE)
    opx = bgra_to_rgba(opx)
    mw, mh, mpx = png_rgba_from_ico(MINE, SIZE)
    gap = 24
    W, H = SIZE * 2 + gap * 3, SIZE + gap * 2
    canvas = bytearray([0xff] * (W * H * 4))          # 白底
    for idx, (px, ox) in enumerate(((opx, gap), (mpx, gap * 2 + SIZE))):
        for y in range(SIZE):
            for x in range(SIZE):
                i = (y * SIZE + x) * 4
                r, g, b, a = px[i], px[i + 1], px[i + 2], px[i + 3]
                if a == 0:
                    continue
                j = ((y + gap) * W + ox + x) * 4
                for c, v in enumerate((r, g, b)):
                    canvas[j + c] = (v * a + canvas[j + c] * (255 - a)) // 255
                canvas[j + 3] = 255
    write_png(out, W, H, bytes(canvas))
    print(f'[+] 已生成对比图：{out}（左＝官方 WorkBuddy，右＝本看板）')

    # 顺带报一下两者的顶部/底部色，方便直接比数字
    for label, px in (('官方', opx), ('本看板', mpx)):
        cx = SIZE // 2
        def at(y):
            i = (y * SIZE + cx) * 4
            return px[i], px[i + 1], px[i + 2]
        ys = [y for y in range(SIZE) if px[(y * SIZE + cx) * 4 + 3] > 200]
        r1, g1, b1 = at(ys[1]); r2, g2, b2 = at(ys[-2])
        print(f'    {label:<5} 顶 #{r1:02x}{g1:02x}{b1:02x}   底 #{r2:02x}{g2:02x}{b2:02x}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
