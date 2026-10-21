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
import subprocess
import sys
import threading
import time
import queue as _queue

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog

APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
CFG_PATH = os.path.join(APP_DIR, "守护壳配置.json")
CREATE_NEW_CONSOLE = 0x00000010

DEFAULTS = {"name": "", "remark": "", "command": "", "keep_alive": True,
            "daily_time": "", "autostart": True, "min_runtime": 60, "cooldown": 10}


class Target(object):
    """守护目标：持久字段 + 运行时状态"""

    def __init__(self, d):
        d = {**DEFAULTS, **(d or {})}
        self.name = d["name"]
        self.remark = str(d.get("remark", ""))
        self.command = d["command"]
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
                    keep_alive=self.keep_alive, daily_time=self.daily_time,
                    autostart=self.autostart, min_runtime=self.min_runtime,
                    cooldown=self.cooldown)

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

    def launch(self, t, reason):
        try:
            t.proc = subprocess.Popen(t.command, shell=True,
                                      creationflags=CREATE_NEW_CONSOLE)
            t.last_launch = time.time()
            t.pid = t.proc.pid
            t.exit_recorded = False
            t.manual_stop = False
            self.log("[%s] 启动(%s) PID=%d" % (t.tag(), reason, t.pid))
        except Exception as e:
            t.last_exit = time.time()
            t.exit_recorded = True
            t.last_exit_code = "启动失败"
            self.log("[%s] 启动失败: %s" % (t.tag(), str(e)[:100]))

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
        # 状态文本
        if t.manual_stop and not t.running():
            return "已手动停止"
        if t.running():
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
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            return [Target(x) for x in d.get("targets", [])]
        except Exception as e:
            print("[配置读取失败] %s" % e)
    return []


def save_cfg(targets):
    d = {"targets": [t.to_dict() for t in targets]}
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


class App(object):
    def __init__(self, targets):
        self.targets = targets
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
                          ("立即重启", self._restart), ("停止", self._stop)):
            ttk.Button(btns, text=text, command=cmd).pack(side="left", padx=2)
        self.logbox = scrolledtext.ScrolledText(top, height=10, state="disabled",
                                                font=("Consolas", 9))
        self.logbox.pack(fill="both", expand=True)
        self._refresh_tree()

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
                ("每日定时(HH:MM,空=不定时)", "daily_time"),
                ("最短运行秒(定时重启前)", "min_runtime"), ("重启冷却秒", "cooldown")]
        ents = {}
        for i, (lab, key) in enumerate(rows):
            ttk.Label(win, text=lab).grid(row=i, column=0, sticky="e", padx=4, pady=2)
            e = ttk.Entry(win, width=72)
            e.insert(0, str(d[key]))
            e.grid(row=i, column=1, padx=4, pady=2)
            ents[key] = e

        def pick():
            p = filedialog.askopenfilename(
                parent=win, title="选择策略脚本",
                filetypes=[("Python脚本", "*.py"), ("批处理", "*.bat"),
                           ("全部文件", "*.*")])
            if p:
                ents["command"].delete(0, "end")
                ents["command"].insert(0, 'py -3.14 "%s"' % os.path.abspath(p))

        ttk.Button(win, text="浏览...", command=pick)\
            .grid(row=2, column=2, padx=(0, 6))
        cb1_val = tk.BooleanVar(value=bool(d["keep_alive"]))
        cb2_val = tk.BooleanVar(value=bool(d["autostart"]))
        ttk.Checkbutton(win, text="守护模式（退出即拉起）", variable=cb1_val)\
            .grid(row=len(rows), column=1, sticky="w")
        ttk.Checkbutton(win, text="守护壳启动时自动拉起", variable=cb2_val)\
            .grid(row=len(rows) + 1, column=1, sticky="w")

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
                      cooldown=int(ents["cooldown"].get() or 10))
            if t:
                if t.running():
                    self.wd.kill(t, "配置修改")
                self.targets[self.targets.index(t)] = Target(nd)
            else:
                self.targets.append(Target(nd))
            save_cfg(self.targets)
            self._refresh_tree()
            win.destroy()
        ttk.Button(win, text="保存", command=ok).grid(row=len(rows) + 2, column=1, pady=6)

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
        save_cfg(self.targets)
        self._refresh_tree()

    def _start(self):
        t = self._sel_t()
        if t and not t.running():
            t.manual_stop = False
            self.wd.launch(t, "手动启动")

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
            save_cfg(self.targets)
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
    targets = load_cfg()
    if not targets:
        targets = [Target(dict(name="示例-编辑成实际命令行",
                               command='cmd /c echo 这是示例目标，右键编辑成实际命令行',
                               keep_alive=False, daily_time="", autostart=False))]
    App(targets).run()


if __name__ == "__main__":
    main()
