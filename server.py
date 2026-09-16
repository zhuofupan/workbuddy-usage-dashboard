#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 用量实时看板 —— 后端服务

职责：
  1. 增量 tail ~/.workbuddy/projects/**/*.jsonl（按字节偏移只读新增部分，不全量重扫）
  2. 把每条 LLM 请求的 usage、用户提问、工具调用、会话标题解析进内存索引
  3. 以「用户提问」为边界切分轮次，支持细到 账号 / 设备 / 项目 / 会话 / 轮次 / 模型档位
  4. 提供 REST API 与 SSE 实时推送

仅监听 127.0.0.1，不对外暴露。
数据全部来自本地文件，不上传任何东西。
"""

import argparse
import bisect
import calendar
import csv
import datetime as dt
import errno
import glob
import html
import io
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
PID_FILE = os.path.join(ROOT_DIR, "dashboard.pid")
LOG_FILE = os.path.join(ROOT_DIR, "dashboard.log")
# 本地个人数据集中放在这一个文件里，已被 .gitignore 排除，不会提交到仓库：
#   {
#     "quota":         {"plan": 10000, "remaining": 5000, "remainingAt": 0, "resetDay": 1},
#     "sessionTitles": {"<sessionId 或前 8 位前缀>": "我的名字"}
#   }
# 其中 quota 是「本月剩余」用的额度（本地任何文件都没有账户余额，必须由用户提供）；
# sessionTitles 是手写会话名（界面里的改名不会落盘，只能这样补）。
# 模板见 config.example.json，复制成 config.local.json 即可。
CONFIG_FILE = os.path.join(ROOT_DIR, "config.local.json")
USAGE_PAGE = "https://www.codebuddy.cn/profile/usage"
SERVER_TAG = "WBUsage"          # 响应头里的指纹，用来判断端口上跑的是不是本看板

# pythonw.exe 启动时没有控制台：print 是空操作，出错必须弹窗，否则完全静默
WINDOWLESS = os.path.basename(sys.executable).lower().startswith("pythonw") or sys.stdout is None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WB_ROOT = os.path.join(os.path.expanduser("~"), ".workbuddy")
PROJECTS_DIR = os.path.join(WB_ROOT, "projects")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

DAY_MS = 86400 * 1000
# 用户提问常被包在 <system-reminder>（上下文注入）里，真实提问在同一消息的后半段，
# 所以不能整条丢弃，必须先剥掉注入块再判断。
SR_RE = re.compile(r"<system-reminder\b[^>]*>.*?</system-reminder>", re.S | re.I)
UQ_RE = re.compile(r"<user_query>(.*?)</user_query>", re.S | re.I)
# 自动压缩 / 自动续跑 / 后台任务通知 / 斜杠命令 都不是用户主动提问，单独标注
COMPACT_RE = re.compile(r"<conversation_history_summary>", re.I)
CONTINUE_RE = re.compile(r"Please continue with the conversation based on the summarized context", re.I)
NOTIFY_RE = re.compile(r"<task-notification\b", re.I)
COMMAND_RE = re.compile(r"<command-message\b", re.I)
TAG_RE = re.compile(r"<(\w[\w-]*)>(.*?)</\1>", re.S)


def tag_text(raw, name):
    """按标签名取内容。不要用 findall 一次抓全部：非重叠匹配会被最外层标签整段吃掉。"""
    m = re.search(rf"<{name}>(.*?)</{name}>", raw, re.S | re.I)
    return html.unescape(m.group(1).strip()) if m else ""


def notification_label(raw):
    """把 <task-notification> 的 XML 摘成一句人话，别把原始标签塞进表格。"""
    bits = []
    tid = tag_text(raw, "task-id")
    status = tag_text(raw, "status")
    summary = tag_text(raw, "summary")
    if tid:
        bits.append("任务 " + tid)
    if status:
        bits.append(status)
    if summary:
        bits.append(one_line_local(summary, 90))
    return "后台任务通知：" + " · ".join(bits) if bits else "后台任务通知"


def one_line_local(s, n):
    t = " ".join(str(s or "").split())
    return t if len(t) <= n else t[:n] + "…"


def now_ms():
    return int(time.time() * 1000)


# ------------------------------------------------------------------ 进程与日志
def log(msg):
    """总是写日志：pythonw 下没有控制台，日志是唯一的排障线索。"""
    line = f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 2 * 1024 * 1024:
            os.replace(LOG_FILE, LOG_FILE + ".old")
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    try:
        print(msg)
    except Exception:
        pass


def fatal(msg, title="WorkBuddy 用量看板"):
    """致命错误：写日志，无控制台时弹窗，绝不让用户面对"双击没反应"。"""
    log("[!] " + msg)
    if WINDOWLESS:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, title, 0x10)
        except Exception:
            pass
    return 1


def http_fingerprint(host, port, family, timeout=0.8):
    """连过去发一个最小 GET，看响应头里有没有本看板的指纹。"""
    s = socket.socket(family)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.sendall(b"GET /api/meta HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        buf = b""
        while len(buf) < 2048:
            chunk = s.recv(2048)
            if not chunk:
                break
            buf += chunk
        return SERVER_TAG.encode() in buf
    except OSError:
        return False
    finally:
        s.close()


def http_get_json(host, port, family, path, timeout=1.0):
    """向运行中的看板要一小段 JSON（用于取它的 pid）。"""
    s = socket.socket(family)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
        buf = b""
        while True:
            chunk = s.recv(4096)
            if not chunk or len(buf) > 65536:
                break
            buf += chunk
        if SERVER_TAG.encode() not in buf:
            return None
        _, _, body = buf.partition(b"\r\n\r\n")
        return json.loads(body.decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    finally:
        s.close()


def find_running(port):
    """已有一个看板在跑？（port 起往上找几号，认指纹不认端口号）"""
    for p in range(port, port + 4):
        for host, family in (("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)):
            if http_fingerprint(host, p, family):
                return p
    return None


def running_pid(port):
    """
    直接问运行中的实例要它自己的 pid。

    为什么不只靠 dashboard.pid：多实例并存时，**先退出的那个会把 pid 文件删掉**，
    幸存者就失去了控制手段（实测踩过：端口还在听，但 --stop 找不到 pid）。
    所以 pid 文件只作为兜底，主路径是 HTTP 自报。
    """
    for host, family in (("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)):
        d = http_get_json(host, port, family, "/api/whoami")
        if isinstance(d, dict) and isinstance(d.get("pid"), int):
            return d["pid"]
    return None


def stop_running(port=8791):
    """先确认端口上真有我们的看板，再按 pid 结束；没有就顺手清掉残留 pid 文件。"""
    running = find_running(port)
    if not running:
        note = ""
        if os.path.exists(PID_FILE):
            try:
                os.remove(PID_FILE)
                note = "（已清理残留的 dashboard.pid）"
            except OSError:
                pass
        return False, "当前没有运行中的看板" + note

    pid = running_pid(running)                       # 主路径：让它自己报
    if not pid and os.path.exists(PID_FILE):         # 兜底：pid 文件（旧版本没这接口）
        try:
            pid = int(open(PID_FILE, encoding="utf-8").read().strip())
        except (OSError, ValueError):
            pid = None
    if not pid:
        return False, (f"看板在端口 {running} 上运行，但取不到它的进程号，"
                       "请在任务管理器里结束对应的 python.exe")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return False, f"结束进程 {pid} 失败：{exc.strerror or exc}"
    for _ in range(20):                     # 等端口真正释放
        time.sleep(0.15)
        if not find_running(port):
            break
    if os.path.exists(PID_FILE):
        try:
            if open(PID_FILE, encoding="utf-8").read().strip() == str(pid):
                os.remove(PID_FILE)
        except OSError:
            pass
    return True, f"已停止看板（进程 {pid}，端口 {running}）"


def write_pid(port):
    try:
        with open(PID_FILE, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass


def diagnose(port):
    """
    启动失败时给用户看的东西。放这里而不是 .vbs 里：
    wscript 只按 ANSI/UTF-16 读源码，UTF-8 中文会乱码，所以中文提示统一由 Python 弹。
    """
    import textwrap
    tail = ""
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, encoding="utf-8", errors="replace") as fh:
                raw = fh.read().splitlines()[-16:]
            tail = "\n".join(textwrap.fill(x, 86) for x in raw if x.strip())
    except OSError:
        tail = "(读取日志失败)"

    exe = sys.executable
    msg = "\n".join([
        "看板没能启动，或者本机回环端口被系统/安全软件拦截了。",
        "",
        f"· 检测端口：{port}（上面没有发现看板进程）",
        f"· 日志文件：{LOG_FILE}",
        "" if tail else "· 日志不存在 —— 说明进程根本没跑起来，多半是 python 路径或权限问题",
        "建议按顺序试：",
        "  1) 等 5 秒再双击一次。本机回环偶发抽风，重试通常就好了。",
        "  2) 换个端口启动（命令行执行）：",
        f'     "{exe}" "{os.path.join(ROOT_DIR, "server.py")}" --port 8899 --open',
        "  3) 兜底：双击看板目录里的 start.bat —— 它会保留一个命令行窗口，"
        "能看到完整输出和报错。",
        "  4) 检查安全软件/代理软件是否拦截了 python.exe 的本机监听。",
        "",
        "— 最近日志 —",
        tail or "(空)",
    ])
    log("[diagnose] " + msg)
    try:                            # 无论有无控制台都弹：用户是双击点进来的
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "WorkBuddy 用量看板", 0x30)
    except Exception:
        pass
    return 1


def day_str(ts_ms):
    return dt.datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")


def hour_str(ts_ms):
    return dt.datetime.fromtimestamp(ts_ms / 1000).strftime("%H")


def parse_date(s, end=False):
    """YYYY-MM-DD -> 毫秒时间戳；end=True 时取当天 23:59:59.999"""
    if not s:
        return None
    try:
        d = dt.datetime.strptime(s.strip()[:10], "%Y-%m-%d")
    except ValueError:
        return None
    if end:
        d += dt.timedelta(days=1)
    return int(d.timestamp() * 1000)


# ------------------------------------------------------------------ 身份解析
def resolve_identity():
    """
    账号与设备在会话记录里没有逐请求落盘，因此按「安装实例」归属。
    这里从 WorkBuddy 自己的几个配置文件里把真实标识读出来，不猜。
    """
    accounts, devices = [], []

    # --- 账号：settings.json 的 claw.users + connectors 的 accountIdentityKey
    seen = set()
    settings_path = os.path.join(WB_ROOT, "settings.json")
    if os.path.exists(settings_path):
        try:
            settings = json.load(open(settings_path, encoding="utf-8"))
            for uid in (settings.get("claw", {}).get("users") or {}):
                if uid not in seen:
                    seen.add(uid)
                    accounts.append({"id": uid, "type": "", "source": "settings.json"})
        except (OSError, ValueError):
            pass

    for p in glob.glob(os.path.join(WB_ROOT, "connectors", "*", "connector-states*.json")):
        try:
            st = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        key = st.get("accountIdentityKey") or ""
        if "||" not in key:
            continue
        uid, _, kind = key.partition("||")
        for a in accounts:
            if a["id"] == uid:
                a["type"] = kind
                a["source"] = "connectors"
                break
        else:
            accounts.append({"id": uid, "type": kind, "source": "connectors"})

    for a in accounts:
        label = a["id"][:8]
        if a.get("type"):
            label += f" · {a['type']}"
        a["label"] = label

    # --- 设备：device-id + 主机名 + 客户端版本
    device_id = ""
    p = os.path.join(WB_ROOT, "device-id")
    if os.path.exists(p):
        device_id = open(p, encoding="utf-8", errors="replace").read().strip()

    version = ""
    p = os.path.join(WB_ROOT, "last-launch.json")
    if os.path.exists(p):
        try:
            version = json.load(open(p, encoding="utf-8")).get("version", "")
        except (OSError, ValueError):
            pass

    hostname = socket.gethostname()
    qimei = ""
    p = os.path.join(WB_ROOT, "qimei-cache.json")
    if os.path.exists(p):
        try:
            qimei = json.load(open(p, encoding="utf-8")).get("qimei36", "")
        except (OSError, ValueError):
            pass

    if device_id:
        devices.append({
            "id": device_id,
            "label": f"{hostname} · {device_id[:8]}",
            "hostname": hostname,
            "os": sys.platform,
            "version": version,
            "qimei": qimei[:12],
            "source": "device-id",
        })

    # --- 会话 → 主机名映射（sessions/<pid>.json），用于给历史会话标注设备
    sess_dev = {}
    for p in glob.glob(os.path.join(WB_ROOT, "sessions", "*.json")):
        try:
            s = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sid = s.get("sessionId")
        if sid:
            sess_dev[sid] = {
                "hostname": s.get("hostname") or hostname,
                "os": s.get("os") or sys.platform,
                "version": s.get("version") or version,
            }

    return {
        "accounts": accounts,
        "devices": devices,
        "sessions": sess_dev,
        "primary_account": accounts[0]["id"] if accounts else "",
        "primary_device": devices[0]["id"] if devices else "",
    }


# ------------------------------------------------------------------ 索引
class UsageIndex:
    """增量索引：只读文件新增的字节。"""

    def __init__(self, projects_dir=PROJECTS_DIR, scan_interval=2.0):
        self.projects_dir = projects_dir
        self.scan_interval = scan_interval
        self.lock = threading.RLock()
        self.requests = []          # 每次 LLM 请求一条
        self.prompts = []           # 用户提问
        self.tools = []             # 工具调用（只留 session/ts/name）
        self.titles = {}            # sessionId -> 标题
        self._files = {}            # path -> {offset, mtime, size}
        self.version = 0
        self.last_scan = 0
        self._turns = None
        self._turns_ver = None
        self.title_ts = {}          # sessionId -> 该标题的时间戳（用于"取最新"）
        self.overrides = {}         # sessionId / 前缀 -> 手写名字
        self._ov_mtime = -1.0
        self.identity = resolve_identity()
        self.error = ""
        self.scan_full()

    # ---------- 增量读取
    def _scan_file(self, path):
        st = os.stat(path)
        state = self._files.get(path)
        if state and state["size"] == st.st_size:
            return 0
        offset = state["offset"] if state else 0
        if state and st.st_size < state["size"]:
            offset = 0                     # 文件被截断/重写，从头来
            self._purge_file(path)
        added = 0
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                buf = fh.read()
        except OSError:
            return 0
        if not buf:
            self._files[path] = {"offset": offset, "size": st.st_size, "mtime": st.st_mtime}
            return 0
        # 只处理完整的行，末尾不完整片段留到下次
        cut = buf.rfind(b"\n")
        if cut < 0:
            self._files[path] = {"offset": offset, "size": st.st_size, "mtime": st.st_mtime}
            return 0
        chunk, consumed = buf[:cut], offset + cut + 1
        session_guess = os.path.splitext(os.path.basename(path))[0]
        for raw in chunk.split(b"\n"):
            if not raw.strip():
                continue
            n = self._ingest_line(raw, path, session_guess)
            added += n
        self._files[path] = {"offset": consumed, "size": st.st_size, "mtime": st.st_mtime}
        return added

    def _purge_file(self, path):
        """文件被重写时清掉它贡献的记录。"""
        self.requests = [r for r in self.requests if r["_file"] != path]
        self.prompts = [p for p in self.prompts if p["_file"] != path]
        self.tools = [t for t in self.tools if t["_file"] != path]

    def _ingest_line(self, raw, path, session_guess):
        if b'"usage"' not in raw and b'"aiTitle"' not in raw and b'"role":"user"' not in raw:
            return 0
        try:
            rec = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return 0
        rtype = rec.get("type")
        ts = rec.get("timestamp") or 0
        session = rec.get("sessionId") or session_guess
        cwd = rec.get("cwd") or ""
        pd = rec.get("providerData")
        added = 0

        # 会话标题：同一会话会有多条 ai-title（用户边聊边改主题、或手动改名后重新生成），
        # 必须按**时间戳**取最新 —— 只按文件顺序取最后一条，遇到乱序写入就会显示旧名字。
        if rtype == "ai-title" and rec.get("aiTitle"):
            title = rec["aiTitle"].strip()
            t2 = rec.get("timestamp") or 0
            if title and t2 >= self.title_ts.get(session, -1):
                self.titles[session] = title
                self.title_ts[session] = t2

        # 工具调用计数
        if rtype == "function_call":
            self.tools.append({"ts": ts, "session": session, "_file": path,
                               "name": rec.get("name") or ""})

        # 用户提问
        if rtype == "message" and rec.get("role") == "user":
            content = rec.get("content") or []
            texts = [c.get("text", "") for c in content
                     if isinstance(c, dict) and c.get("type") == "input_text"]
            blob = "\n".join(texts)
            clean = SR_RE.sub("", blob).strip() if blob else ""
            if clean:
                if NOTIFY_RE.search(clean):
                    kind, text = "notify", notification_label(clean)
                elif COMMAND_RE.search(clean):
                    kind = "command"
                    text = tag_text(clean, "command-message") or one_line_local(clean, 120)
                elif COMPACT_RE.search(clean):
                    kind = "compact"
                    text = f"（自动压缩上下文：摘要 {len(clean):,} 字）"
                elif CONTINUE_RE.search(clean):
                    kind = "continue"
                    text = "（压缩后自动续跑）"
                else:
                    kind = "user"
                    m = UQ_RE.search(clean)
                    text = m.group(1).strip() if m else clean
                self.prompts.append({
                    "ts": ts, "session": session, "cwd": cwd, "_file": path,
                    "id": rec.get("id") or "",
                    "text": text,
                    "kind": kind,
                    "raw_len": len(clean),
                    "images": sum(1 for c in content
                                  if isinstance(c, dict) and c.get("type") == "image_blob_ref"),
                })

        # LLM 请求用量
        if isinstance(pd, dict):
            u = pd.get("usage")
            if isinstance(u, dict) and u.get("totalTokens"):
                ru = pd.get("rawUsage") or {}
                itd = (u.get("inputTokensDetails") or [{}])[0]
                otd = (u.get("outputTokensDetails") or [{}])[0]
                model = pd.get("model") or "unknown"
                scene_id = pd.get("requestModelId") or model
                self.requests.append({
                    "_file": path,
                    "ts": ts,
                    "session": session,
                    "cwd": cwd,
                    "msg": pd.get("messageId") or "",
                    "trace": pd.get("traceId") or "",
                    "creq": pd.get("conversationRequestId") or "",
                    "model": model,
                    "model_name": pd.get("requestModelName") or model,
                    "scene_id": scene_id,
                    "inp": u.get("inputTokens") or 0,
                    "out": u.get("outputTokens") or 0,
                    "total": u.get("totalTokens") or 0,
                    "cached": itd.get("cached_tokens") or 0,
                    "reasoning": otd.get("reasoning_tokens") or 0,
                    "hit": ru.get("prompt_cache_hit_tokens") or 0,
                    "miss": ru.get("prompt_cache_miss_tokens") or 0,
                    "credit": float(ru.get("credit") or 0),
                    "agent": pd.get("agent") or "",
                    "queue": (pd.get("queuePosition") or 0),
                    "errored": bool(pd.get("error")),
                })
                added += 1
        return added

    # ---------- 扫描循环
    def _load_overrides(self):
        """
        读可选的手写会话名（config.local.json 的 sessionTitles）。

        为什么需要它：界面里手动改的会话名**不落任何本地文件**（实测客户端 profile、
        artifact-index、sessions/*.json 里都没有），本地唯一的标题来源是 jsonl 里自动生成的
        aiTitle。所以想要用自己起的名字，只能在这里显式补一份映射。
        """
        try:
            mt = os.path.getmtime(CONFIG_FILE)
        except OSError:
            if self.overrides:
                self.overrides = {}
                self._ov_mtime = -1.0
            return
        if mt == self._ov_mtime:
            return
        name = os.path.basename(CONFIG_FILE)
        data = _read_local_config().get("sessionTitles")
        if not isinstance(data, dict):
            if data is not None:
                log(f'[!] {name} 的 sessionTitles 应为对象：'
                    '{"<会话ID 或前8位>": "名字"}')
            self._ov_mtime = mt
            return
        # 以 _ 开头的键是自己写的说明，不是会话 ID，必须跳过
        self.overrides = {str(k): str(v).strip() for k, v in data.items()
                          if not str(k).startswith("_") and str(v).strip()}
        self._ov_mtime = mt
        log(f"[+] 已载入 {len(self.overrides)} 条手写会话名（{name} · sessionTitles）")

    def title_of(self, session):
        """会话展示名：手写覆盖 > 时间戳最新的 aiTitle > 空。"""
        if self.overrides:
            if session in self.overrides:
                return self.overrides[session]
            for k, v in self.overrides.items():      # 允许只写前 8 位前缀
                if session.startswith(k):
                    return v
        return self.titles.get(session, "")

    def scan_full(self):
        added = 0
        with self.lock:
            self._load_overrides()
            files = glob.glob(os.path.join(self.projects_dir, "**", "*.jsonl"), recursive=True)
            current = set(files)
            for gone in set(self._files) - current:
                self._purge_file(gone)
                self._files.pop(gone, None)
            for path in files:
                try:
                    added += self._scan_file(path)
                except OSError:
                    continue
            self.last_scan = now_ms()
            if added:
                self.version += 1
        return added

    def start(self):
        def loop():
            while True:
                try:
                    self.scan_full()
                except Exception as exc:            # 后台线程不能死
                    self.error = f"{type(exc).__name__}: {exc}"
                time.sleep(self.scan_interval)

        t = threading.Thread(target=loop, daemon=True, name="wb-index-scanner")
        t.start()
        return t

    # ---------- 轮次切分
    def build_turns(self):
        """
        轮次 = 一次用户提问到下一次提问之间的所有 API 请求。
        之所以不按 conversationRequestId 分组：实测它与提问数不等（一条提问可能
        对应多个 convReq，也可能是系统补发），按提问时间轴切分才是用户认知里的「一轮」。
        """
        ver = self.data_version()
        if self._turns is not None and self._turns_ver == ver:
            return self._turns
        # 取快照后释放锁：purge 是整体替换列表，快照始终自洽，不必长期持锁
        with self.lock:
            requests = list(self.requests)
            prompts = list(self.prompts)
            tools = list(self.tools)
        ident = self.identity
        acct = ident["primary_account"]
        dev = ident["primary_device"]

        prompts_by_session = {}
        for p in prompts:
            prompts_by_session.setdefault(p["session"], []).append(p)
        for lst in prompts_by_session.values():
            lst.sort(key=lambda x: x["ts"])

        # 每个会话的轮次骨架
        turns, turn_of_session = [], {}
        for session, plist in prompts_by_session.items():
            cwd = plist[0]["cwd"] or ""
            arr = []
            for i, p in enumerate(plist, 1):
                sm = ident["sessions"].get(session) or {}
                arr.append({
                    "id": f"{session}#{i}",
                    "session": session,
                    "cwd": cwd,
                    "project": os.path.basename(cwd.rstrip("\\/")) or cwd or "(未知)",
                    "account": acct,
                    "device": dev,
                    "hostname": sm.get("hostname") or (ident["devices"][0]["hostname"] if ident["devices"] else ""),
                    "client": sm.get("version") or (ident["devices"][0]["version"] if ident["devices"] else ""),
                    "index": i,
                    "prompt": p["text"],
                    "kind": p.get("kind", "user"),
                    "images": p["images"],
                    "start": p["ts"],
                    "end": p["ts"],
                    "reqs": [],
                    "tools": 0,
                    "models": {},
                })
            turn_of_session[session] = arr
            turns.extend(arr)

        # 请求归入轮次
        pkeys = {s: [p["ts"] for p in lst] for s, lst in prompts_by_session.items()}
        orphan = []
        for r in requests:
            lst = turn_of_session.get(r["session"])
            if not lst:
                orphan.append(r)
                continue
            keys = pkeys[r["session"]]
            i = bisect.bisect_right(keys, r["ts"]) - 1
            if i < 0:
                orphan.append(r)
                continue
            t = lst[i]
            t["reqs"].append(r)
            t["end"] = max(t["end"], r["ts"])
            t["models"][r["model"]] = t["models"].get(r["model"], 0) + r["credit"]

        # 工具调用归入轮次
        for tl in tools:
            lst = turn_of_session.get(tl["session"])
            if not lst:
                continue
            keys = pkeys[tl["session"]]
            i = bisect.bisect_right(keys, tl["ts"]) - 1
            if i >= 0:
                lst[i]["tools"] += 1

        # 没有对应提问的请求（后台摘要、标题生成等）归到一个虚拟轮
        if orphan:
            by_sess = {}
            for r in orphan:
                by_sess.setdefault(r["session"], []).append(r)
            for session, rs in by_sess.items():
                cwd = rs[0]["cwd"] or ""
                sm = ident["sessions"].get(session) or {}
                t = {
                    "id": f"{session}#0",
                    "session": session,
                    "cwd": cwd,
                    "project": os.path.basename(cwd.rstrip("\\/")) or cwd or "(未知)",
                    "account": acct, "device": dev,
                    "hostname": sm.get("hostname") or "",
                    "client": sm.get("version") or "",
                    "index": 0,
                    "prompt": "（后台任务：无对应用户提问）",
                    "kind": "orphan",
                    "images": 0,
                    "start": min(r["ts"] for r in rs),
                    "end": max(r["ts"] for r in rs),
                    "reqs": rs, "tools": 0, "models": {},
                    "orphan": True,
                }
                for r in rs:
                    t["models"][r["model"]] = t["models"].get(r["model"], 0) + r["credit"]
                turns.append(t)

        for t in turns:
            t["req_count"] = len(t["reqs"])
            t["duration"] = (t["end"] - t["start"]) if t["end"] >= t["start"] else 0
        turns.sort(key=lambda x: x["start"])
        self._turns = turns
        self._turns_ver = ver
        return turns

    def data_version(self):
        return f"{self.version}-{len(self.requests)}"


def parse_query(query):
    """解析查询串。

    两个坑：
    1. BaseHTTPRequestHandler 用 latin-1 解请求行，URL 里直接塞非 ASCII（curl / 手粘链接）
       会变成乱码，先按 latin-1 还原字节再按 UTF-8 解。纯 ASCII 与 %XX 编码不受影响。
    2. 终端是 GBK 时中文参数按 GBK 发出，解码出现替换字符则回退 GBK 再试一次。
    """
    try:
        fixed = query.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        fixed = query
    q = parse_qs(fixed)
    if any("\ufffd" in v for vs in q.values() for v in vs):
        try:
            q2 = parse_qs(fixed, encoding="gbk", errors="replace")
        except (LookupError, ValueError):
            return q
        if not any("\ufffd" in v for vs in q2.values() for v in vs):
            return q2
    return q


# ------------------------------------------------------------------ 聚合
SUM_FIELDS = ("inp", "out", "total", "cached", "reasoning", "credit", "miss", "hit", "req")


def blank():
    return dict.fromkeys(SUM_FIELDS, 0)


def acc(b, r):
    for k in ("inp", "out", "total", "cached", "reasoning", "miss", "hit"):
        b[k] += r[k]
    b["credit"] += r["credit"]
    b["req"] += 1
    return b


def apply_filters(requests, turns, q, titles=None):
    """按查询条件过滤请求与轮次。"""
    titles = titles or {}
    frm = parse_date(q.get("from", [""])[0])
    to = parse_date(q.get("to", [""])[0], end=True)
    account = (q.get("account", [""])[0] or "").strip()
    device = (q.get("device", [""])[0] or "").strip()
    project = (q.get("project", [""])[0] or "").strip()
    session = (q.get("session", [""])[0] or "").strip()
    model = (q.get("model", [""])[0] or "").strip()
    scene = (q.get("scene", [""])[0] or "").strip()
    kw = (q.get("q", [""])[0] or "").strip().lower()

    # 关键词命中「轮」而不是「会话」：命中哪个提问就只留哪一轮的请求，
    # 否则搜一个词会把整个会话的历史请求全捞回来。
    kw_turn_ids = None
    kw_msgs = None
    if kw:
        kw_turn_ids, kw_msgs = set(), set()
        for t in turns:
            hay = " ".join([
                t.get("prompt") or "", t.get("project") or "", t.get("cwd") or "",
                titles.get(t["session"], ""), t["session"],
            ]).lower()
            if kw in hay:
                kw_turn_ids.add(t["id"])
                for r in t["reqs"]:
                    kw_msgs.add(r["msg"] or id(r))

    def ok_req(r):
        if frm and r["ts"] < frm:
            return False
        if to and r["ts"] >= to:
            return False
        if project and r["cwd"] != project:
            return False
        if session and r["session"] != session:
            return False
        if model and r["model"] != model:
            return False
        if scene and r["scene_id"] != scene:
            return False
        if kw_msgs is not None and (r["msg"] or id(r)) not in kw_msgs:
            return False
        return True

    def ok_turn(t):
        if frm and t["end"] < frm:
            return False
        if to and t["start"] >= to:
            return False
        if account and t["account"] != account:
            return False
        if device and t["device"] != device:
            return False
        if project and t["cwd"] != project:
            return False
        if session and t["session"] != session:
            return False
        if kw_turn_ids is not None and t["id"] not in kw_turn_ids:
            return False
        return True

    freqs = [r for r in requests if ok_req(r)]
    fturns = []
    for t in turns:
        if not ok_turn(t):
            continue
        rs = [r for r in t["reqs"] if ok_req(r)]
        if not rs and (model or scene):
            continue                      # 按模型过滤后该轮无请求就不显示
        tt = dict(t)
        tt["reqs"] = rs
        tt["req_count"] = len(rs)
        fturns.append(tt)
    return freqs, fturns


def group_by(items, keyfn):
    out = {}
    for it in items:
        out.setdefault(keyfn(it), blank())
        acc(out[keyfn(it)], it)
    return out


# 「均衡 / 快速」是自动档位的显示名，不是模型名。requestModelName 有时填的是档位，
# 直接拿来当模型名会把 deepseek-v4.1-flash 显示成「均衡」，所以必须分开处理。
AUTO_SCENE_NAMES = {"均衡", "快速"}
SCENE_LABELS = {"balanced-model": "均衡（自动档）", "fast-model": "快速（自动档）"}


def pretty_model(mid):
    return mid.split(":", 1)[1] if mid.startswith("custom-local:") else mid


def model_display_names(requests):
    """为每个真实模型 id 选一个像样的展示名：优先非档位的 requestModelName。"""
    votes = {}
    for r in requests:
        n = r["model_name"]
        if n in AUTO_SCENE_NAMES or n == r["model"]:
            continue
        votes.setdefault(r["model"], {})
        votes[r["model"]][n] = votes[r["model"]].get(n, 0) + 1
    out = {}
    for r in requests:
        mid = r["model"]
        if mid in out:
            continue
        v = votes.get(mid)
        out[mid] = max(v, key=v.get) if v else pretty_model(mid)
    return out


def scene_labels(requests):
    labels = dict(SCENE_LABELS)
    for r in requests:
        sid = r["scene_id"]
        if sid in labels:
            continue
        labels[sid] = r["model_name"] if r["model_name"] != sid else pretty_model(sid)
    return labels


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f >= 0 else None


def _read_local_config():
    """
    读整个 config.local.json（本地个人数据，已被 .gitignore 排除）。

    文件不存在、读不动、JSON 坏掉，一律返回 {} —— 这些都属于"用户还没配"，
    不该让看板起不来。以 _ 开头的键当作注释，读的时候就过滤掉。
    """
    try:
        raw = open(CONFIG_FILE, encoding="utf-8").read()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log(f"[!] 读取 {os.path.basename(CONFIG_FILE)} 失败：{exc}")
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        log(f"[!] {os.path.basename(CONFIG_FILE)} 解析失败，已忽略：{exc}")
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


def _write_local_config(data):
    """
    原子写回整个 config.local.json（先写临时文件再替换，避免写坏）。

    注意：调用方必须把**整份** data 传进来。只写 quota 会把 sessionTitles 冲掉 ——
    所以在下面各自负责"读整份 → 改自己那块 → 写整份"。
    """
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


def load_config():
    """
    读 config.local.json 的 quota 段（界面上的「设置」保存的就是它）。

    字段：
      plan        每月套餐额度（积分）
      remaining   保存那一刻的「当前剩余」（积分）
      remainingAt 保存剩余量的时刻（ms）；之后新增的消耗从这上面扣
      resetDay    每月刷新日（1-31），决定"本周期"从哪天算起
    兼容旧写法 monthlyCredit。没配过就返回 {}。
    """
    data = _read_local_config().get("quota")
    if not isinstance(data, dict):
        return {}

    cfg = {}
    plan = _num(data.get("plan", data.get("monthlyCredit")))
    if plan is not None:
        cfg["plan"] = plan
    rem = _num(data.get("remaining"))
    if rem is not None:
        cfg["remaining"] = rem
        at = _num(data.get("remainingAt"))
        cfg["remainingAt"] = int(at) if at else 0
    day = _num(data.get("resetDay"))
    cfg["resetDay"] = int(day) if day and 1 <= day <= 31 else 1
    return cfg


def save_config(patch):
    """
    把 quota 合并写回 config.local.json（原子写：先写临时文件再替换，避免写坏）。

    只动 quota 这一段，sessionTitles 等其它键**原样保留** ——
    所以读整份、改一块、写整份。
    """
    root = _read_local_config()
    cfg = load_config()
    for key in ("plan", "remaining"):
        if key in patch:
            v = _num(patch[key])
            if v is None:
                cfg.pop(key, None)
                if key == "remaining":
                    cfg.pop("remainingAt", None)
            else:
                cfg[key] = v
                if key == "remaining":
                    # 以"保存这一刻"为基准：之后新增的消耗从它上面扣
                    cfg["remainingAt"] = int(time.time() * 1000)
    if "resetDay" in patch:
        d = _num(patch["resetDay"])
        cfg["resetDay"] = int(d) if d and 1 <= d <= 31 else 1
    cfg.setdefault("resetDay", 1)
    root["quota"] = cfg
    _write_local_config(root)
    log(f"[+] 已保存额度设置：{cfg}")
    return cfg


def _month_days(y, m):
    return calendar.monthrange(y, m)[1]


def cycle_start(now, reset_day):
    """本周期起点：最近一次「刷新日」的 00:00（含当天）。"""
    def make(y, m):
        return dt.datetime(y, m, min(reset_day, _month_days(y, m)))

    cur = make(now.year, now.month)
    if now >= cur:
        return cur
    y, m = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    return make(y, m)


def cycle_next(now, reset_day):
    y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return dt.datetime(y, m, min(reset_day, _month_days(y, m)))


def month_payload(idx, now=None):
    """
    本周期用量 + 剩余额度。这块**不受看板筛选影响**（余额是账户级的，
    跟着"项目/日期"筛选变就会得出错误的"剩余"）。

    剩余的算法，优先级从高到低：
      1) 你填过「当前剩余」且填的时间还在本周期内 → 剩余 = 你填的数 − 之后新增的消耗
      2) 只填了套餐额度 → 剩余 = 套餐额度 − 本周期已消耗
      3) 都没填 → 不算
    """
    now = now or dt.datetime.now()
    cfg = load_config()
    reset_day = cfg.get("resetDay", 1)
    start = cycle_start(now, reset_day)
    nxt = cycle_next(now, reset_day)
    start_ms = start.timestamp() * 1000

    used_cycle = 0.0
    reqs = 0
    for r in idx.requests:
        if r["ts"] >= start_ms:
            used_cycle += r["credit"]
            reqs += 1

    plan = cfg.get("plan")
    snap_rem = cfg.get("remaining")
    snap_at = cfg.get("remainingAt") or 0
    remaining = None
    used_since = None
    source = "none"
    stale = False

    if snap_rem is not None:
        if snap_at >= start_ms:
            used_since = sum(r["credit"] for r in idx.requests if r["ts"] >= snap_at)
            remaining = snap_rem - used_since
            source = "snapshot"
        else:
            # 快照早于本周期 —— 跨过刷新日了，那份剩余量已经过期
            stale = True
            if plan is not None:
                remaining = plan - used_cycle
                source = "plan"
    elif plan is not None:
        remaining = plan - used_cycle
        source = "plan"

    days_left = max(1, (nxt.date() - now.date()).days)
    return {
        "cycleStart": start.strftime("%Y-%m-%d"),
        "cycleEnd": (nxt - dt.timedelta(days=1)).strftime("%Y-%m-%d"),
        "cycleNext": nxt.strftime("%Y-%m-%d"),
        "resetDay": reset_day,
        "used": round(used_cycle, 2),
        "requests": reqs,
        "plan": plan,
        "quota": plan,                      # 兼容旧前端字段名
        "remaining": round(remaining, 2) if remaining is not None else None,
        "usedRatio": (1 - remaining / plan) if (plan and remaining is not None) else None,
        "daysLeft": days_left,
        "perDayLeft": round(remaining / days_left, 2) if (remaining is not None and days_left > 0) else None,
        "source": source,
        "snapshot": ({"remain": snap_rem, "at": snap_at, "usedSince": round(used_since, 2)}
                     if (source == "snapshot" and used_since is not None) else None),
        "stale": stale,
        "configured": bool(cfg.get("plan") is not None or snap_rem is not None),
        "usagePage": USAGE_PAGE,
    }


def hourly_series(idx, day):
    """
    某一天内按小时的分布 —— 供「按日」粒度下钻用。

    单独做一个接口而不是塞进 /api/summary：按小时 × 三个维度 × 每个实体的数据量
    比按天大一个数量级，而只有在下钻到某一天时才需要。
    """
    reqs = [r for r in idx.requests if day_str(r["ts"]) == day]
    name_map = model_display_names(idx.requests)
    dims = {}
    for dim, keyfn, labelfn in (
        ("model", lambda r: r["model"], lambda k: name_map.get(k, k)),
        ("project", lambda r: r["cwd"], lambda k: os.path.basename(k.rstrip("\\/")) or k),
        ("session", lambda r: r["session"], lambda k: idx.title_of(k) or k[:8]),
    ):
        per = {}
        for r in reqs:
            k = keyfn(r)
            buckets = per.setdefault(k, [blank() for _ in range(24)])
            acc(buckets[int(hour_str(r["ts"]))], r)
        rows = []
        for k, buckets in per.items():
            row = {"key": k, "name": labelfn(k)}
            for field in ("credit", "total", "req"):
                vals = [b[field] for b in buckets]
                row["values_" + field] = vals
                row[field] = sum(vals)
            rows.append(row)
        dims[dim] = rows
    return {"day": day, "labels": [f"{h:02d}" for h in range(24)],
            "requests": len(reqs), "dims": dims}


def build_summary(idx, q):
    requests, turns = apply_filters(idx.requests, idx.build_turns(), q, idx.titles)
    totals = blank()
    for r in requests:
        acc(totals, r)
    totals["turns"] = len(turns)
    totals["user_turns"] = sum(1 for t in turns if t.get("kind") == "user")
    totals["sessions"] = len({r["session"] for r in requests})
    totals["think"] = totals["reasoning"]

    days = sorted({day_str(r["ts"]) for r in requests})
    by_day = []
    for d in days:
        rs = [r for r in requests if day_str(r["ts"]) == d]
        b = blank()
        for r in rs:
            acc(b, r)
        b["day"] = d
        b["turns"] = len({t["id"] for t in turns if day_str(t["start"]) == d})
        b["sessions"] = len({r["session"] for r in rs})
        by_day.append(b)

    def ranked(keyfn, meta=None):
        g = group_by(requests, keyfn)
        rows = []
        for k, b in g.items():
            row = {"key": k, **b}
            if meta:
                row.update(meta(k))
            rows.append(row)
        rows.sort(key=lambda x: -x["total"])
        return rows

    model_names = model_display_names(requests)
    scenes = scene_labels(requests)
    sess_title = {s: idx.title_of(s) for s in {r["session"] for r in requests}}
    sess_cwd = {r["session"]: r["cwd"] for r in requests}

    # ---- 24 小时分布：既要合计，也要按天拆开（前端可以切日期看某一天的小时分布）
    hours_total = {f"{h:02d}": blank() for h in range(24)}
    per_day_hours = {}
    for r in requests:
        h = hour_str(r["ts"])
        acc(hours_total[h], r)
        d = day_str(r["ts"])
        per_day_hours.setdefault(d, {f"{x:02d}": blank() for x in range(24)})
        acc(per_day_hours[d][h], r)
    hours_by_day = [
        {"day": d, "hours": [{"hour": h, **b} for h, b in sorted(hs.items())]}
        for d, hs in sorted(per_day_hours.items())
    ]

    # ---- 分布曲线：按天 × 实体，一次把 模型/项目/会话 三个维度都算好。
    # 前端切换维度或口径时不需要重新请求（每个实体都带 credit / total / req 三条序列）。
    DIMS = [
        ("model", lambda r: r["model"], lambda k: model_names.get(k, k)),
        ("project", lambda r: r["cwd"], lambda k: os.path.basename(k.rstrip("\\/")) or k),
        ("session", lambda r: r["session"], lambda k: sess_title.get(k) or k[:8]),
    ]
    series = {}
    for dim, keyfn, labelfn in DIMS:
        per = {}
        for r in requests:
            k = keyfn(r)
            d = day_str(r["ts"])
            per.setdefault(k, {}).setdefault(d, blank())
            acc(per[k][d], r)
        rows = []
        for k, bd in per.items():
            row = {"key": k, "name": labelfn(k)}
            for field in ("credit", "total", "req"):
                vals = [bd.get(d, blank())[field] for d in days]
                row["values_" + field] = vals
                row[field] = sum(vals)
            rows.append(row)
        series[dim] = rows

    sess_span = {}
    for r in requests:
        e = sess_span.setdefault(r["session"], {"start": r["ts"], "end": r["ts"], "turns": 0})
        e["start"] = min(e["start"], r["ts"])
        e["end"] = max(e["end"], r["ts"])
    for t in turns:
        if t["session"] in sess_span and t["req_count"]:
            sess_span[t["session"]]["turns"] += 1

    # 档位 → 实际承载模型 的对照（自动档位到底跑在哪个模型上，这是最实用的一张表）
    scene_models = []
    for sid, b in sorted(group_by(requests, lambda r: r["scene_id"]).items(),
                         key=lambda kv: -kv[1]["total"]):
        sub = group_by([r for r in requests if r["scene_id"] == sid], lambda r: r["model"])
        scene_models.append({
            "scene": sid, "label": scenes.get(sid, sid), **b,
            "auto": sid in SCENE_LABELS,
            "models": sorted(
                [{"id": mid, "name": model_names.get(mid, mid), **mb} for mid, mb in sub.items()],
                key=lambda x: -x["total"]),
        })

    cache_base = totals["cached"] + totals["miss"]
    return {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "range": [days[0], days[-1]] if days else ["", ""],
        "totals": totals,
        "cache_hit_rate": (totals["cached"] / cache_base) if cache_base else 0.0,
        "by_day": by_day,
        "by_hour": _by_hour(requests),
        "hours_by_day": hours_by_day,
        "series": {"days": days, "dims": series},
        "by_model": ranked(lambda r: r["model"], lambda k: {"name": model_names.get(k, k)}),
        "by_scene": ranked(lambda r: r["scene_id"], lambda k: {"name": scenes.get(k, k)}),
        "scene_models": scene_models,
        "by_project": ranked(lambda r: r["cwd"], lambda k: {"name": os.path.basename(k.rstrip("\\/")) or k}),
        "by_session": ranked(
            lambda r: r["session"],
            lambda k: {"title": sess_title.get(k, ""), "cwd": sess_cwd.get(k, ""),
                       **sess_span.get(k, {"start": 0, "end": 0, "turns": 0})}),
        "titleOverrides": idx.overrides,
        "month": month_payload(idx),
    }


def _by_hour(requests):
    buckets = {f"{h:02d}": blank() for h in range(24)}
    for r in requests:
        acc(buckets[hour_str(r["ts"])], r)
    return [{"hour": h, **b} for h, b in buckets.items()]


def turn_rows(idx, q):
    """轮次明细（分页 + 排序）。"""
    _, turns = apply_filters(idx.requests, idx.build_turns(), q, idx.titles)
    # 模型展示名统一用 model_display_names 算：requestModelName 有时填的是**档位名**
    # （"均衡"/"快速"），不能直接当模型名用。
    name_map = model_display_names(idx.requests)
    rows = []
    for t in turns:
        b = blank()
        for r in t["reqs"]:
            acc(b, r)
        rows.append({
            "id": t["id"],
            "session": t["session"],
            "sessionTitle": idx.title_of(t["session"]),
            "project": t["project"],
            "cwd": t["cwd"],
            "account": t["account"],
            "device": t["device"],
            "hostname": t.get("hostname", ""),
            "client": t.get("client", ""),
            "index": t["index"],
            "kind": t.get("kind", "user"),
            "prompt": (t["prompt"] or "")[:400],
            "images": t.get("images", 0),
            "start": t["start"],
            "end": t["end"],
            "duration": t["duration"],
            "tools": t["tools"],
            "orphan": bool(t.get("orphan")),
            # models 与 modelNames 必须**同序**（都按该模型的积分降序），
            # 否则前端拿 models[0] 配 modelNames[0] 会张冠李戴（踩过：图例显示成了档位名）
            "models": sorted(t["models"], key=lambda m: -t["models"][m]),
            "modelNames": [name_map.get(m, m)
                           for m in sorted(t["models"], key=lambda m: -t["models"][m])],
            **b,
        })
    field = q.get("sort", ["start"])[0] or "start"
    order = (q.get("order", ["desc"])[0] or "desc")
    if rows:
        if field not in rows[0]:
            field = "start"
        rows.sort(
            key=lambda x: (x.get(field) if isinstance(x.get(field), (int, float))
                           else str(x.get(field) or "")),
            reverse=(order == "desc"),
        )

    total = len(rows)
    limit = min(int(q.get("limit", ["50"])[0] or 50), 1000)
    offset = int(q.get("offset", ["0"])[0] or 0)
    return {"total": total, "limit": limit, "offset": offset, "rows": rows[offset:offset + limit]}


def turn_detail(idx, turn_id, q):
    _, turns = apply_filters(idx.requests, idx.build_turns(), q, idx.titles)
    for t in turns:
        if t["id"] == turn_id:
            out = []
            for pos, r in enumerate(sorted(t["reqs"], key=lambda x: x["ts"]), 1):
                out.append({
                    "seq": pos, "ts": r["ts"], "model": r["model"], "modelName": r["model_name"],
                    "scene": r["scene_id"], "agent": r["agent"],
                    "inp": r["inp"], "out": r["out"], "total": r["total"],
                    "cached": r["cached"], "reasoning": r["reasoning"], "credit": r["credit"],
                    "msg": r["msg"], "trace": r["trace"], "creq": r["creq"],
                })
            return {"turn": {"id": t["id"], "prompt": t["prompt"], "kind": t.get("kind", "user"),
                             "start": t["start"], "end": t["end"], "tools": t["tools"],
                             "index": t["index"], "session": t["session"], "project": t["project"]},
                    "requests": out}
    return None


# ------------------------------------------------------------------ HTTP
INDEX = None


class Handler(BaseHTTPRequestHandler):
    server_version = "WBUsage/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---- helpers
    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, full, ctype, cache="no-store"):
        try:
            data = open(full, "rb").read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def _index_html(self):
        """
        首页：把图标 URL 上的 __ICON_V__ 换成图标文件的修改时间。

        为什么需要：favicon 由浏览器**单独缓存**，而且很顽固——改了图标、页面刷新了，
        标签页还是显示旧图标（本项目被这个坑过两次：一次是根本没图标，一次是留着旧图标）。
        给 URL 带上 mtime 就等于换了地址，缓存自然失效，以后改图标不用手动改版本号。
        """
        full = os.path.join(STATIC_DIR, "index.html")
        try:
            text = open(full, encoding="utf-8").read()
        except OSError:
            return self.send_error(404)
        stamps = []
        for f in (os.path.join(STATIC_DIR, "favicon.svg"), os.path.join(ROOT_DIR, "icon.ico")):
            try:
                stamps.append(int(os.path.getmtime(f)))
            except OSError:
                pass
        body = text.replace("__ICON_V__", str(max(stamps) if stamps else 1)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path):
        rel = path.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(os.path.normpath(STATIC_DIR)) or not os.path.isfile(full):
            self.send_error(404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
            ".json": "application/json; charset=utf-8",
        }.get(os.path.splitext(full)[1], "application/octet-stream")
        data = open(full, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---- routes
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_query(parsed.query)
        try:
            if path == "/" or path == "/index.html":
                return self._index_html()
            if path.startswith("/static/"):
                return self._static(path.replace("/static/", "/", 1))
            # 浏览器默认会直接找 /favicon.ico —— 不路由它，标签页就只能显示默认的灰色地球
            if path == "/favicon.ico":
                return self._send_file(os.path.join(ROOT_DIR, "icon.ico"),
                                       "image/x-icon", cache="public, max-age=86400")
            if path == "/api/meta":
                return self._json(meta_payload(INDEX))
            if path == "/api/whoami":
                # 极小响应，供 --probe / --stop 取进程号（--stop 不依赖 pid 文件）
                return self._json({"ok": True, "pid": os.getpid(),
                                   "port": self.server.server_address[1]})
            if path == "/api/config":
                return self._json({"ok": True, "config": load_config(),
                                   "month": month_payload(INDEX)})
            if path == "/api/hourly":
                day = (q.get("day", [""])[0] or "").strip()
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                    return self._json({"ok": False, "error": "day 需要 YYYY-MM-DD"}, 400)
                return self._json(hourly_series(INDEX, day))
            if path == "/api/summary":
                return self._json(build_summary(INDEX, q))
            if path == "/api/turns":
                return self._json(turn_rows(INDEX, q))
            if path == "/api/turn":
                tid = q.get("id", [""])[0]
                d = turn_detail(INDEX, tid, q)
                return self._json(d or {"error": "not found"}, 200 if d else 404)
            if path == "/api/export.csv":
                return self._csv(INDEX, q)
            if path == "/api/stream":
                return self._sse()
            self.send_error(404)
        except BrokenPipeError:
            pass
        except Exception as exc:
            try:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            except Exception:
                pass

    def _csv(self, idx, q):
        rows = turn_rows(idx, {**q, "limit": ["100000"], "offset": ["0"]})["rows"]
        cols = ["start", "session", "sessionTitle", "project", "index", "prompt",
                "account", "device", "hostname", "client", "req", "tools",
                "inp", "out", "cached", "reasoning", "total", "credit", "duration", "models"]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["时间", "会话ID", "会话标题", "项目", "轮次", "类型", "提问", "账号", "设备", "主机名",
                    "客户端版本", "API请求数", "工具调用", "输入tokens", "输出tokens", "缓存命中",
                    "思考tokens", "合计tokens", "积分", "耗时ms", "涉及模型"])
        for r in rows:
            w.writerow([
                dt.datetime.fromtimestamp(r["start"] / 1000).strftime("%Y-%m-%d %H:%M:%S"),
                r["session"], r["sessionTitle"], r["cwd"], r["index"], r["kind"],
                (r["prompt"] or "").replace("\n", " ")[:200],
                r["account"], r["device"], r["hostname"], r["client"],
                r["req"], r["tools"], r["inp"], r["out"], r["cached"],
                r["reasoning"], r["total"], round(r["credit"], 4), r["duration"],
                ",".join(r["models"]),
            ])
        body = ("\ufeff" + buf.getvalue()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="workbuddy-usage-turns.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self):
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        last = None
        try:
            while True:
                snap = {
                    "version": INDEX.data_version(),
                    "scanAt": INDEX.last_scan,
                    "requests": len(INDEX.requests),
                    "prompts": len(INDEX.prompts),
                    "sessions": len({r["session"] for r in INDEX.requests}),
                    "lastTs": max((r["ts"] for r in INDEX.requests), default=0),
                }
                if snap["version"] != last:
                    last = snap["version"]
                    payload = json.dumps({"type": "update", **snap}, ensure_ascii=False)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    # ---- POST：目前只有保存额度设置
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            if not isinstance(body, dict):
                raise ValueError("请求体必须是 JSON 对象")
        except (ValueError, UnicodeDecodeError) as exc:
            return self._json({"ok": False, "error": f"请求体解析失败：{exc}"}, 400)
        try:
            if path == "/api/config":
                save_config(body)
                return self._json({"ok": True, "config": load_config(),
                                   "month": month_payload(INDEX)})
            return self._json({"ok": False, "error": "未知接口"}, 404)
        except OSError as exc:
            log(f"[!] 保存设置失败：{exc}")
            return self._json({"ok": False, "error": f"写入失败：{exc}"}, 500)


def meta_payload(idx):
    reqs = idx.requests
    ident = idx.identity
    devices = list(ident["devices"])
    for dev in devices:
        dev["sessions"] = len({r["session"] for r in reqs})
        dev["requests"] = len(reqs)
    accounts = []
    for a in ident["accounts"]:
        accounts.append({**a, "requests": len(reqs),
                         "sessions": len({r["session"] for r in reqs})})
    projects = {}
    for r in reqs:
        projects[r["cwd"]] = projects.get(r["cwd"], 0) + 1
    models, scenes = {}, {}
    mnames = model_display_names(reqs)
    slabels = scene_labels(reqs)
    for r in reqs:
        models.setdefault(r["model"], {"name": mnames.get(r["model"], r["model"]), "req": 0})
        models[r["model"]]["req"] += 1
    for r in reqs:
        scenes[r["scene_id"]] = scenes.get(r["scene_id"], 0) + 1
    sessions = {}
    for r in reqs:
        s = sessions.setdefault(r["session"], {"title": idx.title_of(r["session"]),
                                               "cwd": r["cwd"], "req": 0})
        s["req"] += 1
    days = sorted({day_str(r["ts"]) for r in reqs})
    return {
        "identity": {"accounts": accounts, "devices": devices},
        "attribution_note": (
            "会话记录里不含逐请求的账号/设备字段，账号与设备按安装实例归属："
            "账号取自 settings.json 的 claw.users 与 connectors 的 accountIdentityKey，"
            "设备取自 device-id 与主机名。"
        ),
        "projects": [{"cwd": k, "name": os.path.basename(k.rstrip("\\/")) or k, "req": v}
                     for k, v in sorted(projects.items(), key=lambda kv: -kv[1])],
        "models": [{"id": k, **v} for k, v in sorted(models.items(), key=lambda kv: -kv[1]["req"])],
        "scenes": [{"id": k, "label": slabels.get(k, k), "req": v}
                   for k, v in sorted(scenes.items(), key=lambda kv: -kv[1])],
        "sessions": [{"id": k, **v} for k, v in sorted(sessions.items(), key=lambda kv: -kv[1]["req"])],
        "range": [days[0], days[-1]] if days else ["", ""],
        "counts": {"requests": len(reqs), "prompts": len(idx.prompts), "tools": len(idx.tools),
                   "sessions": len(sessions), "files": len(idx._files), "days": len(days)},
        "dataVersion": idx.data_version(),
        "lastScan": idx.last_scan,
        "scanError": idx.error,
        "projectsDir": idx.projects_dir,
    }


class V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def selftest(host, port, family):
    """真连一次，别只是打印一个想当然的地址。
    本机（实测）IPv4 回环 127.0.0.1 会被拦截，只有 ::1 通，所以必须验。"""
    s = socket.socket(family)
    s.settimeout(2.0)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


ADDR_IN_USE = {getattr(errno, "EADDRINUSE", 98), 10048, getattr(errno, "WSAEADDRINUSE", 0)}


def _in_use(exc):
    return (getattr(exc, "errno", None) in ADDR_IN_USE
            or getattr(exc, "winerror", None) == 10048)


def bind_group(port, lan=False):
    """在指定端口绑定 127.0.0.1 / ::1（可选 0.0.0.0）。返回 (servers, urls, busy)。"""
    cand = [("127.0.0.1", socket.AF_INET, "IPv4 回环"),
            ("::1", socket.AF_INET6, "IPv6 回环")]
    if lan:
        cand.append(("0.0.0.0", socket.AF_INET, "局域网"))
    servers, urls, busy = [], [], False
    for host, family, label in cand:
        cls = V6Server if family == socket.AF_INET6 else ThreadingHTTPServer
        try:
            srv = cls((host, port), Handler)
        except OSError as exc:
            if _in_use(exc):
                busy = True
                break                       # 端口被占：整组换端口重来
            urls.append({"url": "", "label": label, "host": host, "ok": False,
                         "why": f"绑定失败：{exc.strerror or exc}"})
            continue
        srv.daemon_threads = True
        servers.append(srv)
        probe_host = lan_ip() if host == "0.0.0.0" else host
        shown = probe_host or host
        url = f"http://[{shown}]:{port}/" if family == socket.AF_INET6 else f"http://{shown}:{port}/"
        ok = selftest(probe_host, port, family) if probe_host else False
        urls.append({"url": url, "label": label, "host": host, "ok": ok,
                     "why": "" if ok else "本机该协议族回环被系统/安全软件拦截"})
    return servers, urls, busy


def start_servers(args, port=None):
    """端口被占用就自动往上顺延，避免"双击了却起不来"。"""
    base = port or args.port
    for offset in range(20):
        p = base + offset
        servers, urls, busy = bind_group(p, args.lan)
        if busy:
            continue
        note = f"端口 {base} 被占用，已自动改用 {p}" if offset else ""
        return servers, urls, p, note
    return [], [], base, f"端口 {base}~{base + 19} 全被占用，无法启动"


# ------------------------------------------------------------------ main
def main(argv=None):
    ap = argparse.ArgumentParser(description="WorkBuddy 用量实时看板")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--host", default="", help="兼容参数：指定单一绑定地址（默认自动多绑）")
    ap.add_argument("--lan", action="store_true", help="额外在局域网开放（同网段设备可访问）")
    ap.add_argument("--interval", type=float, default=2.0, help="增量扫描间隔（秒）")
    ap.add_argument("--projects", default=PROJECTS_DIR, help="会话记录目录")
    ap.add_argument("--open", action="store_true", help="启动后用可用地址打开浏览器")
    ap.add_argument("--no-browser", action="store_true", help="不打开浏览器")
    ap.add_argument("--stop", action="store_true", help="停止正在运行的看板后退出")
    ap.add_argument("--probe", action="store_true", help="探测看板是否在运行（退出码 0=在跑），不启动")
    ap.add_argument("--diagnose", action="store_true", help="启动失败时弹窗给出排查指引")
    args = ap.parse_args(argv)

    if args.probe:
        # 静默快速返回，供启动器轮询；不能写日志，否则一次启动刷 40 行
        return 0 if find_running(args.port) else 1

    if args.diagnose:
        return diagnose(args.port)

    if args.stop:
        ok, msg = stop_running(args.port)
        log(("" if ok else "[!] ") + msg)
        if not ok and WINDOWLESS:
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, msg, "WorkBuddy 用量看板", 0x40)
            except Exception:
                pass
        return 0 if ok else 1

    # 已经有一个在跑：直接把浏览器指过去，不要起第二个（也避免端口顺延后的"两个看板"）
    running = find_running(args.port)
    if running:
        url = f"http://127.0.0.1:{running}/"
        log(f"[+] 看板已在运行（端口 {running}），直接打开 {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    global INDEX
    t0 = time.time()
    log("[*] 正在建立索引（首次全量）…")
    try:
        INDEX = UsageIndex(projects_dir=args.projects, scan_interval=args.interval)
    except Exception as exc:
        return fatal(f"索引会话记录失败：{type(exc).__name__}: {exc}")
    log(f"[+] 索引完成：{len(INDEX.requests):,} 条 LLM 请求 / {len(INDEX.prompts):,} 条提问 / "
        f"{len(INDEX._files)} 个文件，用时 {time.time() - t0:.1f}s")
    accts = INDEX.identity["accounts"]
    devs = INDEX.identity["devices"]
    log(f"[+] 账号：{accts[0]['label'] if accts else '(未识别)'}    "
        f"设备：{devs[0]['label'] if devs else '(未识别)'}")
    INDEX.start()

    if args.host:
        family = socket.AF_INET6 if ":" in args.host else socket.AF_INET
        cls = V6Server if family == socket.AF_INET6 else ThreadingHTTPServer
        try:
            srv = cls((args.host, args.port), Handler)
        except OSError as exc:
            return fatal(f"无法绑定 {args.host}:{args.port} — {exc.strerror or exc}")
        srv.daemon_threads = True
        servers = [srv]
        shown = f"[{args.host}]" if family == socket.AF_INET6 else args.host
        urls = [{"url": f"http://{shown}:{args.port}/", "label": "手动指定",
                 "host": args.host, "ok": selftest(args.host, args.port, family), "why": ""}]
        port = args.port
        note = ""
    else:
        servers, urls, port, note = start_servers(args)
        if not servers:
            return fatal(note or "无法在任何端口上启动监听。")

    write_pid(port)
    lines = ["[+] 可用访问地址（每个都实测连过）："]
    best = ""
    for u in urls:
        mark = "✓" if u["ok"] else "✗"
        extra = f"   {u['why']}" if not u["ok"] else ""
        lines.append(f"      {mark} {u['label']:<12} {(u['url'] or '(未监听)'):<28}{extra}")
        if u["ok"] and not best:
            best = u["url"]
    if note:
        lines.insert(0, "[!] " + note)
    if not best:
        lines.append("      [!] 没有任何可用地址，请检查安全软件是否拦截了本机回环监听。")
    log("\n".join(lines))
    log(f"[+] 看板运行中（增量扫描 {args.interval}s，进程 {os.getpid()}）")
    log(f"[+] 日志：{LOG_FILE}")

    if args.open and best and not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(best)).start()

    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log("[+] 已停止")
    finally:
        for s in servers:
            s.server_close()
        # 只删自己写的 pid 文件：多实例并存时，先退出的那个把别人的删掉，
        # 会让幸存者失去控制手段（见 running_pid 的注释）
        try:
            if os.path.exists(PID_FILE):
                if open(PID_FILE, encoding="utf-8").read().strip() == str(os.getpid()):
                    os.remove(PID_FILE)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
