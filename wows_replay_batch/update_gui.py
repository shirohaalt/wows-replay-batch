"""Manual update checking and download, independent of REP/video tasks."""
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import __version__
from .core import AppError, Cancelled
from .ui_theme import ACCENT, BG, FIELD, MUTED, PANEL, TEXT
from .updates import UpdateClient, repository_name


def capacity(value):
    return f"{value / 1024 / 1024:.2f} MB"


class UpdateWindow(tk.Toplevel):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.title("软件更新")
        self.geometry("770x610")
        self.minsize(650, 530)
        self.configure(bg=BG)
        self.repository = tk.StringVar(value=owner.update_repository)
        self.status = tk.StringVar(value="点击检查更新，读取 GitHub 最新正式版本。")
        self.detail = tk.StringVar(value="更新包下载到程序目录的 updates 文件夹。")
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.client = None
        self.release = None
        self.downloaded = None
        self.closing = False
        body = tk.Frame(self, bg=BG, padx=24, pady=22)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="软件更新", bg=BG, fg=TEXT, font=("Microsoft YaHei UI", 19, "bold")).pack(anchor="w")
        tk.Label(body, text=f"当前版本：{__version__}", bg=BG, fg=MUTED).pack(anchor="w", pady=(7, 15))
        tk.Label(body, text="GitHub 仓库（owner/repository 或仓库网址）", bg=BG, fg=MUTED).pack(anchor="w")
        self.entry = ttk.Entry(body, textvariable=self.repository)
        self.entry.pack(fill="x", pady=(6, 12))
        actions = tk.Frame(body, bg=BG)
        actions.pack(fill="x")
        self.check_button = ttk.Button(actions, text="检查更新", command=self.check)
        self.check_button.pack(side="left", padx=(0, 8))
        self.download_button = ttk.Button(actions, text="下载更新", command=self.download, state="disabled", style="Accent.TButton")
        self.download_button.pack(side="left", padx=(0, 8))
        self.cancel_button = ttk.Button(actions, text="取消", command=lambda: self.cancel.set(), state="disabled")
        self.cancel_button.pack(side="left")
        self.folder_button = ttk.Button(actions, text="打开下载位置", command=self.open_folder, state="disabled")
        self.folder_button.pack(side="right")
        tk.Label(body, textvariable=self.status, bg=BG, fg=ACCENT, anchor="w", wraplength=700).pack(fill="x", pady=(16, 9))
        self.progress = ttk.Progressbar(body, maximum=100)
        self.progress.pack(fill="x")
        tk.Label(body, textvariable=self.detail, bg=BG, fg=MUTED, anchor="w", wraplength=700).pack(fill="x", pady=(8, 12))
        tk.Label(body, text="更新说明", bg=BG, fg=TEXT).pack(anchor="w", pady=(0, 6))
        tk.Label(body, text="下载完成后自行解压使用；下载、转换任务可继续运行。", bg=BG, fg=MUTED, anchor="w").pack(side="bottom", fill="x", pady=(12, 0))
        notes_frame = tk.Frame(body, bg=PANEL)
        notes_frame.pack(fill="both", expand=True)
        self.notes = tk.Text(notes_frame, bg=FIELD, fg=TEXT, relief="flat", padx=12, pady=10, wrap="word", height=1, state="disabled")
        self.notes.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(notes_frame, command=self.notes.yview)
        scroll.pack(side="right", fill="y")
        self.notes.configure(yscrollcommand=scroll.set)
        self.repository.trace_add("write", lambda *_: self.invalidate())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_id = self.after(100, self.poll)

    def invalidate(self):
        self.release, self.downloaded = None, None
        self.download_button.configure(state="disabled")
        self.folder_button.configure(state="disabled")
        self.status.set("仓库已变化，请重新检查更新。")
        self.detail.set("更新包下载到程序目录的 updates 文件夹。")
        self.progress.configure(value=0)
        self.set_notes("")

    def set_notes(self, text):
        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")
        self.notes.insert("end", text)
        self.notes.configure(state="disabled")

    def busy(self, value):
        self.entry.configure(state="disabled" if value else "normal")
        self.check_button.configure(state="disabled" if value else "normal")
        self.cancel_button.configure(state="normal" if value else "disabled")
        available = self.release and self.release.newer_than(__version__)
        self.download_button.configure(state="normal" if not value and available else "disabled")
        self.folder_button.configure(state="normal" if not value and self.downloaded else "disabled")

    def launch(self, operation):
        self.cancel = threading.Event()
        self.client = UpdateClient(self.owner.root_dir, cancel=self.cancel, proxy=self.owner.vars["proxy"].get())
        self.busy(True)
        def work():
            try:
                operation()
            except Cancelled:
                self.events.put(("cancelled", None))
            except Exception as error:
                self.events.put(("error", str(error)))
            finally:
                self.events.put(("idle", None))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def check(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            repository = repository_name(self.repository.get())
            self.owner.update_repository = repository
            self.owner.save_settings()
            self.release, self.downloaded = None, None
            self.set_notes("")
            self.detail.set("正在连接 GitHub…")
            self.status.set("检查更新中…")
            self.progress.configure(mode="indeterminate")
            self.progress.start(15)
            self.launch(lambda: self.events.put(("release", self.client.check(repository))))
        except (AppError, OSError) as error:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.status.set(str(error))

    def download(self):
        if not self.release or self.worker and self.worker.is_alive():
            return
        self.downloaded = None
        self.status.set(f"正在下载 {self.release.version}…")
        self.progress.configure(mode="determinate", value=0)
        def operation():
            path = self.client.download(self.release, lambda done, total: self.events.put(("progress", (done, total))))
            self.events.put(("downloaded", path))
        try:
            self.launch(operation)
        except AppError as error:
            self.status.set(str(error))
            self.busy(False)

    def poll(self):
        for _ in range(200):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "release":
                self.release = value
                self.status.set(f"发现新版本 {value.version}" if value.newer_than(__version__) else f"当前已是最新版本（发布版本 {value.version}）")
                self.detail.set(f"更新包 {capacity(value.size)}，下载完成后校验 SHA-256。")
                self.set_notes(value.notes)
            elif kind == "progress":
                done, total = value
                self.progress.configure(value=min(done / max(total, 1) * 100, 99))
                self.detail.set(f"已接收 {capacity(done)} / {capacity(total)}" + (" · 正在校验…" if done >= total else ""))
            elif kind == "downloaded":
                self.downloaded = value
                self.status.set("下载完成，文件校验通过。")
                self.progress.configure(value=100)
                self.detail.set(f"已保存：{value}")
            elif kind == "cancelled":
                self.status.set("已取消，可重新检查或下载。")
                self.detail.set("未完成的临时下载文件已清理。")
            elif kind == "error":
                self.status.set("更新未完成，可重试。")
                self.detail.set(value)
            elif kind == "idle":
                if str(self.progress.cget("mode")) == "indeterminate":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                self.busy(False)
        if self.closing and not (self.worker and self.worker.is_alive()):
            self.destroy()
        else:
            self.poll_id = self.after(100, self.poll)

    def open_folder(self):
        if self.downloaded and self.downloaded.is_file():
            try:
                os.startfile(str(self.downloaded.parent))
            except OSError as error:
                messagebox.showerror("无法打开下载位置", str(error), parent=self)

    def close(self):
        if self.worker and self.worker.is_alive():
            self.closing = True
            self.cancel.set()
            self.status.set("正在取消，请稍候…")
        else:
            self.after_cancel(self.poll_id)
            self.destroy()
