from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .core import AppError, Cancelled, Options, Runner, app_home, load_library, write_json
from .updates import DEFAULT_REPOSITORY
from .regions import REGIONS, region_for_url
from .ui_theme import ACCENT, BG, FIELD, MUTED, PANEL, TEXT, configure_controls, configure_stripes, stripe_rows
STATUS = {"pending": "等待", "downloading": "下载中", "downloaded": "已下载", "converting": "转换中",
          "completed": "已完成", "skipped": "已完成 · 跳过", "failed": "失败 · 可重试", "cancelled": "已停止"}


class Application(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"WoWS 队伍回放助手 v{__version__}")
        self.geometry(f"1160x{min(960, self.winfo_screenheight() - 80)}")
        self.minsize(1040, 760)
        self.configure(bg=BG)
        self.option_add("*Font", ("Microsoft YaHei UI", 10))
        self.root_dir = app_home()
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.worker = self.runner = None
        self.active_phase = "download"
        self.last_output = self.root_dir / "results"
        saved = {}
        try:
            saved = json.loads((self.root_dir / "settings.json").read_text("utf-8"))
        except (OSError, ValueError):
            pass
        self.update_repository = saved.get("update_repository", DEFAULT_REPOSITORY)
        self.update_window = None
        defaults = asdict(Options(team="", output=""))
        defaults.update({k: v for k, v in saved.items() if k in defaults})
        if "trail_mode" not in saved and not saved.get("trails", True):
            defaults["trail_mode"] = "none"
        self.trail_selections = defaults.pop("trail_selections", {})
        if not isinstance(self.trail_selections, dict):
            self.trail_selections = {}
        self.vars = {k: (tk.BooleanVar(value=v) if isinstance(v, bool) else tk.StringVar(value=v)) for k, v in defaults.items()}
        self.region = tk.StringVar(value=region_for_url(self.vars["url"].get()))
        self.vars["url"].set(REGIONS[self.region.get()])
        self.region.trace_add("write", lambda *_: self.vars["url"].set(REGIONS[self.region.get()]))
        self.source = tk.StringVar(value=saved.get("conversion_source", ""))
        self.filter_text = tk.StringVar()
        self.perspective = tk.StringVar(value="全部已下载视角")
        self.state_text = tk.StringVar(value="准备就绪")
        self.detail_text = tk.StringVar(value="先下载 REP，再到转换页选择回放和视频选项。")
        self.download_count = tk.StringVar(value="尚未下载")
        self.download_detail = tk.StringVar(value="单文件进度将在下载时显示")
        self.conversion_count = tk.StringVar(value="尚未读取本地回放")
        self.convert_detail = tk.StringVar(value="选择已下载回放，设置 BP 和轨迹后开始转换。")
        self.download_jobs, self.conversion_jobs, self.checked = {}, {}, set()
        self.team_candidates = []
        self.team_candidate = tk.StringVar(value="输入部分队名，查询后选择队伍")
        self.match_selection_count = tk.StringVar(value="先查询比赛，再勾选需要下载的比赛")
        self.search_matches, self.match_checked = [], set()
        self.query_identity = None
        self.resolved_candidate = None
        self.library_path = None
        self.library_team = ""
        self.inputs = []
        self.saved_local_settings = saved
        self._style()
        style = ttk.Style(self)
        configure_controls(self)
        style.configure("Treeview", bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL)
        style.configure("Horizontal.TProgressbar", bordercolor=BG, lightcolor=BG, darkcolor=BG)
        style.configure("TCombobox", fieldbackground=FIELD, foreground=TEXT, padding=7)
        style.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", TEXT)])
        self._build()
        self.filter_text.trace_add("write", lambda *_: self.refresh_conversion())
        self.perspective.trace_add("write", lambda *_: self.refresh_conversion())
        for key in ("team", "url"):
            self.vars[key].trace_add("write", lambda *_: self.invalidate_search())
        self.update_download_actions()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(120, self.poll)
        self.after(200, self.restore_library)

    def button(self, parent, text, command, accent=False):
        widget = ttk.Button(parent, text=text, command=command, style="Accent.TButton" if accent else "TButton")
        widget.pack(side="left", padx=(0, 8))
        self.inputs.append(widget)
        return widget

    def _build(self):
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="both", expand=True, padx=24, pady=18)
        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x", pady=(0, 12))
        self.label(head, "W O W S   /   R E P L A Y   S T U D I O", size=9, color=ACCENT).pack(anchor="w")
        row = tk.Frame(head, bg=BG)
        row.pack(fill="x", pady=(4, 0))
        self.label(row, "队伍回放助手", size=23, bold=True).pack(side="left")
        self.button(row, "设置", self.settings).pack(side="right")
        ttk.Button(row, text="检查更新", command=self.open_update).pack(side="right", padx=(0, 8))
        self.label(row, "下载 REP  →  选择条件  →  转换视频", color=MUTED).pack(side="right", padx=20)
        self.notebook = ttk.Notebook(outer, style="Studio.TNotebook")
        self.notebook.pack(fill="both", expand=True)
        self.download_page = tk.Frame(self.notebook, bg=BG)
        self.convert_page = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(self.download_page, text="01   下载 REP")
        self.notebook.add(self.convert_page, text="02   转换视频")
        self._download_page()
        self._convert_page()
        from .local_gui import LocalPage
        self.local_page = LocalPage(self, self.saved_local_settings)
        self.notebook.add(self.local_page, text="03   本地回放转换")
        self.notebook.bind("<<NotebookTabChanged>>", self.page_changed)
        self.saved_local_settings = None
        footer = tk.Frame(outer, bg=BG)
        footer.pack(fill="x", pady=(10, 6))
        self.label(footer, textvariable=self.state_text, color=ACCENT, bold=True).pack(side="left")
        self.stop_button = ttk.Button(footer, text="停止当前任务", command=self.stop, state="disabled")
        self.stop_button.pack(side="right")
        ttk.Button(footer, text="打开结果目录", command=self.open_output).pack(side="right", padx=8)
        self.label(outer, textvariable=self.detail_text, color=MUTED, size=9, anchor="w").pack(fill="x", pady=(0, 6))
        self.log_box = tk.Text(outer, height=3, bg=FIELD, fg=MUTED, relief="flat", padx=12, pady=7,
                               wrap="word", font=("Microsoft YaHei UI", 9), state="disabled")
        self.log_box.pack(fill="x")

    def _download_page(self):
        page = self.download_page
        page.columnconfigure(0, weight=1)
        page.rowconfigure(4, weight=1)
        form = tk.Frame(page, bg=PANEL, padx=18, pady=10)
        form.grid(row=0, column=0, sticky="ew", pady=(12, 0))
        form.columnconfigure(1, weight=1)
        self.label(form, "赛事服务器", color=MUTED).grid(row=0, column=0, sticky="w", padx=(0, 16), pady=5)
        self.region_choice = ttk.Combobox(form, textvariable=self.region, values=tuple(REGIONS), state="readonly")
        self.region_choice.grid(row=0, column=1, sticky="ew", pady=4)
        self.inputs.append(self.region_choice)
        self.label(form, "King of the Sea XIX", color=MUTED, size=9).grid(row=0, column=2, padx=(12, 0), sticky="w")
        for r, title, key in ((1, "队伍关键词 / 队标", "team"), (2, "REP 保存位置", "output")):
            self.label(form, title, color=MUTED).grid(row=r, column=0, sticky="w", padx=(0, 16), pady=5)
            entry = ttk.Entry(form, textvariable=self.vars[key])
            entry.grid(row=r, column=1, sticky="ew", pady=4)
            self.inputs.append(entry)
        self.team_choice = ttk.Combobox(form, textvariable=self.team_candidate, width=29, state="disabled")
        self.team_choice.grid(row=1, column=2, padx=(12, 0), sticky="ew")
        self.team_choice.bind("<<ComboboxSelected>>", self.choose_team)
        self.inputs.append(self.team_choice)
        browse = ttk.Button(form, text="选择文件夹", command=self.browse)
        browse.grid(row=2, column=2, padx=(12, 0), sticky="ew")
        self.inputs.append(browse)
        check = ttk.Checkbutton(form, text="同时下载对手视角（转换时可分别选择）", variable=self.vars["both"], style="Studio.TCheckbutton")
        check.grid(row=3, column=1, sticky="w", pady=(3, 0))
        self.inputs.append(check)
        actions = tk.Frame(page, bg=BG)
        actions.grid(row=1, column=0, sticky="ew", pady=10)
        self.button(actions, "查询比赛", lambda: self.start_download(True), True)
        self.start_button = self.button(actions, "全部下载", self.start_download)
        self.selected_download_button = self.button(actions, "下载已勾选比赛", lambda: self.start_download(selected_only=True))
        self.button(actions, "BAN / PICK 查询", self.open_bp)
        self.button(actions, "前往转换页 →", self.go_convert)
        panel = tk.Frame(page, bg=BG)
        panel.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.label(panel, textvariable=self.download_count, color=ACCENT, anchor="w").pack(fill="x")
        self.download_progress = ttk.Progressbar(panel, mode="determinate", maximum=100)
        self.download_progress.pack(fill="x", pady=(6, 6))
        self.label(panel, textvariable=self.download_detail, color=MUTED, size=9, anchor="w").pack(fill="x")
        self.file_progress = ttk.Progressbar(panel, mode="determinate", maximum=100)
        self.file_progress.pack(fill="x", pady=(5, 0))
        selection = tk.Frame(page, bg=BG)
        selection.grid(row=3, column=0, sticky="ew", pady=(0, 7))
        self.select_matches_button = self.button(selection, "全选比赛", lambda: self.check_matches(True))
        self.clear_matches_button = self.button(selection, "清空勾选", lambda: self.check_matches(False))
        self.label(selection, textvariable=self.match_selection_count, color=ACCENT).pack(side="right")
        self.download_table = self.make_table(page, ("check", "match", "stage", "game", "map", "team", "progress", "status"),
                                             ("下载", "比赛 / 回放", "阶段 / 轮次", "局数", "地图", "REP 视角", "文件进度", "状态"),
                                             (50, 210, 170, 50, 110, 80, 110, 125))
        self.download_table.configure(show="tree headings")
        self.download_table.column("#0", width=22, minwidth=22, stretch=False)
        self.download_table.master.grid(row=4, column=0, sticky="nsew")
        self.download_table.bind("<Button-1>", self.toggle_match)
        self.download_table.bind("<space>", self.toggle_match)

    def _convert_page(self):
        page = self.convert_page
        page.columnconfigure(0, weight=1)
        page.rowconfigure(4, weight=1)
        form = tk.Frame(page, bg=PANEL, padx=18, pady=12)
        form.grid(row=0, column=0, sticky="ew", pady=(12, 0))
        form.columnconfigure(1, weight=1)
        self.label(form, "本地回放清单", color=MUTED).grid(row=0, column=0, padx=(0, 15), sticky="w")
        entry = ttk.Entry(form, textvariable=self.source)
        entry.grid(row=0, column=1, sticky="ew")
        entry.bind("<Return>", lambda _: self.load_conversion())
        self.inputs.append(entry)
        controls = tk.Frame(form, bg=PANEL)
        controls.grid(row=0, column=2, padx=(10, 0))
        self.button(controls, "选择清单", self.choose_manifest)
        self.button(controls, "读取", self.load_conversion)
        self.label(form, "可读取以前下载的 manifest.json 或其所在目录；转换只使用本地 REP。", size=9, color=MUTED).grid(row=1, column=1, columnspan=2, sticky="w", pady=(7, 10))
        check = ttk.Checkbutton(form, text="视频显示双方 BP 信息（双方提交的禁船）", variable=self.vars["show_bans"], style="Studio.TCheckbutton")
        check.grid(row=2, column=0, columnspan=3, sticky="w")
        self.inputs.append(check)
        trail = tk.Frame(form, bg=PANEL)
        trail.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self.label(trail, "舰船轨迹", color=MUTED).pack(side="left", padx=(0, 15))
        for value, title in (("all", "全部舰船"), ("selected", "仅指定舰船"), ("none", "关闭轨迹")):
            radio = tk.Radiobutton(trail, text=title, variable=self.vars["trail_mode"], value=value, bg=PANEL, fg=TEXT,
                                   activebackground=PANEL, activeforeground=TEXT, selectcolor=FIELD)
            radio.pack(side="left", padx=(0, 12))
            self.inputs.append(radio)
        self.button(trail, "选择舰船…", self.select_trails)
        actions = tk.Frame(page, bg=BG)
        actions.grid(row=1, column=0, sticky="ew", pady=(10, 7))
        self.button(actions, "转换已勾选回放", self.start_conversion, True)
        self.button(actions, "全选当前列表", lambda: self.check_visible(True))
        self.button(actions, "清空当前列表", lambda: self.check_visible(False))
        self.label(actions, "双击任务播放已生成的视频", color=MUTED, size=9).pack(side="right")
        filters = tk.Frame(page, bg=BG)
        filters.grid(row=2, column=0, sticky="ew", pady=(0, 7))
        combo = ttk.Combobox(filters, textvariable=self.perspective,
                             values=("全部已下载视角", "仅目标队伍视角", "仅对手视角"), state="readonly", width=18)
        combo.pack(side="left", padx=(0, 10))
        self.inputs.append(combo)
        self.label(filters, "筛选比赛 / 地图 / 队伍", color=MUTED, size=9).pack(side="left", padx=(0, 8))
        query = ttk.Entry(filters, textvariable=self.filter_text, width=20)
        query.pack(side="left")
        self.inputs.append(query)
        self.label(filters, textvariable=self.conversion_count, color=ACCENT).pack(side="right")
        panel = tk.Frame(page, bg=BG)
        panel.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        self.convert_progress = ttk.Progressbar(panel, mode="determinate", maximum=100)
        self.convert_progress.pack(fill="x")
        self.label(panel, textvariable=self.convert_detail, color=MUTED, size=9, anchor="w").pack(fill="x", pady=(5, 0))
        self.convert_table = self.make_table(page, ("check", "match", "game", "map", "team", "trails", "status"),
                                            ("转换", "比赛", "对局", "地图", "REP 视角", "指定轨迹", "视频状态"),
                                            (55, 260, 55, 150, 90, 120, 140))
        self.convert_table.master.grid(row=4, column=0, sticky="nsew")
        self.convert_table.bind("<Button-1>", self.toggle_check)
        self.convert_table.bind("<space>", self.toggle_check)
        self.convert_table.bind("<Double-1>", self.open_video)
        self.convert_table.bind("<Return>", self.open_video)

    def make_table(self, page, columns, titles, widths):
        frame = tk.Frame(page, bg=PANEL)
        table = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse", height=4)
        for key, title, width in zip(columns, titles, widths):
            table.heading(key, text=title)
            table.column(key, width=width, minwidth=45, anchor="center" if key in ("check", "game") else "w", stretch=key != "check")
        table.tag_configure("completed", foreground=ACCENT)
        table.tag_configure("failed", foreground="#ffac95")
        configure_stripes(table)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=scrollbar.set)
        table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return table

    def save_settings(self):
        write_json(self.root_dir / "settings.json", {**asdict(self.get_options()), "conversion_source": self.source.get(),
                                                    "update_repository": self.update_repository, **self.local_page.settings()})

    def choose_manifest(self):
        path = filedialog.askopenfilename(title="选择已下载的回放清单", initialdir=str(self.library_path.parent if self.library_path else self.last_output),
                                         filetypes=[("回放清单", "manifest.json"), ("JSON", "*.json")])
        if path:
            self.source.set(path)
            self.load_conversion()

    def restore_library(self):
        if self.source.get() and Path(self.source.get()).exists():
            self.load_conversion(silent=True)
        else:
            if not self.vars["team"].get().strip() or not self.vars["output"].get().strip():
                return
            try:
                path = Runner(self.get_options()).output / "manifest.json"
                if path.exists():
                    self.source.set(str(path))
                    self.load_conversion(silent=True)
            except (AppError, OSError):
                pass

    def load_conversion(self, silent=False, preserve=False):
        try:
            path, manifest = load_library(self.source.get())
            jobs = {str(j["report_id"]): j for j in manifest["jobs"]}
            same = path == self.library_path
            self.library_path, self.library_team = path, manifest.get("team", "")
            self.source.set(str(path))
            self.conversion_jobs = jobs
            available = {key for key, j in jobs.items() if (path.parent / j["replay"]).is_file()}
            self.checked = self.checked & available if preserve and same else available
            self.convert_detail.set(f"已读取 {len(available)} 份本地 REP。勾选回放并设置 BP、轨迹后开始转换。")
            self.last_output = path.parent
            self.refresh_conversion()
            self.save_settings()
            return True
        except (AppError, OSError) as e:
            if not silent:
                messagebox.showerror("无法读取本地回放", str(e), parent=self)
            return False

    def conversion_runner(self, **kwargs):
        if not self.library_path:
            raise AppError("请先读取下载结果中的 manifest.json。")
        return Runner.from_manifest(self.library_path, self.get_options(), **kwargs)

    def go_convert(self):
        self.notebook.select(self.convert_page)
        if self.runner and self.active_phase == "download" and (self.runner.output / "manifest.json").exists():
            self.source.set(str(self.runner.output / "manifest.json"))
            self.load_conversion()

    def page_changed(self, _event=None):
        if not (self.worker and self.worker.is_alive()) and self.notebook.select() == str(self.local_page):
            self.detail_text.set("本地转换：选择文件夹并读取回放，再设置舰船轨迹。无需查询比赛网站。")

    def visible_jobs(self):
        query = self.filter_text.get().strip().casefold()
        for key, job in self.conversion_jobs.items():
            own = job.get("team", "").strip().casefold() == self.library_team.strip().strip("[]").casefold()
            target_tag = job.get("target_team", "")
            own = own or bool(target_tag and job.get("team", "").casefold() == target_tag.casefold())
            if self.perspective.get() == "仅目标队伍视角" and not own or self.perspective.get() == "仅对手视角" and own:
                continue
            if query and query not in " ".join(str(job.get(k, "")) for k in ("match_id", "opponents", "map", "team", "stage", "round")).casefold():
                continue
            yield key, job

    def selected_ids(self):
        return [j["report_id"] for key, j in self.visible_jobs() if key in self.checked]

    def refresh_conversion(self):
        if not hasattr(self, "convert_table"):
            return
        table = self.convert_table
        focus, scroll = table.selection(), table.yview()
        table.delete(*table.get_children())
        visible = list(self.visible_jobs())
        for key, job in visible:
            local = self.library_path and (self.library_path.parent / job["replay"]).is_file()
            count = len(self.trail_selections.get(key, []))
            status = STATUS.get(job.get("status"), "待转换") if local else "REP 缺失，请下载"
            table.insert("", "end", iid=key, values=("☑" if key in self.checked else "☐", " vs ".join(job["opponents"]),
                         job["game_number"], job["map"], job["team"], f"{count} 条" if count else "未指定", status),
                         tags=("completed" if job.get("status") in ("completed", "skipped") else job.get("status", ""),))
        if focus and table.exists(focus[0]):
            table.selection_set(focus)
        if scroll:
            table.yview_moveto(scroll[0])
        stripe_rows(table)
        self.conversion_count.set(f"显示 {len(visible)} 份 · 已勾选 {len(self.selected_ids())} 份")

    def check_visible(self, value):
        for key, job in self.visible_jobs():
            if value and self.library_path and (self.library_path.parent / job["replay"]).is_file():
                self.checked.add(key)
            else:
                self.checked.discard(key)
        self.refresh_conversion()

    def toggle_check(self, event):
        if self.worker and self.worker.is_alive():
            return "break"
        if event.keysym == "space":
            selected = self.convert_table.selection()
            key = selected[0] if selected else ""
        elif self.convert_table.identify_column(event.x) == "#1":
            key = self.convert_table.identify_row(event.y)
        else:
            return
        if key:
            job = self.conversion_jobs[key]
            if not (self.library_path.parent / job["replay"]).is_file():
                return "break"
            self.checked.symmetric_difference_update({key})
            self.refresh_conversion()
            self.convert_table.selection_set(key)
        return "break"

    def select_trails(self):
        from .bp_gui import TrailWindow
        try:
            self.conversion_runner()
            TrailWindow(self)
        except (AppError, OSError) as e:
            messagebox.showerror("无法读取回放", str(e), parent=self)

    def on_trails_saved(self):
        self.save_settings()
        self.refresh_conversion()
        self.convert_detail.set("已保存舰船选择。仅指定舰船模式下，未指定的回放不显示轨迹。")

    def busy(self, active):
        for widget in self.inputs:
            widget.configure(state="disabled" if active else "readonly" if isinstance(widget, ttk.Combobox) else "normal")
        self.stop_button.configure(state="normal" if active else "disabled")
        self.update_download_actions(active)
        self.local_page.busy(active)

    def launch(self, operation):
        self.save_settings()
        self.busy(True)
        def work():
            try:
                operation()
            except Cancelled:
                self.events.put(("stopped", None))
            except Exception as e:
                self.events.put(("error", str(e)))
            finally:
                self.events.put(("idle", None))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def query_key(self):
        return self.vars["url"].get().strip(), self.vars["team"].get().strip()

    def invalidate_search(self):
        self.query_identity = None
        self.resolved_candidate = None
        self.team_candidates, self.search_matches = [], []
        self.match_checked.clear()
        self.download_jobs.clear()
        self.team_choice.configure(values=())
        self.team_candidate.set("关键词已变化，请重新查询")
        self.download_table.delete(*self.download_table.get_children())
        self.download_count.set("请查询比赛")
        self.update_download_actions()

    def update_download_actions(self, active=None):
        if active is None:
            active = bool(self.worker and self.worker.is_alive())
        valid = bool(self.resolved_candidate and self.query_identity == self.query_key() and self.search_matches)
        self.start_button.configure(state="normal" if valid and not active else "disabled")
        self.selected_download_button.configure(state="normal" if valid and self.match_checked and not active else "disabled")
        for button in (self.select_matches_button, self.clear_matches_button):
            button.configure(state="normal" if valid and not active else "disabled")
        self.team_choice.configure(state="readonly" if self.team_candidates and not active else "disabled")
        tag = self.resolved_candidate["tag"] + " · " if self.resolved_candidate else ""
        self.match_selection_count.set(f"{tag}共 {len(self.search_matches)} 场 · 已勾选 {len(self.match_checked)} 场")

    def receive_team_candidates(self, data):
        if tuple(data["query"]) != self.query_key():
            return
        self.query_identity = tuple(data["query"])
        self.team_candidates = data["candidates"]
        self.team_choice.configure(values=[f"{c['tag']} · {c['name']} · {len(c['matches'])} 场" for c in self.team_candidates])
        best = max((c["score"] for c in self.team_candidates), default=0)
        preferred = [i for i, c in enumerate(self.team_candidates) if best >= 2 and c["score"] == best]
        if len(preferred) == 1 or len(self.team_candidates) == 1:
            self.team_choice.current(preferred[0] if preferred else 0)
            self.choose_team()
        elif self.team_candidates:
            self.team_candidate.set(f"匹配 {len(self.team_candidates)} 支队伍，请选择 ↓")
            self.state_text.set("请选择需要下载的队伍")
            self.download_count.set("已找到多个匹配队伍，请在队名右侧选择。")
            self.detail_text.set("选定队伍后会显示比赛，可全部下载或勾选部分比赛下载。")
        else:
            self.team_candidate.set("没有匹配队伍，可缩短关键词")
            self.download_count.set("没有找到匹配的队伍")
            self.state_text.set("查询完成，未找到队伍")
        self.update_download_actions()

    def choose_team(self, _event=None):
        index = self.team_choice.current()
        if index < 0 or index >= len(self.team_candidates):
            return
        self.resolved_candidate = self.team_candidates[index]
        self.search_matches = self.resolved_candidate["matches"]
        self.match_checked = {str(m["id"]) for m in self.search_matches}
        self.download_jobs.clear()
        self.download_table.delete(*self.download_table.get_children())
        for match in self.search_matches:
            self.insert_match(match)
        stripe_rows(self.download_table)
        self.download_count.set(f"查询完成：{self.resolved_candidate['tag']}，共 {len(self.search_matches)} 场比赛")
        self.state_text.set("比赛查询完成")
        self.detail_text.set("可点击「全部下载」，或勾选比赛后点击「下载已勾选比赛」。")
        self.update_download_actions()

    def insert_match(self, match):
        key = f"m{match['id']}"
        self.download_table.insert("", "end", iid=key, open=True,
            values=("☑" if str(match["id"]) in self.match_checked else "☐",
                " vs ".join(o["signup"]["teamTag"] for o in match["matchOpponents"]),
                f"{match.get('stageName', '')} / {match.get('roundName', '')}", match.get("gameCount", ""),
                "—", self.resolved_candidate["tag"] if self.resolved_candidate else "—", "—",
                "已结束" if match.get("matchStatus") == 3 else "待比赛"))

    def check_matches(self, value):
        if self.worker and self.worker.is_alive():
            return
        self.match_checked = {str(m["id"]) for m in self.search_matches} if value else set()
        for match in self.search_matches:
            self.download_table.set(f"m{match['id']}", "check", "☑" if value else "☐")
        self.update_download_actions()

    def toggle_match(self, event):
        if event.keysym == "space":
            selection = self.download_table.selection()
            key = selection[0] if selection else ""
        elif self.download_table.identify_column(event.x) == "#1":
            key = self.download_table.identify_row(event.y)
        else:
            return
        if self.worker and self.worker.is_alive():
            return "break"
        if key.startswith("m") and key[1:] in {str(m["id"]) for m in self.search_matches}:
            self.match_checked.symmetric_difference_update({key[1:]})
            self.download_table.set(key, "check", "☑" if key[1:] in self.match_checked else "☐")
            self.download_table.selection_set(key)
            self.update_download_actions()
        return "break"

    def get_download_options(self):
        options = self.get_options()
        if self.resolved_candidate and self.query_identity == self.query_key():
            options = replace(options, team=self.resolved_candidate["tag"])
        return options

    def start_download(self, search_only=False, selected_only=False):
        if self.worker and self.worker.is_alive():
            return
        try:
            options = self.get_options() if search_only else self.get_download_options()
            if not options.team.strip():
                raise AppError("请填写队伍名称或队标，例如 DEMO。")
            if not search_only and (not self.resolved_candidate or self.query_identity != self.query_key()):
                raise AppError("请先查询比赛并选择目标队伍。")
            matches = [m for m in self.search_matches if not selected_only or str(m["id"]) in self.match_checked]
            if not search_only and not matches:
                raise AppError("请勾选至少一场比赛。")
            if not search_only and not options.output.strip():
                raise AppError("请先选择 REP 保存位置。")
            self.cancel = threading.Event()
            self.runner = Runner(options, cancel=self.cancel, emit=lambda k, d: self.events.put((k, d)))
            if not search_only:
                self.runner.target_signup_id = self.resolved_candidate.get("signup_id")
            self.last_output, self.active_phase = self.runner.output, "download"
            self.download_jobs.clear()
            if search_only:
                self.invalidate_search()
            else:
                # Retain the match rows and their choices while refreshing the per-REP children.
                for parent in self.download_table.get_children():
                    children = self.download_table.get_children(parent)
                    if children:
                        self.download_table.delete(*children)
                stripe_rows(self.download_table)
            self.download_progress.configure(mode="indeterminate")
            self.download_progress.start(15)
            self.file_progress.stop()
            self.file_progress.configure(mode="determinate", value=0)
            self.download_count.set("正在模糊查询队伍和比赛…" if search_only else f"准备下载 {len(matches)} 场比赛的 REP…")
            self.state_text.set("查询比赛中" if search_only else "准备下载 REP")
            self.detail_text.set("支持队标、队名的部分文字，忽略大小写及队标装饰符号。" if search_only else "仅下载选定比赛，完成后可前往转换页设置视频。")
            query = self.query_key()
            def operation():
                if search_only:
                    candidates = self.runner.client.find_teams(options.team)
                    self.events.put(("team_candidates", {"query": query, "candidates": candidates}))
                else:
                    self.runner.download(matches)
            self.launch(operation)
        except (AppError, OSError) as e:
            messagebox.showerror("无法开始下载", str(e), parent=self)

    def start_conversion(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            if not self.library_path or Path(self.source.get()).resolve() != self.library_path:
                if not self.load_conversion():
                    return
            selected = self.selected_ids()
            if not selected:
                raise AppError("请勾选至少一份已下载回放。可通过视角和关键词筛选。")
            self.cancel = threading.Event()
            self.runner = self.conversion_runner(cancel=self.cancel, emit=lambda k, d: self.events.put((k, d)))
            self.last_output, self.active_phase = self.runner.output, "convert"
            self.convert_progress.configure(maximum=len(selected), value=0)
            self.state_text.set(f"正在转换 {len(selected)} 份本地回放")
            self.detail_text.set("正在使用本地 REP；首次转换所需的游戏素材会自动准备。")
            self.launch(lambda: self.runner.convert(selected))
        except (AppError, OSError) as e:
            messagebox.showerror("无法开始转换", str(e), parent=self)

    def stop(self):
        self.cancel.set()
        self.state_text.set("正在停止…")
        self.detail_text.set("已完成的文件会保留，等待当前请求或转换工具退出。")
        self.stop_button.configure(state="disabled")

    def open_output(self):
        try:
            if self.notebook.select() == str(self.local_page):
                self.local_page.open_output()
                return
            path = self.library_path.parent if self.notebook.select() == str(self.convert_page) and self.library_path else self.last_output
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
        except OSError as e:
            messagebox.showerror("打开失败", str(e), parent=self)

    def open_video(self, event=None):
        if event is not None and getattr(event, "num", None) == 1 and self.convert_table.identify_column(event.x) == "#1":
            return "break"
        selected = self.convert_table.selection()
        if not selected or not self.library_path:
            return
        job = self.conversion_jobs[selected[0]]
        path = self.library_path.parent / job["video"]
        if path.is_file():
            try:
                os.startfile(str(path))
            except OSError as e:
                messagebox.showerror("播放失败", str(e), parent=self)
        elif job.get("error"):
            messagebox.showerror("转换详情", job["error"], parent=self)

    def update_download_job(self, job):
        key = str(job["report_id"])
        self.download_jobs[key] = job
        status = job.get("download_status", "pending")
        parent = f"m{job['match_id']}"
        if not self.download_table.exists(parent):
            self.download_table.insert("", "end", iid=parent, open=True,
                values=("", " vs ".join(job["opponents"]), f"{job['stage']} / {job['round']}", "", "", "", "", ""))
        values = ("", f"第 {job['game_number']} 局", "", job["game_number"], job["map"], job["team"],
                  "100%" if status == "downloaded" else "—", {"downloaded": "已下载 REP", "failed": "失败 · 可重试", "cancelled": "已停止"}.get(status, "等待下载"))
        if self.download_table.exists(key):
            self.download_table.item(key, values=values, tags=("completed" if status == "downloaded" else status,))
        else:
            self.download_table.insert(parent, "end", iid=key, values=values, tags=("completed" if status == "downloaded" else status,))
        siblings = [j for j in self.download_jobs.values() if j["match_id"] == job["match_id"]]
        done = sum(j.get("download_status") == "downloaded" for j in siblings)
        failed = sum(j.get("download_status") == "failed" for j in siblings)
        self.download_table.set(parent, "status", f"REP {done}/{len(siblings)}" + (f" · 失败 {failed}" if failed else ""))
        stripe_rows(self.download_table)

    def receive_download_progress(self, data):
        total, done = data["total"], data["done"]
        percent = data["value"] / max(total, 1) * 100
        self.download_progress.stop()
        self.download_progress.configure(mode="determinate", maximum=100, value=percent)
        self.download_count.set(f"整批进度  {percent:.1f}%    ·    已结束 {done} / {total} 份    ·    已接收 {capacity(data['bytes'])}")
        key, size, received = str(data["report_id"]), data["size"], data["received"]
        text = f"{min(received / size * 100, 99):.0f}%" if size else capacity(received)
        if self.download_table.exists(key):
            self.download_table.set(key, "progress", "100%" if data["state"] == "downloaded" else text)
            self.download_table.set(key, "status", {"downloaded": "已下载 REP", "failed": "失败 · 可重试", "cancelled": "已停止"}.get(data["state"], "正在接收"))
        active = data["active"]
        if active:
            first, state = next(iter(active.items()))
            job = self.download_jobs.get(first, {})
            self.download_detail.set(f"{len(active)} 个文件接收中  ·  {job.get('team', '')} / 比赛 {job.get('match_id', '')} / G{job.get('game_number', '')}  ·  "
                                     + (f"{capacity(state['received'])} / {capacity(state['size'])}" if state["size"] else f"已接收 {capacity(state['received'])}（网站未提供总大小）"))
            if state["size"]:
                self.file_progress.stop()
                self.file_progress.configure(mode="determinate", value=min(state["received"] / state["size"] * 100, 99))
            elif str(self.file_progress.cget("mode")) != "indeterminate":
                self.file_progress.configure(mode="indeterminate")
                self.file_progress.start(15)
        else:
            self.file_progress.stop()
            self.file_progress.configure(mode="determinate", value=100 if done else 0)
        self.state_text.set(f"正在下载 REP · {done} / {total}")

    def poll(self):
        for _ in range(200):
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "phase":
                self.active_phase = data
            elif kind == "local_event":
                self.local_page.receive_event(*data)
            elif kind == "log":
                self.add_log(data)
            elif kind == "team_candidates":
                self.download_progress.stop()
                self.download_progress.configure(mode="determinate", value=0)
                self.receive_team_candidates(data)
            elif kind == "matches" and self.active_phase == "download":
                self.receive_team_candidates({"query": self.query_key(), "candidates": [{"tag": self.runner.options.team,
                    "name": self.runner.options.team, "score": 3, "matches": data}]})
            elif kind == "jobs":
                if self.active_phase == "download":
                    for job in data:
                        self.update_download_job(job)
                    self.download_progress.stop()
                    self.download_progress.configure(mode="determinate", maximum=100, value=0)
                else:
                    self.convert_progress.configure(maximum=max(len(data), 1), value=0)
            elif kind == "job":
                if self.active_phase == "download":
                    self.update_download_job(data)
                else:
                    self.conversion_jobs[str(data["report_id"])] = data
                    self.refresh_conversion()
                    self.convert_detail.set(f"{STATUS.get(data['status'], data['status'])} · {' vs '.join(data['opponents'])} / G{data['game_number']} / {data['team']} 视角")
            elif kind == "download_progress":
                self.receive_download_progress(data)
            elif kind == "progress":
                self.convert_progress.configure(value=data["done"])
                self.state_text.set(f"转换进度 {data['done']} / {data['total']} · 新转换 {data['converted']} · 跳过 {data['skipped']} · 失败 {data['failed']}")
            elif kind == "compose_progress":
                self.convert_detail.set(f"正在合成 BP 信息与舰船轨迹 · 已处理 {data['frames']} 帧")
            elif kind == "complete":
                self.last_output = Path(data["output"])
                if data["phase"] == "download":
                    self.source.set(str(self.last_output / "manifest.json"))
                    self.load_conversion(silent=True)
                    self.state_text.set("REP 下载结束，可前往转换页")
                    self.download_detail.set(f"成功 {data['downloaded']} 份 · 失败 {data['failed']} 份 · 网站尚无 REP {data['missing']} 项")
                    self.detail_text.set("下载与转换已分开。请点击「02 转换视频」，选择视角、BP 和舰船轨迹后开始转换。")
                else:
                    self.state_text.set("视频转换结束")
                    self.detail_text.set(f"新转换 {data['converted']} 个 · 相同选项跳过 {data['skipped']} 个 · 失败 {data['failed']} 个")
            elif kind == "search_complete":
                self.download_progress.stop()
                self.download_progress.configure(mode="determinate", value=0)
                self.download_count.set(f"查询完成：{data} 场比赛")
                self.state_text.set("比赛查询完成")
                self.detail_text.set("可全部下载，或勾选部分比赛后下载。")
            elif kind == "stopped":
                self.state_text.set("任务已停止")
                self.detail_text.set("已完成文件已保留，可继续下载或在转换页重试。")
                if self.active_phase == "download" and (self.last_output / "manifest.json").exists():
                    self.source.set(str(self.last_output / "manifest.json"))
                    self.load_conversion(silent=True)
            elif kind == "error":
                self.state_text.set("任务未完成，可重试")
                if self.active_phase.startswith("local_"):
                    self.local_page.detail.set(data)
                self.add_log(data)
                messagebox.showerror("任务未完成", data, parent=self)
            elif kind == "idle":
                for bar in (self.download_progress, self.file_progress):
                    if str(bar.cget("mode")) == "indeterminate":
                        bar.stop()
                        bar.configure(mode="determinate", value=0)
                self.busy(False)
        self.after(120, self.poll)

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("TFrame", background=BG)
        s.configure("TLabel", background=BG, foreground=TEXT)
        s.configure("TEntry", fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT, bordercolor="#324663", padding=8)
        s.map("TEntry", fieldbackground=[("disabled", PANEL)])
        s.configure("TButton", background="#233750", foreground=TEXT, borderwidth=0, padding=(16, 9), font=("Microsoft YaHei UI", 10))
        s.map("TButton", background=[("active", "#314969"), ("disabled", "#1c2b3f")], foreground=[("disabled", "#637893")])
        s.configure("Accent.TButton", background=ACCENT, foreground="#09281f", font=("Microsoft YaHei UI", 10, "bold"))
        s.map("Accent.TButton", background=[("active", "#7be4ca"), ("disabled", "#254c49")])
        s.configure("TCheckbutton", background=PANEL, foreground=TEXT, padding=(0, 5))
        s.map("TCheckbutton", background=[("active", PANEL)], foreground=[("disabled", "#637893")])
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=37,
                    borderwidth=0, font=("Microsoft YaHei UI", 9))
        s.configure("Treeview.Heading", background="#21324b", foreground=MUTED, padding=(8, 10), borderwidth=0,
                    font=("Microsoft YaHei UI", 9))
        s.map("Treeview", background=[("selected", "#245762")], foreground=[("selected", "#ffffff")])
        s.map("Treeview.Heading", background=[("active", "#2d4565")])
        s.configure("Horizontal.TProgressbar", troughcolor="#21324b", background=ACCENT, borderwidth=0, thickness=6)
        s.configure("Vertical.TScrollbar", background="#31435d", troughcolor=PANEL, borderwidth=0, arrowsize=12)


    def label(self, parent, text="", *, size=10, color=TEXT, bold=False, bg=None, **kw):
        return tk.Label(parent, text=text, bg=bg or parent.cget("bg"), fg=color,
                        font=("Microsoft YaHei UI", size, "bold" if bold else "normal"), **kw)


    def browse(self):
        folder = filedialog.askdirectory(title="选择保存位置", initialdir=self.vars["output"].get() or str(self.root_dir))
        if folder:
            self.vars["output"].set(folder)


    def settings(self):
        dialog = tk.Toplevel(self)
        dialog.title("设置")
        dialog.configure(bg=PANEL)
        dialog.geometry("670x330")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        body = tk.Frame(dialog, bg=PANEL, padx=24, pady=22)
        body.pack(fill="both", expand=True)
        self.label(body, "转换与网络", size=16, bold=True).pack(anchor="w")
        self.label(body, "默认自动下载轻量游戏素材，无需安装完整游戏。", color=MUTED, size=9).pack(anchor="w", pady=(5, 16))
        self.label(body, "本地游戏目录（可选，须与回放版本对应）", color=MUTED).pack(anchor="w")
        line = tk.Frame(body, bg=PANEL)
        line.pack(fill="x", pady=(6, 13))
        ttk.Entry(line, textvariable=self.vars["game_dir"]).pack(side="left", fill="x", expand=True)
        def choose():
            folder = filedialog.askdirectory(parent=dialog, title="选择 World of Warships 安装目录")
            if folder:
                self.vars["game_dir"].set(folder)
        ttk.Button(line, text="浏览", command=choose).pack(side="right", padx=(8, 0))
        self.label(body, "HTTP 代理（可选，例如 http://127.0.0.1:7897）", color=MUTED).pack(anchor="w")
        ttk.Entry(body, textvariable=self.vars["proxy"]).pack(fill="x", pady=6)
        def save():
            try:
                self.save_settings()
                dialog.destroy()
            except OSError as e:
                messagebox.showerror("保存失败", str(e), parent=dialog)
        ttk.Button(body, text="保存", command=save, style="Accent.TButton").pack(anchor="e", pady=(10, 0))


    def get_options(self):
        values = {k: v.get() for k, v in self.vars.items()}
        values["trails"] = values["trail_mode"] != "none"
        return Options(**values, trail_selections=self.trail_selections)


    def open_bp(self):
        from .bp_gui import BanPickWindow
        try:
            BanPickWindow(self)
        except (AppError, OSError) as e:
            messagebox.showerror("无法打开查询", str(e), parent=self)

    def open_update(self):
        from .update_gui import UpdateWindow
        if self.update_window and self.update_window.winfo_exists():
            self.update_window.deiconify()
            self.update_window.lift()
        else:
            self.update_window = UpdateWindow(self)

    def update_running(self):
        window = self.update_window
        return bool(window and window.winfo_exists() and window.worker and window.worker.is_alive())


    def add_log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        if int(self.log_box.index("end-1c").split(".")[0]) > 400:
            self.log_box.delete("1.0", "100.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")


    def close(self):
        if self.update_window and self.update_window.winfo_exists():
            self.update_window.close()
        if self.worker and self.worker.is_alive():
            self.stop()
        if self.worker and self.worker.is_alive() or self.update_running():
            self.after(250, self._close_when_idle)
        else:
            self.destroy()


    def _close_when_idle(self):
        if self.worker and self.worker.is_alive() or self.update_running():
            self.after(250, self._close_when_idle)
        else:
            self.destroy()



def capacity(size):
    return f"{size / 1024 / 1024:.2f} MB" if size >= 1024 * 1024 else f"{size / 1024:.1f} KB"


def run_gui():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    Application().mainloop()
