#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从可执行文件/图标文件里取出图标并**采样它的真实像素颜色**。

用途：想让自绘的图标"贴近某个官方图标"时，不要凭肉眼配色 ——
把官方图标抽出来读像素，直接拿到它的渐变起止色。

做法：PrivateExtractIconsW 取 HICON → GetIconInfo 拿位图 → GetDIBits 读 32bpp BGRA。

用法：python tools/sample_icon.py "D:/AI/WorkBuddy/WorkBuddy.exe" [尺寸，默认 256]
"""

import ctypes
import ctypes.wintypes as wt
import sys


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wt.DWORD), ('biWidth', wt.LONG), ('biHeight', wt.LONG),
                ('biPlanes', wt.WORD), ('biBitCount', wt.WORD), ('biCompression', wt.DWORD),
                ('biSizeImage', wt.DWORD), ('biXPelsPerMeter', wt.LONG),
                ('biYPelsPerMeter', wt.LONG), ('biClrUsed', wt.DWORD), ('biClrImportant', wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER), ('bmiColors', wt.DWORD * 3)]


class ICONINFO(ctypes.Structure):
    _fields_ = [('fIcon', wt.BOOL), ('xHotspot', wt.DWORD), ('yHotspot', wt.DWORD),
                ('hbmMask', wt.HBITMAP), ('hbmColor', wt.HBITMAP)]


def load_icon_pixels(path, size=256):
    """返回 (w, h, pixels[BGRA]) —— pixels 是自上而下的，每像素 4 字节。"""
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    # 必须显式声明 argtypes：c_void_p 结构字段读出来是 **Python int**，
    # 不声明的话 ctypes 会按 32 位 int 传参，64 位指针一超出 int 范围就 OverflowError
    # （踩过：hbmColor 侥幸没超，hbmMask 超了，直接抛在 finally 里）
    user32.PrivateExtractIconsW.restype = ctypes.c_uint
    user32.GetIconInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(ICONINFO)]
    user32.GetIconInfo.restype = wt.BOOL
    user32.DestroyIcon.argtypes = [ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_uint, ctypes.c_void_p,
                                ctypes.POINTER(BITMAPINFO), ctypes.c_uint]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]

    hicon = ctypes.c_void_p()
    icon_id = ctypes.c_uint()
    got = user32.PrivateExtractIconsW(ctypes.c_wchar_p(path), 0, size, size,
                                     ctypes.byref(hicon), ctypes.byref(icon_id), 1, 0)
    if got != 1 or not hicon.value:
        raise RuntimeError('取不到图标（PrivateExtractIconsW 返回 %s）' % got)
    try:
        info = ICONINFO()
        if not user32.GetIconInfo(hicon, ctypes.byref(info)):
            raise RuntimeError('GetIconInfo 失败')
        try:
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = size
            bmi.bmiHeader.biHeight = -size          # 负数 = 自上而下
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0         # BI_RGB
            buf = ctypes.create_string_buffer(size * size * 4)
            hdc = user32.GetDC(None)
            try:
                n = gdi32.GetDIBits(hdc, ctypes.c_void_p(info.hbmColor), 0, size, buf,
                                    ctypes.byref(bmi), 0)
            finally:
                user32.ReleaseDC(None, hdc)
            if not n:
                raise RuntimeError('GetDIBits 失败')
            return size, size, buf.raw
        finally:
            gdi32.DeleteObject(ctypes.c_void_p(info.hbmColor))
            if info.hbmMask:
                gdi32.DeleteObject(ctypes.c_void_p(info.hbmMask))
    finally:
        user32.DestroyIcon(hicon)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    size = int(sys.argv[2]) if len(sys.argv) > 2 else 256
    w, h, px = load_icon_pixels(path, size)
    print(f'[*] {path}  {w}x{h}')

    def at(x, y):
        i = (y * w + x) * 4
        b, g, r, a = px[i], px[i + 1], px[i + 2], px[i + 3]
        return r, g, b, a

    opaque = [(x, y) for y in range(0, h, 2) for x in range(0, w, 2) if at(x, y)[3] > 200]
    print(f'    不透明采样点：{len(opaque)}')
    if not opaque:
        print('    !! 整张图都是透明的')
        return 1

    cx = w // 2
    print('\n[竖中线采样]（x = 图宽一半，从上往下）')
    prev = None
    for y in range(0, h, max(1, h // 16)):
        r, g, b, a = at(cx, y)
        if a > 200:
            mark = '  <- 变化' if prev and (abs(r - prev[0]) + abs(g - prev[1]) + abs(b - prev[2]) > 18) else ''
            print(f'    y={y:>3}  #{r:02x}{g:02x}{b:02x}   rgb({r},{g},{b}){mark}')
            prev = (r, g, b)

    # 取"最上"和"最下"的不透明行作为渐变两端
    ys = sorted({y for _, y in opaque})
    top_y, bot_y = ys[0], ys[-1]
    print(f'\n[渐变端点]（按不透明区域的最上/最下取）')
    for label, y in (('顶部', top_y + 2), ('底部', bot_y - 2)):
        r, g, b, a = at(cx, y)
        print(f'    {label}  y={y:>3}  #{r:02x}{g:02x}{b:02x}')

    # 出现次数最多的绿色（量化到 8 级）
    from collections import Counter
    cnt = Counter()
    for x, y in opaque:
        r, g, b, a = at(x, y)
        if g > r and g > b:
            cnt[(r >> 4 << 4, g >> 4 << 4, b >> 4 << 4)] += 1
    print('\n[最常见的绿色（量化）]')
    for (r, g, b), n in cnt.most_common(6):
        print(f'    #{r:02x}{g:02x}{b:02x}  {n} 点')
    return 0


if __name__ == '__main__':
    sys.exit(main())
