#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
在桌面创建「WorkBuddy 用量看板」快捷方式（.lnk）。

为什么用 ctypes 手写 COM 调用：
  - 环境里 WScript.Shell / New-Object -ComObject 这类 COM 实例化被安全策略拦掉了
  - pywin32 不一定装了
所以直接调 IShellLinkW + IPersistFile 这两个标准接口，零依赖。

路径变了、或删了快捷方式想重建，重新跑一次即可：
    python tools/create_shortcut.py
"""

import ctypes
import os
import struct
import sys
from ctypes import byref, c_int, c_long, c_ulong, c_void_p, c_wchar_p, POINTER

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VBS = os.path.join(ROOT, "launch.vbs")
ICO = os.path.join(ROOT, "icon.ico")
LNK_NAME = "WorkBuddy 用量看板.lnk"

HRESULT_OK = 0
CLSCTX_INPROC_SERVER = 1

# IShellLinkW 方法在 vtable 中的下标 = 前面所有方法的个数。
# 0-2 是 IUnknown(QueryInterface/AddRef/Release)，然后：
#   3 GetPath, 4 GetIDList, 5 SetIDList, 6 GetDescription, 7 SetDescription,
#   8 GetWorkingDirectory, 9 SetWorkingDirectory, 10 GetArguments, 11 SetArguments,
#   12 GetHotkey, 13 SetHotkey, 14 GetShowCmd, 15 SetShowCmd,
#   16 GetIconLocation, 17 SetIconLocation, 18 SetRelativePath, 19 Resolve, 20 SetPath
IDX_SET_DESCRIPTION = 7
IDX_SET_WORKDIR = 9
IDX_SET_ARGUMENTS = 11
IDX_SET_SHOWCMD = 15
IDX_SET_ICON = 17
IDX_SET_PATH = 20
IDX_SAVE = 6          # IPersistFile::Save


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text):
        super().__init__()
        if ctypes.oledll.ole32.CLSIDFromString(c_wchar_p(text), byref(self)) != 0:
            raise OSError("CLSIDFromString 失败: " + text)

    def __repr__(self):
        return "GUID(%08X-%04X-%04X-...)" % (self.Data1, self.Data2, self.Data3)


def call_vtbl(ptr, index, restype, argtypes, *args):
    """按 vtable 下标调用 COM 方法。"""
    vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(vtbl[index])(ptr, *args)


def desktop_dir():
    """走 SHGetFolderPathW(CSIDL_DESKTOPDIRECTORY)，自动兼容 OneDrive 重定向。"""
    CSIDL_DESKTOPDIRECTORY = 0x0010
    buf = ctypes.create_unicode_buffer(260)
    hr = ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_DESKTOPDIRECTORY, None, 0, buf)
    if hr != 0:
        raise OSError("SHGetFolderPathW 失败: 0x%08X" % (hr & 0xFFFFFFFF))
    return buf.value


def notify_shell():
    """让资源管理器重建图标缓存。
    程序化创建/更新快捷方式后，图标常常还是旧的（甚至空白），
    等价于手动"刷新图标缓存"，是这一步而不是 ICO 格式的问题。"""
    SHCNE_ASSOCCHANGED = 0x08000000
    SHCNF_IDLIST = 0x0000
    try:
        ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
        return True
    except Exception:
        return False


def make_lnk(lnk_path, target, arguments, workdir, icon, desc):
    ole32 = ctypes.oledll.ole32
    ole32.CoInitialize(None)

    clsid = GUID("{00021401-0000-0000-C000-000000000046}")   # ShellLink
    iid_link = GUID("{000214F9-0000-0000-C000-000000000046}")  # IShellLinkW
    iid_persist = GUID("{0000010B-0000-0000-C000-000000000046}")  # IPersistFile

    link = c_void_p()
    hr = ole32.CoCreateInstance(byref(clsid), None, CLSCTX_INPROC_SERVER,
                                byref(iid_link), byref(link))
    if hr != HRESULT_OK:
        raise OSError("CoCreateInstance(ShellLink) 失败: 0x%08X" % (hr & 0xFFFFFFFF))

    try:
        for idx, val, types, extra in [
            (IDX_SET_PATH, target, [c_wchar_p, c_int], 1),
            (IDX_SET_ARGUMENTS, arguments, [c_wchar_p], None),
            (IDX_SET_WORKDIR, workdir, [c_wchar_p], None),
            (IDX_SET_DESCRIPTION, desc, [c_wchar_p], None),
            (IDX_SET_ICON, icon, [c_wchar_p, c_int], 0),
            (IDX_SET_SHOWCMD, 1, [c_int], None),
        ]:
            args = (val, extra) if extra is not None else (val,)
            r = call_vtbl(link, idx, c_long, types, *args)
            if r != HRESULT_OK:
                raise OSError("IShellLink 方法 #%d 失败: 0x%08X" % (idx, r & 0xFFFFFFFF))

        persist = c_void_p()
        r = call_vtbl(link, 0, c_long, [POINTER(GUID), c_void_p],
                      byref(iid_persist), byref(persist))
        if r != HRESULT_OK:
            raise OSError("QueryInterface(IPersistFile) 失败: 0x%08X" % (r & 0xFFFFFFFF))
        try:
            r = call_vtbl(persist, IDX_SAVE, c_long, [c_wchar_p, c_int], lnk_path, 1)
            if r != HRESULT_OK:
                raise OSError("IPersistFile::Save 失败: 0x%08X" % (r & 0xFFFFFFFF))
        finally:
            call_vtbl(persist, 2, c_ulong, [])
    finally:
        call_vtbl(link, 2, c_ulong, [])


def verify(lnk_path, expect):
    """
    用真正的 .lnk 解析器校验（tools/inspect_lnk.py）。
    早先版本靠"在原始字节里搜 UTF-16 明文"来断言目标存在 —— 那是错的：
    目标路径主要是存在 IDList 里的，明文搜不到，会误报"目标缺失"。
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from inspect_lnk import parse, HAS_ICONLOCATION, HAS_ARGUMENTS, HAS_WORKDIR

    print("[*] 回读校验（解析 .lnk 结构）")
    flags, idlist, strings = parse(lnk_path)

    ok = True
    checks = [
        ("HasArguments", bool(flags & HAS_ARGUMENTS)),
        ("HasWorkingDir", bool(flags & HAS_WORKDIR)),
        ("HasIconLocation", bool(flags & HAS_ICONLOCATION)),
    ]
    for label, good in checks:
        print(f"  {label:<16} {'OK' if good else '缺失 !!'}")
        ok = ok and good

    # 目标：LinkInfo 的 LocalBasePath 或 IDList 片段里应能看到 wscript
    blob = (strings.get("relpath", "") + " " + " ".join(idlist)).lower()
    has_wscript = "wscript" in blob
    print(f"  {'目标含 wscript':<16} {'OK' if has_wscript else '没看到 !!'}")
    ok = ok and has_wscript

    for key, want, label in expect:
        got = strings.get(key, "")
        good = want.lower() in got.lower()
        print(f"  {label:<16} {'OK' if good else '不一致 !!'}  {got}")
        ok = ok and good
    return ok


def main():
    for p in (VBS, ICO):
        if not os.path.exists(p):
            raise SystemExit(f"缺少文件：{p}")

    desktop = desktop_dir()
    lnk_path = os.path.join(desktop, LNK_NAME)
    target = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32", "wscript.exe")
    if not os.path.exists(target):
        raise SystemExit("找不到 wscript.exe：" + target)

    arguments = f'"{VBS}"'
    desc = "WorkBuddy 用量实时看板：双击启动，无窗口"

    print(f"[*] 桌面目录：{desktop}")
    print(f"[*] 创建快捷方式：{lnk_path}")
    # IconLocation 不带 ",index"：桌面上其它能正常显示图标的快捷方式都是这个写法
    make_lnk(lnk_path, target, arguments, ROOT, ICO, desc)

    ok = verify(lnk_path, [
        ("args", VBS, "参数"),
        ("workdir", ROOT, "工作目录"),
        ("icon", ICO, "图标"),
        ("name", desc, "描述"),
    ])
    if notify_shell():
        print("[*] 已通知 shell 重建图标缓存（SHChangeNotify）")
    else:
        print("[!] SHChangeNotify 失败，可能需要手动刷新桌面")
    print("[+] 快捷方式创建成功" if ok else "[!] 校验未全部通过，请检查上面的输出")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
