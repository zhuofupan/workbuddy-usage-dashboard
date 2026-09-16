#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
.lnk（Shell Link，MS-SHLLINK）解析器 —— 用来验证快捷方式真的写对了。

只做只读解析，不依赖 COM / pywin32。
关键点：目标路径通常存在 IDList 里（不是明文），所以不能只搜字符串就断言"目标缺失"。

用法：python tools/inspect_lnk.py "C:\\Users\\X\\Desktop\\WorkBuddy 用量看板.lnk"
"""

import os
import struct
import sys

# LinkFlags
HAS_IDLIST = 0x00000001
HAS_LINKINFO = 0x00000002
HAS_NAME = 0x00000004
HAS_RELPATH = 0x00000008
HAS_WORKDIR = 0x00000010
HAS_ARGUMENTS = 0x00000020
HAS_ICONLOCATION = 0x00000040
IS_UNICODE = 0x00000080
FORCE_NO_LINKINFO = 0x00000100
HAS_EXPSTRING = 0x00000200

FLAG_NAMES = [
    (HAS_IDLIST, "HasLinkTargetIDList"),
    (HAS_LINKINFO, "HasLinkInfo"),
    (HAS_NAME, "HasName"),
    (HAS_RELPATH, "HasRelativePath"),
    (HAS_WORKDIR, "HasWorkingDir"),
    (HAS_ARGUMENTS, "HasArguments"),
    (HAS_ICONLOCATION, "HasIconLocation"),
    (IS_UNICODE, "IsUnicode"),
    (FORCE_NO_LINKINFO, "ForceNoLinkInfo"),
    (HAS_EXPSTRING, "HasExpString"),
]

EXPECT_CLSID = "00021401-0000-0000-C000-000000000046"


def guid_from(data, off):
    d1, d2, d3 = struct.unpack_from("<IHH", data, off)
    d4 = data[off + 8:off + 16]
    return "%08X-%04X-%04X-%s-%s" % (d1, d2, d3, d4[:2].hex().upper(), d4[2:].hex().upper())


def utf16le(data, off, count_chars):
    raw = data[off:off + count_chars * 2]
    return raw.decode("utf-16-le", "replace"), off + count_chars * 2


def _runs_at(blob, start):
    out, run = [], []
    for i in range(start, len(blob) - 1, 2):
        ch = blob[i] | (blob[i + 1] << 8)
        printable = (0x20 <= ch < 0x7F) or 0x4E00 <= ch <= 0x9FFF
        if printable:
            run.append(ch)
        else:
            if len(run) >= 3:
                out.append("".join(chr(c) for c in run))
            run = []
    if len(run) >= 3:
        out.append("".join(chr(c) for c in run))
    return [s for s in out if any(c.isalnum() for c in s)]


def extract_idlist_strings(blob):
    """
    从 IDList 里抠出可读的路径片段。
    不去解析 ItemID 的各种变体（GUID 前缀 / ANSI / Unicode 扩展，坑很深），
    直接找 UTF-16LE 的可打印连续段 —— 对"验证目标是谁"这个目的足够。
    注意要对奇偶两种对齐都试：ItemID 长度多为奇数，字符串常常从奇偏移开始，
    只按偶偏移扫会得到字节交换后的乱码（"圀湩潤獷"）。
    """
    even, odd = _runs_at(blob, 0), _runs_at(blob, 1)

    def score(rs):
        return sum(sum(c.isascii() and c.isalnum() for c in s) for s in rs)

    return even if score(even) >= score(odd) else odd


def parse(path):
    data = open(path, "rb").read()
    print(f"文件        : {path}")
    print(f"大小        : {len(data)} 字节")
    if len(data) < 76:
        raise SystemExit("不是有效的 .lnk（小于 76 字节）")

    header_size = struct.unpack_from("<I", data, 0)[0]
    clsid = guid_from(data, 4)
    flags = struct.unpack_from("<I", data, 20)[0]
    attrs = struct.unpack_from("<I", data, 24)[0]
    filesize = struct.unpack_from("<I", data, 52)[0]
    icon_index = struct.unpack_from("<i", data, 56)[0]
    show_cmd = struct.unpack_from("<I", data, 60)[0]

    print(f"HeaderSize  : {header_size} {'OK' if header_size == 76 else '!! 应为 76'}")
    print(f"CLSID       : {clsid} {'OK' if clsid == EXPECT_CLSID else '!! 应为 ' + EXPECT_CLSID}")
    print(f"FileSize 字段: {filesize}   ShowCommand: {show_cmd}   IconIndex: {icon_index}")

    print("LinkFlags   :")
    for bit, name in FLAG_NAMES:
        if flags & bit:
            print(f"    {name}")

    off = header_size
    idlist_strings = []
    if flags & HAS_IDLIST:
        idlen = struct.unpack_from("<H", data, off)[0]
        # IDListSize 只是 IDList 字段本身的长度，前面还有 2 字节的 size 字段
        blob = data[off + 2:off + 2 + idlen]
        idlist_strings = extract_idlist_strings(blob)
        print(f"IDList      : size 字段 {idlen} 字节 (+2)，解出 {len(idlist_strings)} 段可读片段")
        for s in idlist_strings:
            print(f"    · {s}")
        off += 2 + idlen
    else:
        print("IDList      : 无（!! 双击可能无法解析目标）")

    if flags & HAS_LINKINFO:
        lilen = struct.unpack_from("<I", data, off)[0]
        if 0 < lilen <= len(data) - off:
            li = data[off:off + lilen]
            base_off = struct.unpack_from("<I", li, 16)[0] if lilen >= 20 else 0
            if base_off and base_off < lilen:
                raw = li[base_off:]
                end = raw.find(b"\x00")
                local = raw[:end].decode("mbcs", "replace") if end > 0 else ""
                print(f"LinkInfo    : {lilen} 字节  LocalBasePath = {local}")
            else:
                print(f"LinkInfo    : {lilen} 字节（无 LocalBasePath，目标靠 IDList 解析）")
            off += lilen
        else:
            print(f"LinkInfo    : 长度 {lilen} 不合理，跳过")
    else:
        print("LinkInfo    : 无")

    strings = {}
    for flag, key, label in [
        (HAS_NAME, "name", "Name"),
        (HAS_RELPATH, "relpath", "RelativePath"),
        (HAS_WORKDIR, "workdir", "WorkingDir"),
        (HAS_ARGUMENTS, "args", "Arguments"),
        (HAS_ICONLOCATION, "icon", "IconLocation"),
    ]:
        if not (flags & flag):
            continue
        n = struct.unpack_from("<H", data, off)[0]
        s, off = utf16le(data, off + 2, n)
        strings[key] = s
        print(f"{label:<12}: {s}")
    return flags, idlist_strings, strings


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    flags, idlist, strings = parse(sys.argv[1])

    print("== 结论 ==")
    problems = []
    if not (flags & HAS_IDLIST):
        problems.append("没有 IDList，双击可能无法定位目标")
    if not strings.get("args"):
        problems.append("没有 Arguments")
    if not strings.get("workdir"):
        problems.append("没有 WorkingDirectory")
    if not strings.get("icon"):
        problems.append("没有 IconLocation（桌面图标不会是自定义图标）")
    joined = " ".join(idlist).lower()
    if idlist and "wscript" not in joined and "wscript" not in (strings.get("relpath", "").lower()):
        problems.append("IDList/RelativePath 里没看到 wscript.exe")
    for p in problems:
        print("  [!] " + p)
    if not problems:
        print("  全部关键字段就位，快捷方式可用")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
