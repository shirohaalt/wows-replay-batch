"""Download updates independently, then install when replay tasks have finished."""
import os
import queue
import threading
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk

from . import __version__
from .core import AppError, Cancelled
from .ui_theme import ACCENT, BG, FIELD, MUTED, PANEL, TEXT
from .updates import UpdateClient, repository_name
from .install_update import load_job, read_json, stage_update, start_installer, write_json


def capacity(value):
    return f"{value / 1024 / 1024:.2f} MB"


def dialogs_open(widget, excluded):
    return any((isinstance(child, tk.Toplevel) and child is not excluded) or
               (child is not excluded and dialogs_open(child, excluded)) for child in widget.winfo_children())


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
        self.prepared = None
        self.pending_install = False
        self.installer = None
        self.installer_started = 0
        self.install_supported = os.name == "nt" and bool(getattr(sys, "frozen", False))
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
        self.download_button = ttk.Button(actions, text="下载并安装" if self.install_supported else "下载更新", command=self.download, state="disabled", style="Accent.TButton")
        self.download_button.pack(side="left", padx=(0, 8))
        self.cancel_button = ttk.Button(actions, text="取消", command=self.cancel_update, state="disabled")
        self.cancel_button.pack(side="left")
        self.folder_button = ttk.Button(actions, text="打开下载位置", command=self.open_folder, state="disabled")
        self.folder_button.pack(side="right")
        tk.Label(body, textvariable=self.status, bg=BG, fg=ACCENT, anchor="w", wraplength=700).pack(fill="x", pady=(16, 9))
        self.progress = ttk.Progressbar(body, maximum=100)
        self.progress.pack(fill="x")
        tk.Label(body, textvariable=self.detail, bg=BG, fg=MUTED, anchor="w", wraplength=700).pack(fill="x", pady=(8, 12))
        tk.Label(body, text="更新说明", bg=BG, fg=TEXT).pack(anchor="w", pady=(0, 6))
        tk.Label(body, text="校验后等待当前任务结束，自动安装并重启；保留设置、回放与视频。" if self.install_supported else "源码运行时只下载更新包；自动安装请使用 Windows 发布版。", bg=BG, fg=MUTED, anchor="w", wraplength=700).pack(side="bottom", fill="x", pady=(12, 0))
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
        self.pending_install = False
        self.prepared = None
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
        locked = value or self.pending_install or self.installer is not None
        self.entry.configure(state="disabled" if locked else "normal")
        self.check_button.configure(state="disabled" if locked else "normal")
        self.cancel_button.configure(state="normal" if (value or self.pending_install) and self.installer is None else "disabled")
        available = self.release and self.release.newer_than(__version__)
        self.download_button.configure(state="normal" if not locked and available else "disabled")
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
        if not self.release or self.pending_install or self.installer or self.worker and self.worker.is_alive():
            return
        self.downloaded = None
        self.status.set(f"正在下载 {self.release.version}…")
        self.progress.configure(mode="determinate", value=0)
        def operation():
            path = self.client.download(self.release, lambda done, total: self.events.put(("progress", (done, total))))
            self.events.put(("downloaded", path))
            if self.install_supported:
                self.events.put(("staging", None))
                job = stage_update(self.owner.root_dir, path, self.release, cancel=self.cancel,
                                   progress=lambda done, total: self.events.put(("stage_progress", (done, total))))
                self.events.put(("prepared", job))
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
            elif kind == "staging":
                self.status.set("更新包校验通过，正在准备安装文件…")
                self.progress.configure(value=0)
            elif kind == "stage_progress":
                done, total = value
                self.progress.configure(value=min(done / max(total, 1) * 100, 100))
                self.detail.set(f"已准备 {capacity(done)} / {capacity(total)}")
            elif kind == "prepared":
                self.prepared = value
                if not self.closing and not self.cancel.is_set():
                    self.pending_install = True
                    self.status.set("安装准备完成，等待当前任务结束后自动安装并重启…")
                    self.detail.set("设置、回放和视频保留；旧程序文件会自动备份。")
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
        if self.pending_install and not self.closing and not (self.worker and self.worker.is_alive()) and not (self.owner.worker and self.owner.worker.is_alive()):
            if dialogs_open(self.owner, self):
                self.status.set("安装准备完成，请关闭舰船选择、BP 查询或设置窗口，随后自动安装。")
            else:
                self.begin_install()
        if self.installer:
            try:
                if self.poll_installer():
                    return
            except Exception as error:
                self.installer = None
                self.status.set("安装未开始，原程序保持可用。")
                self.detail.set(str(error))
                self.owner.busy(False)
                self.busy(False)
        if self.closing and not (self.worker and self.worker.is_alive()):
            self.destroy()
        else:
            self.poll_id = self.after(100, self.poll)

    def cancel_update(self):
        self.cancel.set()
        if self.pending_install:
            self.pending_install = False
            self.status.set("已取消安装，下载的更新包已保留。")
            self.detail.set("可重新点击下载并安装，已校验的下载文件会复用。")
            self.busy(False)

    def begin_install(self):
        self.pending_install = False
        try:
            self.owner.save_settings()
            self.installer = start_installer(self.prepared)
            self.installer_started = time.monotonic()
            self.owner.busy(True)
            self.status.set("正在启动安装器，程序即将自动重启…")
            self.busy(True)
        except Exception as error:
            self.status.set("未能启动安装，原程序保持可用。")
            self.detail.set(str(error))
            self.owner.busy(False)
            self.busy(False)

    def poll_installer(self):
        path, job = load_job(self.prepared)
        error = path.parent / "error.json"
        ready = path.parent / "ready.json"
        if error.is_file() or self.installer.poll() is not None or time.monotonic() - self.installer_started > 45:
            write_json(path.parent / "abort.json", {})
            self.status.set("安装器未能就绪，原程序保持可用。")
            self.detail.set(read_json(error)["error"] if error.is_file() else "请稍后重试，更新包和原数据均已保留。")
            self.installer = None
            self.owner.busy(False)
            self.busy(False)
        elif ready.is_file() and read_json(ready).get("token") == job["token"]:
            write_json(path.parent / "proceed.json", {"token": job["token"]})
            self.owner.destroy()
            return True

    def open_folder(self):
        if self.downloaded and self.downloaded.is_file():
            try:
                os.startfile(str(self.downloaded.parent))
            except OSError as error:
                messagebox.showerror("无法打开下载位置", str(error), parent=self)

    def close(self):
        if self.installer:
            return
        self.pending_install = False
        if self.worker and self.worker.is_alive():
            self.closing = True
            self.cancel.set()
            self.status.set("正在取消，请稍候…")
        else:
            self.after_cancel(self.poll_id)
            self.destroy()
