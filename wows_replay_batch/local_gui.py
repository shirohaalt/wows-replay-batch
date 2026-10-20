"""Local replay conversion tab. Only trajectory controls are exposed."""
import os
from pathlib import Path
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from dataclasses import replace

from .core import AppError
from .local_replays import LocalRunner, OUTPUT_DIRECTORY
from .ui_theme import ACCENT, BG, FIELD, MUTED, PANEL, TEXT, stripe_rows

STATUS = {"downloaded": "待转换", "pending": "待转换", "converting": "转换中", "completed": "已完成",
          "skipped": "已完成 · 跳过", "failed": "失败 · 可重试", "cancelled": "已停止"}


class LocalPage(tk.Frame):
    def __init__(self, owner, saved):
        super().__init__(owner.notebook, bg=BG)
        self.owner = owner
        self.folder = tk.StringVar(value=saved.get("local_folder", ""))
        mode = saved.get("local_trail_mode", "all")
        self.vars = {"trail_mode": tk.StringVar(value=mode if mode in ("all", "selected", "none") else "all"),
                     "trails": tk.BooleanVar(value=True)}
        self.trail_selections = saved.get("local_trail_selections", {})
        if not isinstance(self.trail_selections, dict):
            self.trail_selections = {}
        self.jobs, self.checked = {}, set()
        self.loaded_folder = None
        self.filter = tk.StringVar()
        self.count = tk.StringVar(value="请先选择并读取回放文件夹")
        self.detail = tk.StringVar(value=f"扫描所选文件夹及其子文件夹；输出保存到 {OUTPUT_DIRECTORY} 子目录。")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)
        form = tk.Frame(self, bg=PANEL, padx=18, pady=14)
        form.grid(row=0, column=0, sticky="ew", pady=(12, 0))
        form.columnconfigure(1, weight=1)
        owner.label(form, "本地回放文件夹", color=MUTED).grid(row=0, column=0, padx=(0, 16), sticky="w")
        entry = ttk.Entry(form, textvariable=self.folder)
        entry.grid(row=0, column=1, sticky="ew")
        owner.inputs.append(entry)
        controls = tk.Frame(form, bg=PANEL)
        controls.grid(row=0, column=2, padx=(10, 0))
        owner.button(controls, "选择文件夹", self.choose_folder)
        owner.button(controls, "读取回放", self.scan_folder)
        owner.label(form, "直接读取 .wowsreplay 文件；无需比赛网站或 manifest.json。", color=MUTED, size=9).grid(row=1, column=1, columnspan=2, sticky="w", pady=(9, 0))
        trails = tk.Frame(self, bg=PANEL, padx=18, pady=10)
        trails.grid(row=1, column=0, sticky="ew")
        owner.label(trails, "舰船轨迹", color=MUTED).pack(side="left", padx=(0, 15))
        for value, title in (("all", "全部舰船"), ("selected", "仅指定舰船"), ("none", "关闭轨迹")):
            radio = tk.Radiobutton(trails, text=title, variable=self.vars["trail_mode"], value=value, bg=PANEL,
                                   fg=TEXT, selectcolor=FIELD, activebackground=PANEL, activeforeground=TEXT)
            radio.pack(side="left", padx=(0, 12))
            owner.inputs.append(radio)
        self.trails_button = owner.button(trails, "选择舰船…", self.select_trails)
        actions = tk.Frame(self, bg=BG)
        actions.grid(row=2, column=0, sticky="ew", pady=(12, 9))
        self.convert_button = owner.button(actions, "转换已勾选回放", self.start_conversion, True)
        self.select_button = owner.button(actions, "全选当前列表", lambda: self.check_visible(True))
        self.clear_button = owner.button(actions, "清空当前列表", lambda: self.check_visible(False))
        owner.label(actions, "双击任务播放已生成的视频", color=MUTED, size=9).pack(side="right")
        filters = tk.Frame(self, bg=BG)
        filters.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        owner.label(filters, "筛选文件 / 地图 / 玩家 / 舰船", color=MUTED, size=9).pack(side="left", padx=(0, 9))
        query = ttk.Entry(filters, textvariable=self.filter, width=26)
        query.pack(side="left")
        owner.inputs.append(query)
        owner.label(filters, textvariable=self.count, color=ACCENT).pack(side="right")
        panel = tk.Frame(self, bg=BG)
        panel.grid(row=4, column=0, sticky="ew", pady=(0, 9))
        self.progress = ttk.Progressbar(panel, maximum=100)
        self.progress.pack(fill="x")
        owner.label(panel, textvariable=self.detail, color=MUTED, size=9, anchor="w").pack(fill="x", pady=(6, 0))
        self.table = owner.make_table(self, ("check", "file", "map", "player", "ship", "version", "time", "trails", "status"),
                                      ("转换", "回放文件", "地图", "玩家", "舰船", "游戏版本", "对局时间", "指定轨迹", "状态"),
                                      (50, 230, 135, 110, 130, 105, 150, 80, 110))
        self.table.master.grid(row=5, column=0, sticky="nsew")
        self.table.bind("<Button-1>", self.toggle_check)
        self.table.bind("<space>", self.toggle_check)
        self.table.bind("<Double-1>", self.open_video)
        self.table.bind("<Return>", self.open_video)
        self.folder.trace_add("write", lambda *_: self.invalidate())
        self.filter.trace_add("write", lambda *_: self.refresh())
        self.busy(False)

    def settings(self):
        return {"local_folder": self.folder.get(), "local_trail_mode": self.vars["trail_mode"].get(),
                "local_trail_selections": self.trail_selections}

    def options(self):
        mode = self.vars["trail_mode"].get()
        return replace(self.owner.get_options(), show_bans=False, trails=mode != "none", trail_mode=mode,
                       trail_selections=self.trail_selections)

    def choose_folder(self):
        folder = filedialog.askdirectory(title="选择本地回放文件夹", initialdir=self.folder.get() or str(self.owner.root_dir))
        if folder:
            self.folder.set(folder)

    def invalidate(self):
        self.loaded_folder = None
        self.jobs, self.checked = {}, set()
        self.refresh()
        self.detail.set("文件夹已变化，请点击「读取回放」。")
        self.busy(bool(self.owner.worker and self.owner.worker.is_alive()))

    def busy(self, active):
        ready = bool(self.loaded_folder and self.jobs)
        for button in (self.select_button, self.clear_button, self.trails_button):
            button.configure(state="normal" if ready and not active else "disabled")
        self.convert_button.configure(state="normal" if ready and self.selected_ids() and not active else "disabled")
        if not active and str(self.progress.cget("mode")) == "indeterminate":
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)

    def scan_folder(self):
        if self.owner.worker and self.owner.worker.is_alive():
            return
        try:
            runner = LocalRunner(self.folder.get(), self.options(), root=self.owner.root_dir, cancel=threading.Event(),
                                 emit=lambda kind, data: self.owner.events.put(("local_event", (kind, data))))
            self.invalidate()
            self.owner.runner, self.owner.cancel = runner, runner.cancel
            self.owner.last_output, self.owner.active_phase = runner.output, "local_scan"
            self.progress.configure(mode="indeterminate")
            self.progress.start(15)
            self.detail.set("正在读取本地回放信息…")
            self.owner.state_text.set("正在读取本地回放")
            self.owner.detail_text.set("读取文件头、玩家、地图、舰船和游戏版本后，再选择需要转换的回放。")
            self.owner.launch(runner.scan)
        except (AppError, OSError) as error:
            messagebox.showerror("无法读取文件夹", str(error), parent=self)

    def conversion_runner(self, **kwargs):
        if not self.loaded_folder or Path(self.folder.get()).expanduser().resolve() != self.loaded_folder:
            raise AppError("请先读取本地回放文件夹。")
        return LocalRunner(self.loaded_folder, self.options(), root=self.owner.root_dir, **kwargs)

    def visible_jobs(self):
        query = self.filter.get().strip().casefold()
        return [(key, job) for key, job in self.jobs.items() if not query or query in " ".join(
            str(job.get(name, "")) for name in ("replay", "map", "team", "ship", "battle_time", "game_version")).casefold()]

    def selected_ids(self):
        return [job["report_id"] for key, job in self.visible_jobs() if key in self.checked]

    def refresh(self):
        self.table.delete(*self.table.get_children())
        rows = self.visible_jobs()
        for key, job in rows:
            state = job.get("status", "downloaded")
            self.table.insert("", "end", iid=key, values=("☑" if key in self.checked else "☐", job["replay"], job["map"],
                job["team"], job["ship"], job["game_version"], job["battle_time"],
                f"{len(self.trail_selections.get(key, []))} 条", STATUS.get(state, state)),
                tags=("completed" if state in ("completed", "skipped") else state,))
        stripe_rows(self.table)
        self.count.set(f"显示 {len(rows)} / {len(self.jobs)} 份 · 已勾选 {len(self.selected_ids())} 份")
        self.busy(bool(self.owner.worker and self.owner.worker.is_alive()))

    def check_visible(self, value):
        if self.owner.worker and self.owner.worker.is_alive():
            return
        for key, _ in self.visible_jobs():
            self.checked.add(key) if value else self.checked.discard(key)
        self.refresh()

    def toggle_check(self, event):
        if self.owner.worker and self.owner.worker.is_alive():
            return "break"
        if event.keysym == "space":
            selection = self.table.selection()
            key = selection[0] if selection else ""
        elif self.table.identify_column(event.x) == "#1":
            key = self.table.identify_row(event.y)
        else:
            return
        if key in self.jobs:
            self.checked.symmetric_difference_update({key})
            self.refresh()
            self.table.selection_set(key)
        return "break"

    def select_trails(self):
        from .bp_gui import TrailWindow
        try:
            self.conversion_runner()
            TrailWindow(self)
        except (AppError, OSError) as error:
            messagebox.showerror("无法读取舰船", str(error), parent=self)

    def on_trails_saved(self):
        self.owner.save_settings()
        self.refresh()
        self.detail.set("已保存舰船选择，转换时仅显示指定舰船轨迹。")

    def start_conversion(self):
        if self.owner.worker and self.owner.worker.is_alive():
            return
        try:
            runner = self.conversion_runner(cancel=threading.Event(),
                emit=lambda kind, data: self.owner.events.put(("local_event", (kind, data))))
            selected = self.selected_ids()
            if not selected:
                raise AppError("请先勾选需要转换的本地回放。")
            self.owner.runner, self.owner.cancel = runner, runner.cancel
            self.owner.last_output, self.owner.active_phase = runner.output, "local_convert"
            self.progress.configure(mode="determinate", maximum=max(len(selected), 1), value=0)
            self.owner.state_text.set(f"正在转换 {len(selected)} 份本地回放")
            self.owner.detail_text.set("使用本地回放转换视频；首次使用对应游戏版本时会准备公共游戏素材。")
            self.owner.launch(lambda: runner.convert(selected))
        except (AppError, OSError) as error:
            messagebox.showerror("无法开始转换", str(error), parent=self)

    def receive_event(self, kind, data):
        if kind == "log":
            self.owner.add_log(data)
        elif kind == "scan_progress":
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=max(data["total"], 1), value=data["done"])
            self.detail.set(f"已读取 {data['done']} / {data['total']} 个文件 · 有效 {data['valid']} · 跳过 {data['invalid']}")
        elif kind == "local_scanned":
            if Path(self.folder.get()).expanduser().resolve() != Path(data["folder"]):
                return
            self.loaded_folder = Path(data["folder"])
            self.jobs = {str(job["report_id"]): job for job in data["manifest"]["jobs"]}
            self.checked = set(self.jobs)
            self.refresh()
            self.owner.state_text.set(f"本地回放读取完成 · {len(self.jobs)} 份")
            self.owner.detail_text.set("选择回放和舰船轨迹后开始转换。视频和日志保存到回放文件夹的独立输出子目录。")
        elif kind == "job":
            self.jobs[str(data["report_id"])] = data
            self.refresh()
            self.detail.set(f"{STATUS.get(data['status'], data['status'])} · {data['replay']}")
        elif kind == "progress":
            self.progress.configure(value=data["done"])
            self.owner.state_text.set(f"本地转换 {data['done']} / {data['total']} · 新转换 {data['converted']} · 跳过 {data['skipped']} · 失败 {data['failed']}")
        elif kind == "compose_progress":
            self.detail.set(f"正在合成舰船轨迹 · 已处理 {data['frames']} 帧")
        elif kind == "complete":
            self.detail.set(f"转换完成 · 新转换 {data['converted']} · 跳过 {data['skipped']} · 失败 {data['failed']}")
            self.owner.state_text.set("本地回放转换完成")
            self.owner.detail_text.set(f"视频位于所选回放文件夹内的 {OUTPUT_DIRECTORY}/videos。")

    def open_output(self):
        if not self.loaded_folder:
            messagebox.showinfo("本地转换结果", "请先读取本地回放文件夹。", parent=self)
            return
        os.startfile(str(self.loaded_folder / OUTPUT_DIRECTORY))

    def open_video(self, event=None):
        if event is not None and getattr(event, "num", None) == 1 and self.table.identify_column(event.x) == "#1":
            return "break"
        selected = self.table.selection()
        if not selected or not self.loaded_folder:
            return
        job = self.jobs[selected[0]]
        path = self.loaded_folder / OUTPUT_DIRECTORY / job["video"]
        try:
            if path.is_file():
                os.startfile(str(path))
            elif job.get("error"):
                messagebox.showerror("本地转换详情", job["error"], parent=self)
        except OSError as error:
            messagebox.showerror("无法播放视频", str(error), parent=self)
