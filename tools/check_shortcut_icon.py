#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
问 Windows shell：这个文件到底会显示哪个图标？并把图标真的抠出来存成 PNG 看一眼。

只读诊断，不依赖第三方库。
  SHGetFileInfoW(SHGFI_ICONLOCATION) -> shell 解析出的图标来源与索引
  SHGetFileInfoW(SHGFI_ICON|SHGFI_LARGEICON) -> 实际 HICON
  再 GetIconInfo + GetDIBits 把像素拿出来写成 PNG

用法：python tools/check_shortcut_icon.py <file.lnk> [more.lnk ...]
"""

import ctypes
import os
import struct
import sys
import zlib
from ctypes import wintypes

SHGFI_ICON = 0x000000100
SHGFI_LARGEICON = 0x000000000
SHGFI_SMALLICON = 0x000000001
SHGFI_ICONLOCATION = 0x00001000
SHGFI_USEFILEATTRIBUTES = 0x000000010
MAX_PATH = 260


class SHFILEINFOW(ctypes.Structure):
    _fields_ = [("hIcon", wintypes.HANDLE),
                ("iIcon", ctypes.c_int),
                ("dwAttributes", wintypes.DWORD),
                ("szDisplayName", ctypes.c_wchar * MAX_PATH),
                ("szTypeName", ctypes.c_wchar * 80)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL),
                ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD),
                ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long),
                ("bmWidth", ctypes.c_long),
                ("bmHeight", ctypes.c_long),
                ("bmWidthBytes", ctypes.c_long),
                ("bmPlanes", ctypes.c_ushort),
                ("bmBitsPixel", ctypes.c_ushort),
                ("bmBits", ctypes.c_void_p)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD),
                ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def icon_location(path):
    sh = SHFILEINFOW()
    ctypes.set_last_error(0)
    r = ctypes.windll.shell32.SHGetFileInfoW(
        ctypes.c_wchar_p(path), 0, ctypes.byref(sh), ctypes.sizeof(sh), SHGFI_ICONLOCATION)
    if not r:
        return None, f"SHGetFileInfoW 返回 0（LastError={ctypes.get_last_error()}）"
    return sh.szDisplayName, sh.iIcon


def icon_handle(path, small=False):
    sh = SHFILEINFOW()
    flags = SHGFI_ICON | (SHGFI_SMALLICON if small else SHGFI_LARGEICON)
    r = ctypes.windll.shell32.SHGetFileInfoW(
        ctypes.c_wchar_p(path), 0, ctypes.byref(sh), ctypes.sizeof(sh), flags)
    if not r or not sh.hIcon:
        return None
    return sh.hIcon


def _h(v):
    """统一包成 c_void_p：句柄是 64 位，不声明 argtypes 时 ctypes 会按 c_int 截断。
    已经是 c_void_p 就直接用（再包一次会报 cannot be converted to pointer）。"""
    return v if isinstance(v, ctypes.c_void_p) else ctypes.c_void_p(v)


def hicon_to_rgba(hicon):
    """HICON -> (w, h, BGRA bytes)"""
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    ii = ICONINFO()
    if not user32.GetIconInfo(_h(hicon), ctypes.byref(ii)):
        return None
    hbm_color = _h(ii.hbmColor)
    hbm_mask = _h(ii.hbmMask)
    try:
        bm = BITMAP()
        if not gdi32.GetObjectW(hbm_color, ctypes.sizeof(bm), ctypes.byref(bm)):
            return None
        w, h = bm.bmWidth, bm.bmHeight
        hdc = user32.GetDC(None)
        try:
            bih = BITMAPINFOHEADER()
            bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bih.biWidth = w
            bih.biHeight = -h            # 负数 = 自上而下
            bih.biPlanes = 1
            bih.biBitCount = 32
            bih.biCompression = 0
            buf = ctypes.create_string_buffer(w * h * 4)
            got = gdi32.GetDIBits(_h(hdc), hbm_color, 0, h, buf,
                                  ctypes.byref(bih), 0)
            if not got:
                return None
            return w, h, buf.raw
        finally:
            user32.ReleaseDC(None, _h(hdc))
    finally:
        if ii.hbmColor:
            gdi32.DeleteObject(hbm_color)
        if ii.hbmMask:
            gdi32.DeleteObject(hbm_mask)


def write_png(path, w, h, bgra):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        row = bgra[y * w * 4:(y + 1) * w * 4]
        for x in range(w):
            b, g, r, a = row[x * 4], row[x * 4 + 1], row[x * 4 + 2], row[x * 4 + 3]
            raw += bytes((r, g, b, a))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    open(path, "wb").write(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b""))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    # SHGetFileInfoW 要求调用线程先初始化 COM（STA），否则直接返回 0
    hr = ctypes.windll.ole32.CoInitialize(None)
    print(f"[i] CoInitialize hr=0x{hr & 0xFFFFFFFF:08X}（0 或 1 都算成功）")
    for i, path in enumerate(sys.argv[1:]):
        print(f"===== {os.path.basename(path)} =====")
        loc, idx = icon_location(path)
        print(f"  shell 解析的图标来源 : {loc}")
        print(f"  shell 解析的图标索引 : {idx}")
        if loc and not os.path.exists(loc):
            print("  !! 这个图标文件不存在 —— 图标必然显示不出来")
        for small in (False, True):
            h = icon_handle(path, small)
            tag = "小图标" if small else "大图标"
            if not h:
                print(f"  {tag}: 取不到 !!")
                continue
            res = hicon_to_rgba(h)
            ctypes.windll.user32.DestroyIcon(h)
            if not res:
                print(f"  {tag}: 像素读取失败")
                continue
            w, hh, bgra = res
            # 统计非透明像素比例，判断是不是"空白图标"
            opaque = sum(1 for p in range(0, len(bgra), 4) if bgra[p + 3] > 8)
            png = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               f"_shellicon_{i}_{'sm' if small else 'lg'}.png")
            write_png(png, w, hh, bgra)
            print(f"  {tag}: {w}x{hh}  非透明像素 {opaque}/{w * hh} "
                  f"({opaque * 100 // (w * hh)}%)  -> {os.path.basename(png)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
