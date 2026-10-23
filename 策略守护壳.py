# -*- coding: utf-8 -*-
"""策略守护壳：定时启动/重启 + 意外终止自动拉起（通用，任意命令行策略）

    py -3.14 策略守护壳/策略守护壳.py            # GUI
    py -3.14 策略守护壳/策略守护壳.py --selftest  # 无界面自检（调度/守护逻辑）

每个守护目标=一条命令行，两种机制可叠加：
    守护模式 keep_alive     进程退出（崩溃/被关窗/正常结束）后，冷却N秒自动拉起。
                            停止按钮=人工停止，不再拉起，直到点"立即启动"。
    每日定时 daily_time     每天到点时：进程在跑→杀掉换新进程（定时重启）；
                            没跑→直接拉起（定时启动）。跨天只触发一次。
状态机对重启安全（B执行器的消费账+持仓去重保证不会重复下单），但仍建议把
B的每日定时设在 09:10 这种开盘前空档，避开 09:25-09:30 和 14:55-15:01 关键窗。
配置持久化在 exe 同目录的 守护壳配置.json。
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import queue as _queue

import ctypes
import tkinter as tk
from ctypes import wintypes
from tkinter import ttk, messagebox, scrolledtext, filedialog

try:
    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
except Exception:
    user32 = None

try:
    import winreg
except ImportError:
    winreg = None

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "策略守护壳"

APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
CFG_PATH = os.path.join(APP_DIR, "守护壳配置.json")
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000

DEFAULTS = {"name": "", "remark": "", "command": "", "keep_alive": True,
            "daily_time": "", "autostart": True, "min_runtime": 60, "cooldown": 10,
            "show_window": True, "keep_window": False, "workdir": "",
            "no_duplicate": False}


class Target(object):
    """守护目标：持久字段 + 运行时状态"""

    def __init__(self, d):
        d = {**DEFAULTS, **(d or {})}
        self.name = d["name"]
        self.remark = str(d.get("remark", ""))
        self.command = d["command"]
        self.show_window = bool(d.get("show_window", True))
        self.keep_window = bool(d.get("keep_window", False))
        self.workdir = str(d.get("workdir", ""))
        self.no_duplicate = bool(d.get("no_duplicate", False))
        self.keep_alive = bool(d["keep_alive"])
        self.daily_time = str(d["daily_time"] or "")
        self.autostart = bool(d["autostart"])
        self.min_runtime = int(d["min_runtime"])
        self.cooldown = int(d["cooldown"])
        # 运行时
        self.proc = None
        self.manual_stop = False
        self.last_launch = 0.0
        self.last_exit = 0.0
        self.last_exit_code = ""
        self.exit_recorded = False
        self.last_fire_date = ""
        self.pid = 0
        self.status = "…"

    def to_dict(self):
        return dict(name=self.name, remark=self.remark, command=self.command,
                    show_window=self.show_window, keep_window=self.keep_window,
                    workdir=self.workdir, no_duplicate=self.no_duplicate,
                    keep_alive=self.keep_alive, daily_time=self.daily_time,
                    autostart=self.autostart, min_runtime=self.min_runtime,
                    cooldown=self.cooldown)

    def dup_key(self):
        """重复运行检测的特征串：命令行里最后一个带引号路径(通常=脚本)，否则整条命令"""
        qs = re.findall(r'"([^"]+)"', self.command)
        if qs:
            return qs[-1].replace("/", "\\")
        return self.command.strip()

    def console_window(self):
        """找本进程的控制台窗口: 返回(hwnd, 可见)，无则(0, False)"""
        if user32 is None or self.pid <= 0:
            return (0, False)
        res = {"hwnd": 0, "visible": False}

        def cb(h, l):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            if pid.value == self.pid:
                buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(h, buf, 256)
                if buf.value == "ConsoleWindowClass":
                    res["hwnd"] = h
                    res["visible"] = bool(user32.IsWindowVisible(h))
                    return False
            return True
        user32.EnumWindows(WNDENUMPROC(cb), 0)
        return (res["hwnd"], res["visible"])

    def front_window(self):
        """把策略控制台窗口强制还原并置前（排查'程序活着但看不到窗口'）"""
        hwnd, _vis = self.console_window()
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True

    def tag(self):
        """日志用标识：名称+备注，多策略同守时区分谁出问题"""
        return "%s（%s）" % (self.name, self.remark) if self.remark else self.name

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def runtime(self):
        return time.time() - self.last_launch if self.running() else 0.0


def fmt_dur(sec):
    sec = int(sec)
    return "%02d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


class Watchdog(threading.Thread):
    """守护线程：每秒巡检所有目标"""

    def __init__(self, targets, logq):
        super().__init__(daemon=True)
        self.targets = targets
        self.logq = logq
        self.stop_flag = False
        self._day = time.strftime("%Y%m%d")

    def log(self, msg):
        line = time.strftime("%H:%M:%S ") + msg
        if self.logq is not None:
            self.logq.put(("log", line))
        else:
            print(line)
        try:   # 日志每日更新：logs\守护_YYYYMMDD.log
            d = os.path.join(APP_DIR, "logs")
            os.makedirs(d, exist_ok=True)
            today = time.strftime("%Y%m%d")
            if self._day != today and self._day != "":
                self._cleanup_logs()
            if self._day != today:
                self._wfile(d, "――― 日期切换 %s ―――" % today)
            self._day = today
            self._wfile(d, line)
        except Exception:
            pass

    def _wfile(self, d, line):
        fn = os.path.join(d, "守护_%s.log" % time.strftime("%Y%m%d"))
        with open(fn, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _cleanup_logs(self, keep=30):
        import datetime
        d = os.path.join(APP_DIR, "logs")
        cutoff = int((datetime.datetime.now()
                      - datetime.timedelta(days=keep)).strftime("%Y%m%d"))
        try:
            for fn in os.listdir(d):
                if fn.startswith("守护_") and fn.endswith(".log"):
                    d8 = fn[3:11]
                    if d8.isdigit() and int(d8) < cutoff:
                        os.remove(os.path.join(d, fn))
        except Exception:
            pass

    def _cmdline_pids(self, key):
        """PowerShell查询命令行含key的进程PID(排除查询自身)。失败=空列表(放行)。"""
        esc = key.replace("'", "''")
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | Where-Object {$_.ProcessId -ne $PID "
                 "-and $_.CommandLine -like '*" + esc + "*'} | "
                 "Select-Object -ExpandProperty ProcessId"],
                capture_output=True, timeout=25, text=True)
            return [x.strip() for x in (r.stdout or "").split()
                    if x.strip().isdigit()]
        except Exception:
            return []

    def launch(self, t, reason):
        if t.no_duplicate:
            # 单实例保护：启动前扫全进程命令行，发现同脚本进程就拒绝（含手动启动的）
            if time.time() < getattr(t, "_dup_block", 0):
                return False
            key = t.dup_key()
            pids = self._cmdline_pids(key) if key else []
            if pids:
                t._dup_block = time.time() + 30
                self.log("[%s] 不允许重复运行: 已有进程 PID=%s 命令行含该脚本——跳过启动"
                         % (t.tag(), ",".join(pids[:3])))
                return False
        try:
            # 显示策略窗口=开：独立控制台窗口（策略自己的运行窗口，含全部日志）
            # =关：隐藏窗口运行（后台策略用）
            flags = CREATE_NEW_CONSOLE if t.show_window else CREATE_NO_WINDOW
            cmd = t.command
            if t.keep_window and t.show_window:
                cmd = "cmd /k " + cmd   # 策略退出后窗口保持(报错可见)；守护靠每日定时
            si = None
            if t.show_window:
                # GUI父进程spawn的经典坑：不显式指定显示状态，控制台窗口会以隐藏态创建
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 1   # SW_SHOWNORMAL
            t.proc = subprocess.Popen(cmd, shell=True, creationflags=flags,
                                      startupinfo=si, cwd=t.workdir or None)
            t.last_launch = time.time()
            t.pid = t.proc.pid
            t.exit_recorded = False
            t.manual_stop = False
            self.log("[%s] 启动(%s) PID=%d%s" % (t.tag(), reason, t.pid,
                                                 " 工作目录=%s" % t.workdir if t.workdir else ""))
            return True
        except Exception as e:
            t.last_exit = time.time()
            t.exit_recorded = True
            t.last_exit_code = "启动失败"
            self.log("[%s] 启动失败: %s" % (t.tag(), str(e)[:100]))
            return False

    def kill(self, t, reason):
        if t.proc is None:
            return
        try:
            subprocess.run(["taskkill", "/PID", str(t.proc.pid), "/T", "/F"],
                           capture_output=True, timeout=15)
            self.log("[%s] 已终止(%s) PID=%d" % (t.tag(), reason, t.proc.pid))
        except Exception as e:
            self.log("[%s] 终止异常: %s" % (t.tag(), str(e)[:80]))
        end = time.time() + 15
        while t.proc.poll() is None and time.time() < end:
            time.sleep(0.2)
        if t.proc.poll() is None:
            try:
                t.proc.kill()
            except Exception:
                pass
        t.last_exit = time.time()
        t.last_exit_code = str(t.proc.poll())
        t.exit_recorded = True
        t.pid = 0

    def check(self, t):
        """单目标巡检：返回状态文本"""
        running = t.running()
        today = time.strftime("%Y%m%d")
        hhmm = time.strftime("%H:%M")
        # 1) 每日定时：到点且今天未触发 → 杀旧换新 / 直接拉起
        if t.daily_time and hhmm >= t.daily_time and t.last_fire_date != today:
            if not running or t.runtime() >= t.min_runtime:
                t.last_fire_date = today
                if running:
                    self.kill(t, "每日定时重启")
                self.launch(t, "每日定时启动/重启")
                running = t.running()
        # 2) 退出检测：运行→退出 的沿上记录一次（冷却计时基准）
        if t.proc is not None and t.proc.poll() is not None and not t.exit_recorded:
            t.exit_recorded = True
            t.last_exit = time.time()
            t.last_exit_code = str(t.proc.poll())
            if not t.manual_stop:
                self.log("[%s] 已退出(退出码%s) 运行%s"
                         % (t.tag(), t.last_exit_code, "刚启动" if t.runtime() < 1 else ""))
        # 3) 意外终止守护：退出已记录、冷却已过 → 拉起
        if (not t.running() and t.proc is not None and t.keep_alive
                and not t.manual_stop
                and time.time() - t.last_exit >= t.cooldown):
            self.launch(t, "意外终止自动拉起(退出码%s)" % (t.last_exit_code or "?"))
        # 4) 守护壳启动自动拉起：从未启动过
        if (t.proc is None and t.autostart and not t.manual_stop):
            self.launch(t, "守护壳启动自动拉起")
        # 窗口自诊断: 运行中且距上次检测超30秒 → 报告控制台窗口状态
        if t.running() and user32 is not None:
            if time.time() - getattr(t, "_last_winchk", 0) > 30:
                t._last_winchk = time.time()
                found, vis = t.console_window()
                if not found:
                    self.log("[%s] 窗口检测: 未见控制台窗口(隐藏运行或宿主异常)" % t.tag())
                elif not vis:
                    self.log("[%s] 窗口检测: 控制台窗口存在但不可见" % t.tag())
                else:
                    self.log("[%s] 窗口检测: 控制台窗口可见" % t.tag())

        # 状态文本
        if t.manual_stop and not t.running():
            return "已手动停止"
        if t.running():
            if os.environ.get("SHELL_SELFTEST"):
                found, vis = t.console_window()
                self.log("[窗口检测] PID=%d 存在=%s 可见=%s" % (t.pid, found, vis))
            return "运行中 PID=%d (%s)" % (t.pid, fmt_dur(t.runtime()))
        if t.last_exit_code and t.exit_recorded:
            return "已退出(%s) 冷却%s" % (t.last_exit_code,
                                          fmt_dur(max(0, t.cooldown - (time.time() - t.last_exit))))
        if t.daily_time:
            return "未启动（每日%s定时）" % t.daily_time
        return "未启动"

    def run(self):
        self.log("守护壳启动，守护目标%d个" % len(self.targets))
        while not self.stop_flag:
            try:
                for t in list(self.targets):
                    st = self.check(t)
                    if self.logq is not None:
                        self.logq.put(("status", (t.name, st)))
            except Exception as e:
                self.log("[守护线程异常] %s" % str(e)[:120])
            time.sleep(1)


def load_cfg():
    """返回 (targets, 单实例保护开关)。缺省=开（防误启双实例）"""
    targets = []
    single = True
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            targets = [Target(x) for x in d.get("targets", [])]
            single = bool(d.get("single_instance", True))
        except Exception as e:
            print("[配置读取失败] %s" % e)
    return targets, single


def save_cfg(targets, single_instance):
    d = {"targets": [t.to_dict() for t in targets],
         "single_instance": single_instance}
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def autostart_enabled():
    """当前用户开机自启是否已注册（HKCU Run——保证在用户桌面会话启动）"""
    if winreg is None or not getattr(sys, "frozen", False):
        return False
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY)
        winreg.QueryValueEx(k, RUN_NAME)
        winreg.CloseKey(k)
        return True
    except OSError:
        return False


def set_autostart(on):
    exe = os.path.abspath(sys.argv[0])
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    if on:
        winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, '"%s"' % exe)
    else:
        try:
            winreg.DeleteValue(k, RUN_NAME)
        except FileNotFoundError:
            pass
    winreg.CloseKey(k)


class App(object):
    def __init__(self, targets, single_instance=True, mutex=None):
        self.targets = targets
        self.single_instance = single_instance
        self.mutex = mutex
        self.logq = _queue.Queue()
        self.root = tk.Tk()
        self.root.title("策略守护壳")
        self.root.geometry("880x540")
        self._build()
        self.wd = Watchdog(self.targets, self.logq)
        self.wd.start()
        self.root.after(500, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self):
        top = ttk.Frame(self.root, padding=4)
        top.pack(fill="both", expand=True)
        cols = ("name", "remark", "status")
        self.tree = ttk.Treeview(top, columns=cols, show="headings", height=8)
        self.tree.heading("name", text="策略")
        self.tree.heading("remark", text="备注")
        self.tree.heading("status", text="状态")
        self.tree.column("name", width=180)
        self.tree.column("remark", width=220)
        self.tree.column("status", width=400)
        self.tree.pack(fill="both", expand=True)
        btns = ttk.Frame(top)
        btns.pack(fill="x", pady=3)
        for text, cmd in (("添加", self._add), ("编辑", self._edit),
                          ("删除", self._del), ("立即启动", self._start),
                          ("立即重启", self._restart), ("停止", self._stop),
                          ("前置窗口", self._front), ("测试运行", self._testrun)):
            ttk.Button(btns, text=text, command=cmd).pack(side="left", padx=2)
        # 开机自启（HKCU Run=当前用户会话启动，避免任务计划错误配置导致窗口不可见）
        self.autostart_btn = ttk.Button(btns, text="", command=self._toggle_autostart)
        self.autostart_btn.pack(side="right", padx=2)
        # 单实例保护开关（Global命名互斥体，跨会话防双开；改开关保存后下次启动生效）
        self.single_btn = ttk.Button(btns, text="", command=self._toggle_single)
        self.single_btn.pack(side="right", padx=2)
        self._autostart_btn_refresh()
        self._single_btn_refresh()
        self.logbox = scrolledtext.ScrolledText(top, height=10, state="disabled",
                                                font=("Consolas", 9))
        self.logbox.pack(fill="both", expand=True)
        self._refresh_tree()

    def _autostart_btn_refresh(self):
        if not getattr(sys, "frozen", False):
            self.autostart_btn.configure(text="开机自启(仅exe可用)", state="disabled")
            return
        on = autostart_enabled()
        self.autostart_btn.configure(
            text="开机自启:开 ✓" if on else "开机自启:关",
            state="normal")

    def _toggle_autostart(self):
        try:
            set_autostart(not autostart_enabled())
        except Exception as e:
            messagebox.showerror("开机自启", "设置失败: %s" % e)
        self._autostart_btn_refresh()

    def _single_btn_refresh(self):
        self.single_btn.configure(
            text="单实例保护:开 ✓" if self.single_instance else "单实例保护:关")

    def _toggle_single(self):
        import ctypes
        k32 = ctypes.windll.kernel32
        self.single_instance = not self.single_instance
        if self.single_instance:
            self.mutex = k32.CreateMutexW(None, False, "Global\策略守护壳单实例")
            if k32.GetLastError() == 183:
                messagebox.showwarning(
                    "单实例保护", "检测到已有守护壳实例持有单实例互斥体——请先关闭它")
        elif getattr(self, "mutex", None):
            k32.CloseHandle(self.mutex)
            self.mutex = None
        save_cfg(self.targets, self.single_instance)
        self._single_btn_refresh()
        self.wd.log("[守护壳] 单实例保护:%s（开启后已持有互斥体，下次启动即拦截重复）"
                    % ("开" if self.single_instance else "关"))

    def _sel_t(self):
        idx = self.tree.selection()
        if not idx:
            messagebox.showinfo("提示", "先在列表中选择策略")
            return None
        name = self.tree.item(idx[0], "values")[0]
        return next((x for x in self.targets if x.name == name), None)

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for t in self.targets:
            self.tree.insert("", "end", iid=id(t),
                             values=(t.name, t.remark,
                                     getattr(t, "status", "…") or "…"))

    def _drain(self):
        try:
            while True:
                kind, val = self.logq.get_nowait()
                if kind == "log":
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", val + "\n")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                else:
                    name, status = val
                    t = next((x for x in self.targets if x.name == name), None)
                    if t:
                        t.status = status
                    for iid in self.tree.get_children():
                        if self.tree.item(iid, "values")[0] == name:
                            self.tree.set(iid, "status", status)
        except _queue.Empty:
            pass
        self.root.after(500, self._drain)

    def _edit_dialog(self, t=None):
        win = tk.Toplevel(self.root)
        win.title("编辑策略" if t else "添加策略")
        win.grab_set()
        d = t.to_dict() if t else dict(DEFAULTS, name="", command="")
        rows = [("名称", "name"), ("备注", "remark"), ("命令行", "command"),
                ("工作目录(相对路径基准,可空)", "workdir"),
                ("每日定时(HH:MM,空=不定时)", "daily_time"),
                ("最短运行秒(定时重启前)", "min_runtime"), ("重启冷却秒", "cooldown")]
        ents = {}
        for i, (lab, key) in enumerate(rows):
            ttk.Label(win, text=lab).grid(row=i, column=0, sticky="e", padx=4, pady=2)
            e = ttk.Entry(win, width=72)
            e.insert(0, str(d[key]))
            e.grid(row=i, column=1, padx=4, pady=2)
            ents[key] = e
        cmd_i = [i for i, (l, k) in enumerate(rows) if k == "command"][0]
        wd_i = [i for i, (l, k) in enumerate(rows) if k == "workdir"][0]

        def pick():
            p = filedialog.askopenfilename(
                parent=win, title="选择策略脚本",
                filetypes=[("Python脚本", "*.py"), ("批处理", "*.bat"),
                           ("全部文件", "*.*")])
            if p:
                ents["command"].delete(0, "end")
                ents["command"].insert(0, 'py -3.14 "%s"' % os.path.abspath(p))

        def pick_dir():
            p = filedialog.askdirectory(parent=win, title="选择策略工作目录")
            if p:
                ents["workdir"].delete(0, "end")
                ents["workdir"].insert(0, os.path.normpath(p))

        ttk.Button(win, text="浏览...", command=pick)\
            .grid(row=cmd_i, column=2, padx=(0, 6))
        ttk.Button(win, text="目录...", command=pick_dir)\
            .grid(row=wd_i, column=2, padx=(0, 6))
        cb1_val = tk.BooleanVar(value=bool(d["keep_alive"]))
        cb2_val = tk.BooleanVar(value=bool(d["autostart"]))
        cb3_val = tk.BooleanVar(value=bool(d.get("show_window", True)))
        ttk.Checkbutton(win, text="守护模式（退出即拉起）", variable=cb1_val)\
            .grid(row=len(rows), column=1, sticky="w")
        ttk.Checkbutton(win, text="守护壳启动时自动拉起", variable=cb2_val)\
            .grid(row=len(rows) + 1, column=1, sticky="w")
        ttk.Checkbutton(win, text="显示策略窗口（独立控制台）", variable=cb3_val)\
            .grid(row=len(rows) + 2, column=1, sticky="w")
        cb4_val = tk.BooleanVar(value=bool(d.get("keep_window", False)))
        ttk.Checkbutton(win, text="窗口保持（策略退出后不关窗，便于看报错）", variable=cb4_val)\
            .grid(row=len(rows) + 3, column=1, sticky="w")
        cb5_val = tk.BooleanVar(value=bool(d.get("no_duplicate", False)))
        ttk.Checkbutton(win, text="不允许重复运行（启动前扫描同脚本进程）", variable=cb5_val)\
            .grid(row=len(rows) + 4, column=1, sticky="w")

        def ok():
            name = ents["name"].get().strip()
            cmd = ents["command"].get().strip()
            if not name or not cmd:
                messagebox.showwarning("提示", "名称与命令行必填", parent=win)
                return
            nd = dict(DEFAULTS)
            nd.update(name=name, command=cmd, keep_alive=cb1_val.get(),
                      autostart=cb2_val.get(), daily_time=ents["daily_time"].get().strip(),
                      min_runtime=int(ents["min_runtime"].get() or 60),
                      cooldown=int(ents["cooldown"].get() or 10),
                      show_window=cb3_val.get(), keep_window=cb4_val.get(),
                      workdir=ents["workdir"].get().strip(),
                      no_duplicate=cb5_val.get())
            if t:
                if t.running():
                    self.wd.kill(t, "配置修改")
                self.targets[self.targets.index(t)] = Target(nd)
            else:
                self.targets.append(Target(nd))
            save_cfg(self.targets, self.single_instance)
            self._refresh_tree()
            win.destroy()
        ttk.Button(win, text="保存", command=ok).grid(row=len(rows) + 5, column=1, pady=6)

    def _add(self):
        self._edit_dialog(None)

    def _edit(self):
        t = self._sel_t()
        if t:
            self._edit_dialog(t)

    def _del(self):
        t = self._sel_t()
        if not t:
            return
        if not messagebox.askyesno("删除", "删除守护目标 %r？运行中的进程将保持运行。" % t.name):
            return
        if t.running():
            self.wd.kill(t, "目标删除")
        self.targets.remove(t)
        save_cfg(self.targets, self.single_instance)
        self._refresh_tree()

    def _start(self):
        t = self._sel_t()
        if t and not t.running():
            t.manual_stop = False
            self.wd.launch(t, "手动启动")

    def _front(self):
        t = self._sel_t()
        if not t or not t.running():
            messagebox.showinfo("前置窗口", "目标未在运行")
            return
        if t.front_window():
            self.wd.log("[%s] 窗口已强制还原并置前" % t.tag())
        else:
            messagebox.showinfo("前置窗口", "未找到该策略的控制台窗口\n(可能以无窗口方式运行)")

    def _testrun(self):
        """诊断用：cmd /k 方式启动选中命令——窗口强制保持，报错停在窗口里。
        不占用受守护实例，窗口需手动关闭。"""
        t = self._sel_t()
        if not t:
            return
        try:
            subprocess.Popen("cmd /k " + t.command, shell=True,
                             creationflags=CREATE_NEW_CONSOLE,
                             cwd=t.workdir or None)
            self.wd.log("[%s] 测试运行：新窗口已打开(cmd /k保持)，看完手动关窗" % t.tag())
        except Exception as e:
            self.wd.log("[%s] 测试运行失败: %s" % (t.tag(), str(e)[:80]))

    def _restart(self):
        t = self._sel_t()
        if t:
            if t.running():
                self.wd.kill(t, "手动重启")
            t.manual_stop = False
            self.wd.launch(t, "手动重启")

    def _stop(self):
        t = self._sel_t()
        if t:
            t.manual_stop = True
            if t.running():
                self.wd.kill(t, "手动停止")
            self.wd.log("[%s] 已手动停止（守护挂起，点立即启动恢复）" % t.name)

    def _on_close(self):
        if messagebox.askyesno("退出守护壳",
                               "退出后运行中的策略将保持运行，但不再受守护与定时。\n确定退出？"):
            save_cfg(self.targets, self.single_instance)
            self.wd.stop_flag = True
            self.root.destroy()

    def run(self):
        self.root.mainloop()


def selftest():
    """无界面自检：自动拉起→意外退出→冷却后自动重启→手动终止"""
    events = []
    t = Target(dict(name="自检", remark="自检备注", command='py -3.14 -c "import time; time.sleep(2)"',
                    keep_alive=True, daily_time="", autostart=True,
                    min_runtime=0, cooldown=3))
    wd = Watchdog([t], None)
    wd.log = events.append
    deadline = time.time() + 14
    while time.time() < deadline:
        wd.check(t)
        time.sleep(0.5)
    wd.kill(t, "自检结束")
    relaunches = sum(1 for e in events if "自动拉起" in e)
    print("自检事件:")
    for e in events:
        print("  ", e)
    print("自检结果:", "PASS" if relaunches >= 2 else "FAIL")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    targets, single = load_cfg()
    mutex = None
    if single:
        # 单实例保护：命名互斥体（Global=整机级，跨会话防重）。已存在=另一实例在跑
        import ctypes
        k32 = ctypes.windll.kernel32
        mutex = k32.CreateMutexW(None, False, "Global\\策略守护壳单实例")
        if k32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
            ctypes.windll.user32.MessageBoxW(
                0, "已有一个策略守护壳正在运行（单实例保护已开启）。\n\n"
                   "如确需运行第二个实例，请先在运行中的守护壳里点"
                   "'单实例保护:关'按钮。", "策略守护壳", 0x40)
            return
    if not targets:
        targets = [Target(dict(name="示例-编辑成实际命令行",
                               command='cmd /c echo 这是示例目标，右键编辑成实际命令行',
                               keep_alive=False, daily_time="", autostart=False))]
    App(targets, single, mutex).run()


if __name__ == "__main__":
    main()
