#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
提交前自查：这个仓库里有没有混进「个人数据」。

它扫三类东西：

  1. 本机身份标识 —— 家目录绝对路径、用户名、主机名、账号 UUID、
     设备 UUID（家里这几样都是运行时从 ~/.workbuddy 读出来的，不写死在代码里）
  2. 本机记忆 / 会话记录 —— .workbuddy/、memory/、MEMORY.md、*.jsonl
  3. 已知的本地数据文件 —— config.local.json、dashboard.log、dashboard.pid 等

用法：

  python tools/check_personal.py              # 查工作区 + 已跟踪文件（日常提交前跑）
  python tools/check_personal.py --staged     # 只查暂存区（适合放进 pre-commit 钩子）
  python tools/check_personal.py --history    # 连全部 git 历史一起查（开源/发布前跑一次）

退出码：0 = 干净，1 = 发现需要处理的内容。

安全说明：本脚本打印标识时**只显示前 6 位和长度**。
否则"自查工具的输出"本身就成了新的泄漏点。
本文件里没有任何真实标识，全部在运行时从本机读取。
"""

import argparse
import glob
import json
import os
import re
import socket
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# 这些文件名永远不该被 git 跟踪
NEVER_TRACKED = ("config.local.json", "quota.json", "session-titles.json",
                 "dashboard.log", "dashboard.pid", "device-id")

# 路径里出现这些片段就说明是「本机记忆 / 会话记录」
NEVER_TRACKED_SUBSTR = (".workbuddy/", ".jsonl", "memory.md")

BINARY_EXT = (".ico", ".png", ".jpg", ".jpeg", ".gif", ".zip", ".exe", ".dll", ".woff", ".woff2")


# --------------------------------------------------------------------------
# 本机身份标识
# --------------------------------------------------------------------------
def collect_identifiers():
    """
    从本机读出「能唯一指向这台机器 / 这个账号」的字符串。

    搜出来的结果只用于本地比对，不会打印全文（见 mask()）。
    换一台机器跑，这里自然就是那台机器的标识 —— 所以脚本可以公开。
    """
    ids = []

    home = os.path.expanduser("~")
    if home and os.path.isdir(home):
        ids.append(("家目录绝对路径", home))
        login = os.path.basename(home.rstrip("\\/"))
        if len(login) >= 3:
            ids.append(("登录用户名(家目录)", login))

    for var in ("USERNAME", "USER", "USERPROFILE"):
        v = os.environ.get(var) or ""
        if len(v) >= 3:
            ids.append(("环境变量 " + var, v))

    try:
        host = socket.gethostname()
        if len(host) >= 3:
            ids.append(("主机名", host))
    except OSError:
        pass

    wb = os.path.join(home, ".workbuddy")

    # 设备 UUID
    dev_file = os.path.join(wb, "device-id")
    try:
        v = open(dev_file, encoding="utf-8").read().strip()
        if v:
            ids.append(("设备 UUID (device-id)", v))
    except OSError:
        pass

    # 账号 UUID：settings.json 的 claw.users 键
    try:
        d = json.load(open(os.path.join(wb, "settings.json"), encoding="utf-8"))
        for k in ((d.get("claw") or {}).get("users") or {}):
            if UUID_RE.fullmatch(str(k)):
                ids.append(("账号 UUID (claw.users)", str(k)))
    except (OSError, ValueError, AttributeError):
        pass

    # 账号 UUID：connectors 的 accountIdentityKey（形如 <uuid>||enterprise）
    for f in glob.glob(os.path.join(wb, "connectors", "*", "connector-states*.json")):
        try:
            txt = open(f, encoding="utf-8").read()
        except OSError:
            continue
        for key in ("accountIdentityKey", "accountId", "userId", "enterpriseId"):
            for m in re.findall(r'"%s"\s*:\s*"([^"]+)"' % key, txt):
                if m.strip():
                    ids.append(("账号标识 (%s)" % key, m.strip()))

    # 去重 + 丢掉太短的（短串满仓库都是，只会刷屏）
    seen, out = set(), []
    for label, val in ids:
        val = str(val).strip()
        if len(val) < 4 or val in seen:
            continue
        seen.add(val)
        out.append((label, val))
    return out


def mask(value):
    """只暴露前 6 位和长度 —— 自查工具的输出本身不能变成泄漏点。"""
    v = str(value)
    if len(v) <= 6:
        return v[0] + "*" * (len(v) - 1) if len(v) > 1 else "*"
    return v[:6] + "…(" + str(len(v)) + " 字符)"


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------
def git(*args):
    p = subprocess.run(["git"] + list(args), cwd=ROOT,
                       capture_output=True, text=True, errors="replace")
    return p.stdout


def scan_text(text, where, ids, problems):
    """在文本里找身份标识和 UUID 形态的字符串。"""
    low = text.lower()
    for label, val in ids:
        if val.lower() in low:
            problems.append("命中「%s」%s" % (label, mask(val)))
            print("  ✗ [%s] 命中 %s = %s" % (where, label, mask(val)))
            continue
        # 长标识再查前 8 位前缀（日志里常被截断成前 8 位显示）
        if len(val) >= 12 and val[:8].lower() in low:
            problems.append("命中「%s」前缀 %s" % (label, mask(val[:8])))
            print("  ✗ [%s] 命中 %s 前 8 位前缀 %s" % (where, label, mask(val)))

    for m in set(UUID_RE.findall(text)):
        # Windows COM 的公开 CLSID/IID 不是个人信息，放行
        if m.lower().startswith(("00021401", "000214f9", "000214e6", "0000010b",
                                 "0000036b", "000214fd", "45e2b4ae", "79eac9")):
            continue
        problems.append("UUID 形态字符串 %s" % m)
        print("  ✗ [%s] 疑似真实 UUID: %s" % (where, m))


def check_tracked_paths(paths, problems):
    """被跟踪的路径里，不能有本地数据文件 / 记忆文件。"""
    for p in paths:
        leaf = os.path.basename(p)
        low = p.lower()
        if leaf in NEVER_TRACKED or any(s in low for s in NEVER_TRACKED_SUBSTR):
            problems.append("被跟踪的本机数据文件：%s" % p)
            print("  ✗ 本机数据文件被 git 跟踪了：%s" % p)


def check_ignored(problems):
    """确认那几个本地数据文件确实被 .gitignore 挡住。"""
    print("== 本地数据文件是否被忽略 ==")
    for name in NEVER_TRACKED:
        code = subprocess.run(["git", "check-ignore", "-q", name],
                              cwd=ROOT).returncode
        ok = code == 0
        print("  %s %s" % ("✓" if ok else "✗", name))
        if not ok:
            problems.append("未被忽略：%s（在 .gitignore 里补一条）" % name)


def main():
    ap = argparse.ArgumentParser(description="检查仓库里是否混进个人数据")
    ap.add_argument("--staged", action="store_true", help="只查暂存区")
    ap.add_argument("--history", action="store_true", help="连全部 git 历史一起查")
    args = ap.parse_args()

    problems = []
    ids = collect_identifiers()

    print("== 本次比对用的本机标识（只显示前 6 位）==")
    if ids:
        for label, val in ids:
            print("  %-26s %s" % (label, mask(val)))
    else:
        print("  （没读到家目录 / 账号 / 设备标识，可能不在 WorkBuddy 环境下）")
    print()

    check_ignored(problems)
    print()

    if args.staged:
        print("== 扫描暂存区 ==")
        names = [n for n in git("diff", "--cached", "--name-only").split() if n]
        check_tracked_paths(names, problems)
        for n in names:
            if n.endswith(BINARY_EXT):
                continue
            scan_text(git("show", ":./" + n), n, ids, problems)
        print("  扫描了 %d 个暂存文件" % len(names))

    else:
        print("== 扫描被跟踪的文件 + 工作区未忽略的文件 ==")
        names = [n for n in git("ls-files", "--cached", "--others",
                                "--exclude-standard").split() if n]
        check_tracked_paths(names, problems)
        scanned = 0
        for n in names:
            path = os.path.join(ROOT, n)
            if n.endswith(BINARY_EXT) or not os.path.isfile(path):
                continue
            try:
                txt = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            scan_text(txt, n, ids, problems)
            scanned += 1
        print("  扫描了 %d 个文件（共 %d 个待提交项）" % (scanned, len(names)))

    if args.history:
        print()
        print("== 扫描全部 git 历史 ==")
        out = git("rev-list", "--objects", "--all").splitlines()
        scanned = 0
        for line in out:
            parts = line.split(" ", 1)
            if len(parts) != 2:
                continue
            sha, name = parts
            if name.endswith(BINARY_EXT):
                continue
            blob = subprocess.run(["git", "cat-file", "-p", sha], cwd=ROOT,
                                  capture_output=True).stdout
            scan_text(blob.decode("utf-8", errors="replace"), name, ids, problems)
            scanned += 1
        print("  扫描了 %d 个历史对象" % scanned)

    print()
    print("== 结论 ==")
    if problems:
        print("  ✗ 发现 %d 处需要处理：" % len(problems))
        for p in dict.fromkeys(problems):
            print("      - " + p)
        print()
        print("  处理完再提交。若已推送，光删文件没用 —— 历史里的 blob 还在，")
        print("  得改历史（git filter-repo）并轮换对应凭据。")
        return 1
    print("  ✓ 未发现个人信息：账号标识、设备标识、家目录路径、本机记忆都不在里面")
    return 0


if __name__ == "__main__":
    sys.exit(main())
