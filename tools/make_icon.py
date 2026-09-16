#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成 icon.ico（多尺寸），并验证 Windows shell 真的能把它取出来。

为什么不用无头 Chrome 渲染 PNG 再打包：
  只放 PNG 压缩条目的 ICO，资源管理器有可能不渲染（表现为快捷方式还是白图标）。
  这里改成**纯 Python 自己光栅化**（4x4 超采样抗锯齿），
  然后 16~128px 写成经典 BMP(DIB) 条目、256px 写 PNG 条目 —— 这是 Windows 自家图标集的常见形态。

内置校验：调 user32.PrivateExtractIconsW 按多个尺寸取图，取得到才算通过。
不带任何第三方依赖（不需要 Pillow / cairosvg / 浏览器）。

用法：python tools/make_icon.py
"""

import ctypes
import os
import struct
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_ICO = os.path.join(ROOT, "icon.ico")

# 尺寸与容器格式：小的用 DIB（兼容性最好），256 用 PNG（体积小，Vista+ 支持）
SIZES_DIB = [16, 24, 32, 48, 64, 128]
SIZES_PNG = [256]

# 画布统一按 256x256 设计，再按目标尺寸缩放
CANVAS = 256.0
BG = dict(x=6.0, y=6.0, w=244.0, h=244.0, r=58.0)
# 贴近 WorkBuddy 官方图标的绿色。**这两个值不是配出来的，是从官方图标里采样得到的**
# （tools/sample_icon.py 读 D:/AI/WorkBuddy/WorkBuddy.exe 的图标像素）：
#   顶部 #0ec8a9 rgb(14,200,169)  偏青的绿
#   底部 #16cb82 rgb(22,203,130)  偏纯的绿
# 注意官方是**色相**渐变（青绿→绿），不是明暗渐变 —— 所以两端都很亮、很饱和，
# 换色时别顺手把底部压暗，那样立刻就不像了。
GRAD_TOP = (0x0E, 0xC8, 0xA9)      # #0ec8a9
GRAD_BOTTOM = (0x16, 0xCB, 0x82)   # #16cb82
BAR_BOTTOM = 196.0
BAR_HEIGHTS = (62.0, 96.0, 134.0)
BAR_OPACITY = (0.70, 0.86, 1.0)


def geometry(size):
    """小尺寸把柱子加粗、圆角收小，否则 16px 下会糊成一片。"""
    small = size <= 32
    bar_w = 52.0 if small else 46.0
    gap = 20.0 if small else 18.0
    rx = 6.0 if small else 10.0
    total = bar_w * 3 + gap * 2
    x0 = (CANVAS - total) / 2.0
    bars = []
    for i, h in enumerate(BAR_HEIGHTS):
        bars.append(dict(x=x0 + i * (bar_w + gap), y=BAR_BOTTOM - h,
                         w=bar_w, h=h, r=rx, op=BAR_OPACITY[i]))
    return bars


def in_rrect(x, y, bx, by, bw, bh, r):
    """圆角矩形内部判定（用「角点圆」的标准做法）。"""
    if x < bx or x > bx + bw or y < by or y > by + bh:
        return False
    cx = min(max(x, bx + r), bx + bw - r)
    cy = min(max(y, by + r), by + bh - r)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r


def sample(x, y, bars):
    """返回 (r, g, b, a)，a 为 0~1。坐标为 256 画布空间。"""
    r = g = b = 0.0
    a = 0.0
    if in_rrect(x, y, BG["x"], BG["y"], BG["w"], BG["h"], BG["r"]):
        t = (y - BG["y"]) / BG["h"]
        t = 0.0 if t < 0 else (1.0 if t > 1 else t)
        r = GRAD_TOP[0] + (GRAD_BOTTOM[0] - GRAD_TOP[0]) * t
        g = GRAD_TOP[1] + (GRAD_BOTTOM[1] - GRAD_TOP[1]) * t
        b = GRAD_TOP[2] + (GRAD_BOTTOM[2] - GRAD_TOP[2]) * t
        a = 1.0
    # 白色柱子叠在底色之上
    for bar in bars:
        if in_rrect(x, y, bar["x"], bar["y"], bar["w"], bar["h"], bar["r"]):
            op = bar["op"]
            na = op + a * (1.0 - op)
            if na > 0:
                r = (255.0 * op + r * a * (1.0 - op)) / na
                g = (255.0 * op + g * a * (1.0 - op)) / na
                b = (255.0 * op + b * a * (1.0 - op)) / na
            a = na
    return r, g, b, a


def rasterize(size):
    """返回 RGBA 字节（自上而下，每像素 4 字节）。"""
    ss = 4 if size <= 64 else 2          # 大尺寸采样少一点，速度够用
    bars = geometry(size)
    scale = CANVAS / size
    out = bytearray(size * size * 4)
    n = ss * ss
    for py in range(size):
        for px in range(size):
            ar = ag = ab = aa = 0.0
            for sy in range(ss):
                fy = (py + (sy + 0.5) / ss) * scale
                for sx in range(ss):
                    fx = (px + (sx + 0.5) / ss) * scale
                    r, g, b, a = sample(fx, fy, bars)
                    ar += r * a
                    ag += g * a
                    ab += b * a
                    aa += a
            i = (py * size + px) * 4
            if aa > 0:
                out[i] = int(ar / aa + 0.5)
                out[i + 1] = int(ag / aa + 0.5)
                out[i + 2] = int(ab / aa + 0.5)
                out[i + 3] = int(aa / n * 255 + 0.5)
    return bytes(out)


def dib_entry(size, rgba):
    """ICO 里的 BMP 条目：BITMAPINFOHEADER + 自下而上的 BGRA + AND 掩码。"""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         size * size * 4, 0, 0, 0, 0)
    xor = bytearray()
    for y in range(size - 1, -1, -1):          # 自下而上
        row = rgba[y * size * 4:(y + 1) * size * 4]
        for x in range(size):
            r, g, b, a = row[x * 4], row[x * 4 + 1], row[x * 4 + 2], row[x * 4 + 3]
            xor += bytes((b, g, r, a))         # BGRA
    mask_row = ((size + 31) // 32) * 4         # 1bpp，每行 4 字节对齐
    return header + bytes(xor) + bytes(mask_row * size)


def png_entry(size, rgba):
    """ICO 里的 PNG 条目（256px 用）。"""
    raw = bytearray()
    for y in range(size):
        raw.append(0)                          # filter type 0
        raw += rgba[y * size * 4:(y + 1) * size * 4]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def build_ico(entries, path):
    n = len(entries)
    out = bytearray(struct.pack("<HHH", 0, 1, n))
    offset = 6 + 16 * n
    dir_entries = bytearray()
    blobs = bytearray()
    for size, data in entries:
        dim = 0 if size >= 256 else size
        dir_entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    out += dir_entries + blobs
    with open(path, "wb") as fh:
        fh.write(bytes(out))


def verify_shell(path):
    """让 Windows 自己把图标取出来；取不到就是 ICO 有问题，不是缓存问题。"""
    user32 = ctypes.windll.user32
    print("[*] 用 shell 的 PrivateExtractIconsW 验证")
    all_ok = True
    for size in SIZES_DIB + SIZES_PNG:
        hicon = ctypes.c_void_p()
        icon_id = ctypes.c_uint()
        got = user32.PrivateExtractIconsW(
            ctypes.c_wchar_p(path), 0, size, size,
            ctypes.byref(hicon), ctypes.byref(icon_id), 1, 0)
        ok = got == 1 and hicon.value
        all_ok = all_ok and bool(ok)
        print(f"    {size:>3}px  {'取到了 OK' if ok else '取不到 !!'}")
        if hicon.value:
            user32.DestroyIcon(hicon)
    return all_ok


def main():
    print("[*] 光栅化（纯 Python，4x4 超采样）")
    entries = []
    for size in SIZES_DIB:
        rgba = rasterize(size)
        entries.append((size, dib_entry(size, rgba)))
        print(f"    {size:>3}px  DIB  {len(entries[-1][1]):>7,} 字节")
    for size in SIZES_PNG:
        rgba = rasterize(size)
        entries.append((size, png_entry(size, rgba)))
        print(f"    {size:>3}px  PNG  {len(entries[-1][1]):>7,} 字节")

    build_ico(entries, OUT_ICO)
    print(f"[+] 已生成 {OUT_ICO}（{len(entries)} 个尺寸，{os.path.getsize(OUT_ICO):,} 字节）")

    ok = verify_shell(OUT_ICO)
    print("[+] 图标可被 shell 读取" if ok else "[!] 有尺寸取不到，图标可能显示异常")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
