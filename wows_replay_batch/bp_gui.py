from __future__ import annotations

import csv
import json
import queue
import threading
import tkinter as tk
import webbrowser
from dataclasses import asdict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .core import AppError, Cancelled, Runner, team_matches, write_json
from .drafts import statistics
from .gui import ACCENT, BG, FIELD, MUTED, PANEL, TEXT
from .ui_theme import configure_stripes, stripe_rows


class BanPickWindow(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("队伍 BAN/PICK 查询")
        self.geometry(f"1200x{min(900, self.winfo_screenheight() - 90)}")
        self.minsize(1000, 680)
        self.configure(bg=BG)
        self.owner = parent
        self.options = parent.get_download_options()
        self.runner = Runner(self.options, cancel=threading.Event(), emit=lambda k, d: self.events.put((k, d)))
        self.events = queue.Queue()
        self.rows = []
        self.team_tag = self.options.team
        self.busy = False
        self.status = tk.StringVar(value="准备读取 BAN/PICK…")
        self.filter = tk.StringVar()
        top = tk.Frame(self, bg=BG, padx=22, pady=18)
        top.pack(fill="x")
        tk.Label(top, text=f"{self.options.team}  /  BAN · PICK", bg=BG, fg=TEXT, font=("Microsoft YaHei UI", 19, "bold")).pack(side="left")
        self.refresh = ttk.Button(top, text="刷新队伍数据", command=self.load)
        self.refresh.pack(side="right")
        ttk.Button(top, text="导出 JSON", command=lambda: self.export(False)).pack(side="right", padx=8)
        ttk.Button(top, text="导出 CSV", command=lambda: self.export(True)).pack(side="right")
        search = tk.Frame(self, bg=BG, padx=22)
        search.pack(fill="x")
        tk.Label(search, text="筛选对手、舰船、地图或玩家", bg=BG, fg=MUTED).pack(side="left", padx=(0, 12))
        ttk.Entry(search, textvariable=self.filter).pack(side="left", fill="x", expand=True)
        self.filter.trace_add("write", lambda *_: self.fill_matches())
        tk.Label(self, textvariable=self.status, bg=BG, fg=ACCENT, anchor="w").pack(fill="x", padx=22, pady=12)
        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=22)
        left = ttk.Frame(panes)
        right = ttk.Frame(panes)
        panes.add(left, weight=1)
        panes.add(right, weight=3)
        self.matches = self.make_table(left, [("match", "比赛", 210), ("round", "阶段 / 轮次", 175)])
        self.matches.bind("<<TreeviewSelect>>", lambda _: self.show_match())
        self.tabs = ttk.Notebook(right, style="Draft.TNotebook")
        self.tabs.pack(fill="both", expand=True)
        self.bans = self.new_tab("双方禁船", [("order", "禁船顺序", 80), ("team", "提交 BAN 的队伍", 140), ("ship", "舰船", 195), ("scope", "适用范围", 135)])
        self.maps = self.new_tab("地图 BAN/PICK", [("order", "地图顺序", 80), ("kind", "操作", 85), ("team", "选择方", 130), ("map", "地图", 200), ("spawn", "出生点选择方", 130)])
        self.rosters = self.new_tab("每局实际阵容", [("game", "对局 / 地图", 170), ("team", "队伍", 80), ("player", "玩家", 210), ("ship", "实际出场舰船", 180)])
        self.stats = self.new_tab("队伍统计", [("kind", "类别", 145), ("item", "舰船 / 地图", 240), ("count", "次数", 80)])
        bottom = tk.Frame(self, bg=BG, padx=22, pady=14)
        bottom.pack(fill="x")
        tk.Label(bottom, text="选船展示双方比赛报告中的实际阵容；未公开的选船过程不会补写为 PICK。", bg=BG, fg=MUTED).pack(side="left")
        ttk.Button(bottom, text="打开原站记录", command=self.open_source).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_id = self.after(100, self.poll)
        self.load_id = self.after(150, self.load)
        try:
            cached = json.loads((self.runner.output / "ban-pick.json").read_text("utf-8"))
            if isinstance(cached, list):
                self.rows = cached
                self.team_tag = next((t["tag"] for m in cached for t in m["teams"] if team_matches({"teamTag": t["tag"], "teamName": t.get("name", "")}, self.options.team)), self.options.team)
                self.fill_matches()
                self.fill_stats()
                self.status.set(f"已读取 {len(cached)} 场缓存记录，正在自动刷新。")
        except (OSError, ValueError, KeyError):
            pass

    def make_table(self, parent, columns):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", selectmode="browse")
        configure_stripes(tree)
        for key, label, width in columns:
            tree.heading(key, text=label)
            tree.column(key, width=width, minwidth=45, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return tree

    def new_tab(self, title, columns):
        frame = ttk.Frame(self.tabs)
        self.tabs.add(frame, text=title)
        return self.make_table(frame, columns)

    def load(self):
        if self.busy:
            return
        self.busy = True
        self.refresh.configure(state="disabled")
        self.status.set("查询比赛和双方 BAN/PICK 记录…")
        # A new client refreshes previous pending matches instead of reusing a
        # stale in-memory vote record.
        self.runner = Runner(self.options, cancel=threading.Event(), emit=lambda k, d: self.events.put((k, d)))
        def work():
            try:
                matches = self.runner.search()
                tag = self.runner.options.team
                drafts = self.runner.client.drafts(matches)
                write_json(self.runner.output / "ban-pick.json", drafts)
                self.events.put(("loaded", (drafts, tag)))
            except Cancelled:
                pass
            except Exception as e:
                self.events.put(("error", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "log":
                    self.status.set(data[11:] if data.startswith("[") else data)
                elif kind == "loaded":
                    self.rows, self.team_tag = data
                    self.busy = False
                    self.refresh.configure(state="normal")
                    self.fill_matches()
                    self.fill_stats()
                    games = sum(g["played"] for m in self.rows for g in m["games"])
                    self.status.set(f"{self.team_tag}：{len(self.rows)} 场比赛，{games} 局已公开阵容。")
                elif kind == "error":
                    self.busy = False
                    self.refresh.configure(state="normal")
                    self.status.set("查询未完成，可刷新重试。")
                    messagebox.showerror("BAN/PICK 查询失败", data, parent=self)
        except queue.Empty:
            pass
        self.poll_id = self.after(120, self.poll)

    def fill_matches(self):
        self.matches.delete(*self.matches.get_children())
        term = self.filter.get().strip().casefold()
        for item in self.rows:
            if term and term not in json.dumps(item, ensure_ascii=False).casefold():
                continue
            self.matches.insert("", "end", iid=str(item["match_id"]), values=(" vs ".join(t["tag"] for t in item["teams"]), f"{item['stage']} / {item['round']}"))
        children = self.matches.get_children()
        stripe_rows(self.matches)
        if children:
            self.matches.selection_set(children[0])
        else:
            for tree in (self.bans, self.maps, self.rosters):
                tree.delete(*tree.get_children())

    def show_match(self):
        selection = self.matches.selection()
        if not selection:
            return
        item = next(r for r in self.rows if r["match_id"] == int(selection[0]))
        for tree in (self.bans, self.maps, self.rosters):
            tree.delete(*tree.get_children())
        bans = item["ship_bans"] + [b for e in item["map_events"] for b in e["ship_bans"]]
        for ban in bans:
            self.bans.insert("", "end", values=(ban["order"] or "—", ban["actor"], ban["ship"], "全系列" if ban["scope"] is None else "指定地图"))
        if not bans:
            self.bans.insert("", "end", values=("—", "—", "本阶段未启用禁船" if not item["ship_bans_enabled"] else "尚无公开禁船记录", "—"))
        for event in item["map_events"]:
            self.maps.insert("", "end", values=(event["order"] or "—", event["kind"], event["actor"], event["map"], event["spawn_actor"] if event["kind"] == "PICK" else "—"))
        if not item["map_events"]:
            for game in item["games"]:
                self.maps.insert("", "end", values=(game["number"], "指定" if not item["map_votes_enabled"] else "已定地图", "赛事指定" if not item["map_votes_enabled"] else "未公开选择方", game["map"], "—"))
        for game in item["games"]:
            for team in game["teams"]:
                for player in team["players"]:
                    self.rosters.insert("", "end", values=(f"G{game['number']} / {game['map']}", team["tag"], player["name"], player["ship"]))
                if not team["reported"]:
                    self.rosters.insert("", "end", values=(f"G{game['number']} / {game['map']}", team["tag"], "未进行 / 未公开报告", "—"))
        if item["vote_error"]:
            self.status.set("本场投票记录暂未读取完整；已展示可取得的阵容信息，可刷新重试。")
        for tree in (self.bans, self.maps, self.rosters):
            stripe_rows(tree)

    def fill_stats(self):
        self.stats.delete(*self.stats.get_children())
        for category, counts in statistics(self.rows, self.team_tag).items():
            for item, count in counts.most_common():
                self.stats.insert("", "end", values=(category, item, count))
        stripe_rows(self.stats)

    def export(self, csv_format):
        if not self.rows:
            return
        filename = filedialog.asksaveasfilename(parent=self, defaultextension=".csv" if csv_format else ".json",
                                               initialfile=f"{self.team_tag}_BAN_PICK", filetypes=[("CSV" if csv_format else "JSON", "*.csv" if csv_format else "*.json")])
        if not filename:
            return
        if not csv_format:
            write_json(Path(filename), self.rows)
        else:
            with open(filename, "w", encoding="utf-8-sig", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(["比赛ID", "比赛", "类别", "顺序", "队伍", "对局", "地图/舰船", "玩家"])
                for row in self.rows:
                    label = " vs ".join(t["tag"] for t in row["teams"])
                    for ban in row["ship_bans"]:
                        writer.writerow([row["match_id"], label, "禁船", ban["order"], ban["actor"], "", ban["ship"], ""])
                    for event in row["map_events"]:
                        writer.writerow([row["match_id"], label, "地图 " + event["kind"], event["order"], event["actor"], "", event["map"], ""])
                        for ban in event["ship_bans"]:
                            writer.writerow([row["match_id"], label, "地图禁船", ban["order"], ban["actor"], event["map"], ban["ship"], ""])
                    for game in row["games"]:
                        for team in game["teams"]:
                            for player in team["players"]:
                                writer.writerow([row["match_id"], label, "实际出场", "", team["tag"], game["number"], player["ship"], player["name"]])
        self.status.set(f"已导出：{filename}")

    def open_source(self):
        selected = self.matches.selection()
        if selected:
            webbrowser.open(f"{self.runner.tournament.url}/{selected[0]}/vote")

    def close(self):
        self.runner.cancel.set()
        for callback in (self.poll_id, self.load_id):
            self.after_cancel(callback)
        self.destroy()


class TrailWindow(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.owner = parent
        self.title("选择显示轨迹的舰船")
        self.geometry("1190x750")
        self.minsize(1060, 680)
        self.configure(bg=BG)
        self.events = queue.Queue()
        self.runner = parent.conversion_runner(cancel=threading.Event())
        self.selections = {k: list(v) for k, v in parent.trail_selections.items()}
        self.jobs = []
        self.checks = []
        self.current = self.loaded_id = None
        self.generation = 0
        self.loading = False
        self.status = tk.StringVar(value="选择一份已下载回放，再勾选需要显示轨迹的舰船。")
        try:
            manifest = self.runner.load_manifest()[1]
            self.jobs = [j for j in manifest["jobs"] if self.runner.replay_path(j).is_file()]
        except (OSError, ValueError, KeyError, AppError):
            pass
        tk.Label(self, text="指定舰船轨迹", bg=BG, fg=TEXT, font=("Microsoft YaHei UI", 19, "bold")).pack(anchor="w", padx=22, pady=(18, 8))
        tk.Label(self, text="只改变轨迹线；其他舰船图标照常保留。同型舰按玩家分别勾选。", bg=BG, fg=MUTED).pack(anchor="w", padx=22)
        tk.Label(self, textvariable=self.status, bg=BG, fg=ACCENT).pack(anchor="w", padx=22, pady=12)
        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=22)
        left, right = ttk.Frame(panes), tk.Frame(panes, bg=PANEL)
        panes.add(left, weight=2)
        panes.add(right, weight=3)
        self.list = ttk.Treeview(left, columns=("match",), show="", selectmode="browse", style="Trail.Treeview")
        self.list.column("match", width=350, minwidth=280, stretch=True, anchor="w")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.list.yview)
        self.list.configure(yscrollcommand=scroll.set)
        self.list.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.list.tag_configure("alternate", background="#122136")
        self.job_by_id = {str(job["report_id"]): job for job in self.jobs}
        for index, job in enumerate(self.jobs):
            label = f"{' vs '.join(job['opponents'])}   G{job['game_number']}\n{job['map']}  ·  {job['team']} 视角"
            if job.get("local_file"):
                label = f"{Path(job['replay']).name}\n{job['map']}  ·  {job['team']}"
            self.list.insert("", "end", iid=str(job["report_id"]), values=(label,),
                             tags=("alternate",) if index % 2 else ())
        self.list.bind("<<TreeviewSelect>>", self.choose)
        self.list.bind("<Button-1>", lambda _: "break" if self.loading else None)
        self.list.bind("<KeyPress>", lambda _: "break" if self.loading else None)
        toolbar = tk.Frame(right, bg=PANEL)
        toolbar.pack(fill="x", padx=12, pady=10)
        for title, side in (("全选", "all"), ("只选本方", "own"), ("只选对方", "enemy"), ("清空", "none")):
            ttk.Button(toolbar, text=title, command=lambda value=side: self.select_side(value)).pack(side="left", padx=(0, 8))
        ship_list = tk.Frame(right, bg=PANEL)
        ship_list.pack(fill="both", expand=True, padx=12)
        self.ship_canvas = tk.Canvas(ship_list, bg=PANEL, highlightthickness=0, yscrollincrement=24)
        ship_scroll = ttk.Scrollbar(ship_list, orient="vertical", command=self.ship_canvas.yview)
        self.ship_canvas.configure(yscrollcommand=ship_scroll.set)
        self.ship_canvas.pack(side="left", fill="both", expand=True)
        ship_scroll.pack(side="right", fill="y")
        self.grid_frame = tk.Frame(self.ship_canvas, bg=PANEL)
        ship_window = self.ship_canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind("<Configure>", lambda _: self.ship_canvas.configure(scrollregion=self.ship_canvas.bbox("all")))
        self.ship_canvas.bind("<Configure>", lambda e: self.ship_canvas.itemconfigure(ship_window, width=e.width))
        self.ship_canvas.bind("<MouseWheel>", self.scroll_ships)
        self.grid_frame.bind("<MouseWheel>", self.scroll_ships)
        self.grid_frame.columnconfigure(0, weight=1)
        self.grid_frame.columnconfigure(1, weight=1)
        footer = tk.Frame(self, bg=BG, padx=22, pady=16)
        footer.pack(side="bottom", fill="x", before=panes)
        tk.Label(footer, text="“指定舰船”模式下，未勾选的对局不显示轨迹。保存后返回主界面开始转换。", bg=BG, fg=MUTED).pack(side="left")
        self.save_button = ttk.Button(footer, text="保存选择", command=self.save, style="Accent.TButton")
        self.save_button.pack(side="right")
        if not self.jobs:
            local = getattr(self.runner, "is_local", False)
            self.status.set("没有可读取的本地回放，请重新读取文件夹。" if local else "尚无已下载回放。请先下载回放，再读取舰船名单。")
            ttk.Button(self.grid_frame, text="重新读取文件夹" if local else "先下载队伍回放", command=self.download_first).pack(pady=40)
            self.save_button.configure(state="disabled")
        else:
            self.choose_id = self.after(150, lambda: self.list.selection_set(str(self.jobs[0]["report_id"]))
                                        if not self.list.selection() else None)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_id = self.after(120, self.poll)

    def stash(self):
        if self.current is not None and self.loaded_id == self.current["report_id"]:
            self.selections[str(self.current["report_id"])] = [row["entity_id"] for row, variable in self.checks if variable.get()]

    def choose(self, _event):
        if self.loading:
            return
        selected = self.list.selection()
        if not selected:
            return
        self.stash()
        self.current = self.job_by_id[selected[0]]
        self.loaded_id = None
        self.checks = []
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self.generation += 1
        self.loading = True
        self.list.state(["disabled"])
        generation, job = self.generation, self.current
        self.save_button.configure(state="disabled")
        self.status.set("正在读取此回放的舰船名单…")
        def work():
            from .video import read_roster
            try:
                roster = read_roster(self.runner, self.runner.replay_path(job))
                self.events.put((generation, roster, ""))
            except Exception as e:
                self.events.put((generation, [], str(e)))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            while True:
                generation, roster, error = self.events.get_nowait()
                if generation != self.generation:
                    continue
                self.loading = False
                self.list.state(["!disabled"])
                if error:
                    self.status.set("读取名单失败，可重新选择回放重试。")
                    messagebox.showerror("无法读取舰船", error, parent=self)
                    continue
                self.loaded_id = self.current["report_id"]
                saved = set(self.selections.get(str(self.loaded_id), []))
                counts = [0, 0]
                for row in roster:
                    col = 0 if row.get("relation") in ("Self", "Ally") else 1
                    variable = tk.BooleanVar(value=row["entity_id"] in saved)
                    button = ttk.Checkbutton(self.grid_frame, text=f"{row.get('ship_name', '')}\n{row['player_name']}",
                                             variable=variable, style="Ship.Studio.TCheckbutton")
                    button.grid(row=counts[col], column=col, sticky="ew", padx=(0, 10))
                    button.bind("<MouseWheel>", self.scroll_ships)
                    counts[col] += 1
                    self.checks.append((row, variable))
                    variable.trace_add("write", lambda *_: self.count())
                self.save_button.configure(state="normal")
                self.count()
        except queue.Empty:
            pass
        self.poll_id = self.after(120, self.poll)

    def count(self):
        self.status.set(f"本局 {len(self.checks)} 条舰船，已勾选 {sum(v.get() for _, v in self.checks)} 条。左侧可切换其他对局分别选择。")

    def scroll_ships(self, event):
        if event.delta:
            self.ship_canvas.yview_scroll(-3 if event.delta > 0 else 3, "units")
        return "break"

    def select_side(self, side):
        for row, variable in self.checks:
            own = row.get("relation") in ("Self", "Ally")
            variable.set(side == "all" or side == "own" and own or side == "enemy" and not own)

    def save(self):
        self.stash()
        self.owner.trail_selections = self.selections
        self.owner.vars["trail_mode"].set("selected")
        self.owner.vars["trails"].set(True)
        self.owner.on_trails_saved()
        self.close()

    def download_first(self):
        self.close()
        if getattr(self.runner, "is_local", False):
            self.owner.scan_folder()
        else:
            self.owner.notebook.select(self.owner.download_page)

    def close(self):
        self.runner.cancel.set()
        self.after_cancel(self.poll_id)
        if hasattr(self, "choose_id"):
            self.after_cancel(self.choose_id)
        self.destroy()
