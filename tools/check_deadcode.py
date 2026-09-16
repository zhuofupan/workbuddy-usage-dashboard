#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
静态检查 app.js 里的「顶层死代码」——语法检查查不出来的那类 bug。

真实事故：把一个调用改成 `return foo()`（为了配合它变成 async），
结果同一个函数里它**后面**的代码全部不再执行 —— `node --check` 通过、
逻辑"看着没动"，但页面上那几块直接空白。是用户截图才发现的。

本脚本用花括号配平切出每个函数的函数体（不用 `.*?\\n\\}` 这种会吞掉相邻函数的正则），
再找**缩进正好 2 空格的 return**（= 函数体顶层的 return），若其后还有代码就报错。

用法：python tools/check_deadcode.py [路径，默认 static/app.js]
"""

import os
import re
import sys

FUNCS = [
    'renderOverview', 'renderSeries', 'renderTurns', 'renderSessions', 'renderModels',
    'renderKpis', 'renderIdents', 'renderMonth', 'renderTurnChart', 'renderPager',
    'syncSwitchUI', 'syncUrl', 'syncSortIndicator', 'syncMetricColumns',
]


def function_body(src, name):
    """用花括号配平切出函数体（比跨行正则可靠）。"""
    m = re.search(r'(?:async\s+)?function\s+%s\s*\([^)]*\)\s*\{' % re.escape(name), src)
    if not m:
        return None
    i = src.find('{', m.end() - 1)
    depth = 0
    start = i
    while i < len(src):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return src[start + 1:i]
        i += 1
    return None


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'app.js')
    src = open(path, encoding='utf-8').read()

    problems = []
    checked = 0
    for fn in FUNCS:
        body = function_body(src, fn)
        if body is None:
            continue
        checked += 1
        lines = body.split('\n')
        for n, line in enumerate(lines):
            # 缩进正好 2 空格 = 函数体顶层的 return（回调里的 return 缩进更深，不算）
            if not re.match(r'^  return\b', line):
                continue
            rest = [x for x in lines[n + 1:] if x.strip()]
            if rest:
                problems.append((fn, n + 1, len(rest)))

    print(f'检查文件：{os.path.relpath(path)}')
    print(f'覆盖函数：{checked} 个')
    for fn, line_no, count in problems:
        print(f'  [!] {fn} 函数体第 {line_no} 行是顶层 return，但后面还有 {count} 行代码 ——')
        print('      这些代码永远不会执行（通常是想写 await 却写成了 return）')
    if problems:
        print(f'=> 发现 {len(problems)} 处顶层死代码')
        return 1
    print('=> 未发现顶层死代码')
    return 0


if __name__ == '__main__':
    sys.exit(main())
