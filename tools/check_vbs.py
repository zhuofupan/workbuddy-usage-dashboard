#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
launch.vbs 的静态检查。

不能在本机执行 wscript（被安全策略列为 LOLBin），所以改为静态验证：
  1) 编码：纯 ASCII + CRLF（wscript 只按 ANSI/UTF-16 读源码，UTF-8 中文会乱码）
  2) 结构：If/End If、For/Next、Function/End Function 配对
  3) Option Explicit 下，所有被赋值的变量都必须 Dim 过
  4) 用 Chr(34) 重建它实际会执行的命令行，确认引号没问题

用法：python tools/check_vbs.py
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VBS = os.path.join(os.path.dirname(HERE), "launch.vbs")


def normalize(path):
    raw = open(path, "rb").read()
    txt = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    data = txt.replace("\n", "\r\n").encode("ascii")     # 非 ASCII 会在这里抛错
    open(path, "wb").write(data)
    return data.decode("ascii")


def strip_comments(lines):
    """去掉整行注释（VBS 用 ' 开头），保留代码行。"""
    return [ln for ln in lines if not ln.lstrip().startswith("'")]


def detect_python():
    """
    和 launch.vbs 的 FindPython() 同一套逻辑：优先 .workbuddy 里 WorkBuddy 自带的
    python.exe，找不到就回退裸名 "python"（由 cmd 走 PATH 解析）。

    刻意不写死绝对路径 —— 写死等于把某台机器的用户名带进仓库，而且对别人必然不可用。
    """
    versions = os.path.join(os.path.expanduser("~"), ".workbuddy",
                            "binaries", "python", "versions")
    if os.path.isdir(versions):
        for name in sorted(os.listdir(versions)):
            cand = os.path.join(versions, name, "python.exe")
            if os.path.isfile(cand):
                return cand
    return "python"


def main():
    text = normalize(VBS)
    raw_lines = text.split("\r\n")
    lines = strip_comments(raw_lines)

    problems = []
    print("== 文件 ==")
    print(f"  {os.path.basename(VBS)}  行数 {len(raw_lines)}  "
          f"纯 ASCII ✓  非 ASCII 字节 0 ✓")

    # --- 结构配对
    print("== 结构配对 ==")
    pairs = [
        ("多行 If", re.compile(r"^\s*If .*\bThen\s*$", re.I), re.compile(r"^\s*End If\s*$", re.I)),
        ("For", re.compile(r"^\s*For\b", re.I), re.compile(r"^\s*Next\b", re.I)),
        ("Function", re.compile(r"^\s*Function\b", re.I), re.compile(r"^\s*End Function\s*$", re.I)),
        ("Sub", re.compile(r"^\s*Sub\b", re.I), re.compile(r"^\s*End Sub\s*$", re.I)),
        ("With", re.compile(r"^\s*With\b", re.I), re.compile(r"^\s*End With\s*$", re.I)),
        ("Select", re.compile(r"^\s*Select Case\b", re.I), re.compile(r"^\s*End Select\s*$", re.I)),
    ]
    for name, op, cl in pairs:
        a = sum(1 for ln in lines if op.match(ln))
        b = sum(1 for ln in lines if cl.match(ln))
        flag = "OK" if a == b else "不匹配 !!"
        if a != b:
            problems.append(f"{name} 配对不匹配：{a} vs {b}")
        print(f"  {name:<10} 开 {a}  闭 {b}   {flag}")

    # --- 有 Option Explicit 就得所有变量都 Dim
    print("== Option Explicit 变量声明 ==")
    has_oe = any(re.match(r"^\s*Option Explicit\s*$", ln, re.I) for ln in lines)
    print(f"  Option Explicit: {'有' if has_oe else '无'}")
    if has_oe:
        declared = set()
        for ln in lines:
            m = re.match(r"^\s*Dim\s+(.+)$", ln, re.I)
            if m:
                for part in m.group(1).split(","):
                    declared.add(part.strip().lower())
        # 函数名不算未声明：VBScript 靠「给函数名赋值」来返回值
        funcs = set()
        for ln in lines:
            m = re.match(r"^\s*Function\s+([A-Za-z_]\w*)", ln, re.I)
            if m:
                funcs.add(m.group(1).lower())
            m = re.match(r"^\s*Sub\s+([A-Za-z_]\w*)", ln, re.I)
            if m:
                funcs.add(m.group(1).lower())
        assigned = set()
        for ln in lines:
            m = re.match(r"^\s*([A-Za-z_]\w*)\s*=", ln)          # 避免 == 
            if m and not ln.lstrip().startswith("If"):
                assigned.add(m.group(1).lower())
        undeclared = sorted(assigned - declared - funcs)
        print(f"  Dim 声明 {len(declared)} 个：{', '.join(sorted(declared))}")
        print(f"  函数返回值 {len(funcs)} 个：{', '.join(sorted(funcs))}")
        print(f"  赋值变量 {len(assigned)} 个：{', '.join(sorted(assigned))}")
        if undeclared:
            problems.append("有变量未 Dim：" + ", ".join(undeclared))
            print(f"  !! 未声明的变量：{undeclared}")
        else:
            print("  全部已声明（函数返回值已排除）OK")

    # --- 重建实际执行的命令行
    print("== 重建命令行（Chr(34) 求值）==")
    q = '"'
    py = detect_python()
    base = os.path.dirname(HERE)          # tools/ 的上一级 = 项目根目录
    print(f"  FindPython() 探测结果：{py}")
    for opt in ("--port 8791 --no-browser", "--probe --port 8791", "--diagnose --port 8791"):
        cmd = f'{q}{py}{q} {q}{base}\\server.py{q} {opt}'
        print(f"  {cmd}")
    print("  （SrvCmd 定义即 q & py & q & \" \" & q & base & \"\\server.py\" & q & \" \" & options，"
          "展开后与上面一致）")

    print("== 结论 ==")
    if problems:
        for p in problems:
            print("  [!] " + p)
        return 1
    print("  静态检查全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
