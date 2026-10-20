from __future__ import annotations

import hashlib
from contextlib import ExitStack
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import tomllib
import unicodedata
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse

import requests

DEFAULT_URL = "https://wows-tournaments.com/king-of-the-sea-xix/apac/matches"
DATA_URL = "https://raw.githubusercontent.com/landaire/wows-replay-data/main"
TOOL_VERSION = "v1.0.2"
TOOL_URL = f"https://github.com/landaire/wows-toolkit/releases/download/{TOOL_VERSION}/wows_toolkit_tools_{TOOL_VERSION}_win64.zip"
TOOL_SHA256 = "281f3fe425e5517e4657d60bfa9f05d41deaa999f80fcd02cbf5a12d27516c3d"
MAGIC = b"\x12\x32\x34\x11"
CACHE_REVISION = 2


class Cancelled(Exception):
    pass


class AppError(Exception):
    pass


class AmbiguousTeam(AppError):
    def __init__(self, query, candidates):
        self.candidates = candidates
        names = "、".join(f"{c['tag']}（{c['name']}）" for c in candidates)
        super().__init__(f"「{query}」匹配到多个队伍：{names}。请在下载页选择队伍，或输入更完整的队名。")


def app_home() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:110]
    if not value:
        return "unnamed"
    if value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        value = "_" + value
    return value


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def replay_meta(path: Path) -> dict:
    with path.open("rb") as f:
        header = f.read(12)
        if len(header) != 12 or header[:4] != MAGIC:
            raise AppError(f"下载内容不是有效的 .wowsreplay：{path.name}")
        count, length = struct.unpack_from("<II", header, 4)
        if not 1 <= count <= 32 or not 2 <= length <= 16 * 1024 * 1024:
            raise AppError(f"回放头部无效：{path.name}")
        try:
            meta = json.loads(f.read(length))
            if not meta.get("clientVersionFromExe"):
                raise ValueError("missing game version")
            return meta
        except (ValueError, UnicodeDecodeError) as e:
            raise AppError(f"回放元数据损坏：{path.name}") from e


def sha256(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def unpack_zstd(source: Path, destination: Path, expected_hash: str):
    if destination.exists() and sha256(destination).startswith(expected_hash):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".unpacking")
    try:
        with ExitStack() as stack:
            try:
                from compression import zstd
                stream = stack.enter_context(zstd.open(source, "rb"))
            except ImportError:
                import zstandard
                compressed = stack.enter_context(source.open("rb"))
                stream = stack.enter_context(zstandard.ZstdDecompressor().stream_reader(compressed))
            with temp.open("wb") as f:
                shutil.copyfileobj(stream, f)
        if not sha256(temp).startswith(expected_hash):
            raise AppError("游戏参数解压后的 SHA-256 校验失败。")
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)


@dataclass(frozen=True)
class Tournament:
    slug: str
    region: str

    @classmethod
    def parse(cls, url: str):
        parsed = urlparse(url.strip())
        parts = parsed.path.strip("/").split("/")
        if parsed.scheme != "https" or parsed.hostname != "wows-tournaments.com" or len(parts) < 2:
            raise AppError("请输入 https://wows-tournaments.com/赛事名/赛区/matches 格式的网址。")
        if not re.fullmatch(r"[a-z0-9-]+", parts[0]) or parts[1].lower() not in {"apac", "eu", "na", "int", "cis", "cn", "china", "international"}:
            raise AppError("赛事网址中的赛事名或赛区无效。")
        return cls(parts[0], parts[1].lower())

    @property
    def url(self):
        return f"https://wows-tournaments.com/{self.slug}/{self.region}/matches"

    @property
    def headers(self):
        return {"x-tournament-name": self.slug, "x-tournament-region": self.region.upper()}


class Network:
    def __init__(self, cancel=None, log=None, proxy=""):
        self.cancel = cancel or threading.Event()
        self.log = log or (lambda _: None)
        self.proxy = proxy.strip()
        if self.proxy and not re.match(r"^https?://[^\s]+$", self.proxy):
            raise AppError("代理地址应为 http://127.0.0.1:端口；不需要代理时留空。")
        self.local = threading.local()
        self.archive_lock = threading.Lock()
        self.replay_archives = {}

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    @property
    def session(self):
        if not hasattr(self.local, "session"):
            s = requests.Session()
            s.headers.update({"User-Agent": "WoWSReplayBatch/1.0 (+https://github.com/landaire/wows-toolkit)"})
            if self.proxy:
                s.proxies.update({"http": self.proxy, "https": self.proxy})
            self.local.session = s
        return self.local.session

    def request(self, method, url, **kwargs):
        for attempt in range(5):
            self.check()
            try:
                r = self.session.request(method, url, timeout=(10, 35), **kwargs)
                if r.status_code in (401, 403):
                    r.close()
                    raise AppError(f"网站拒绝访问（HTTP {r.status_code}）：{url.split('?')[0]}")
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                if getattr(e, "response", None) is not None:
                    status = e.response.status_code
                    e.response.close()
                    if status not in (408, 429, 500, 502, 503, 504):
                        raise AppError(f"网站返回 HTTP {status}：{url.split('?')[0]}") from e
                if attempt == 4:
                    raise AppError(f"网络请求失败，已重试 5 次：{url.split('?')[0]}（{type(e).__name__}）") from e
                if attempt in (1, 3):
                    self.log(f"网络波动，正在重试 {attempt + 1}/5：{url.split('?')[0]}")
                if self.cancel.wait(min(2 ** attempt, 6)):
                    raise Cancelled()

    def json(self, method, url, **kwargs):
        with self.request(method, url, **kwargs) as r:
            try:
                return r.json()
            except ValueError as e:
                raise AppError(f"网站没有返回有效的 JSON：{url.split('?')[0]}") from e

    def download(self, url: str, path: Path, expected_hash="", validator=None, progress=None, cache_hash="", headers=None, replay_job=None):
        self.check()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            try:
                local_hash = cache_hash or expected_hash
                if local_hash and not sha256(path).startswith(local_hash):
                    raise ValueError("hash mismatch")
                if validator:
                    validator(path)
                if local_hash or validator:
                    if progress:
                        progress(path.stat().st_size, path.stat().st_size)
                    return
            except (ValueError, AppError):
                pass
        temp = path.with_name(path.name + ".part")
        for attempt in range(4):
            self.check()
            try:
                with ExitStack() as stack:
                    r = stack.enter_context(self.request("GET", url, stream=True, headers={"Accept-Encoding": "identity", **(headers or {})}))
                    if replay_job is not None:
                        from .replay_archives import replay_response
                        r = stack.enter_context(replay_response(self, r, replay_job))
                    f = stack.enter_context(temp.open("wb"))
                    length = 0 if r.headers.get("Content-Encoding") else int(r.headers.get("Content-Length", 0))
                    received = 0
                    last = 0.0
                    if progress:
                        progress(0, length)
                    for chunk in r.iter_content(32 * 1024):
                        self.check()
                        if chunk:
                            f.write(chunk)
                            received += len(chunk)
                            if progress and time.monotonic() - last > 0.2:
                                progress(received, length)
                                last = time.monotonic()
                    if length and not r.headers.get("Content-Encoding") and received != length:
                        raise ValueError("incomplete download")
                if expected_hash and not sha256(temp).startswith(expected_hash):
                    raise ValueError("SHA-256 mismatch")
                if validator:
                    validator(temp)
                temp.replace(path)
                if progress:
                    progress(received, received)
                return
            except (requests.RequestException, ValueError, AppError) as e:
                if attempt == 3:
                    raise AppError(f"下载或校验失败：{path.name}（{e}）") from e
                if self.cancel.wait(attempt + 1):
                    raise Cancelled()
            finally:
                temp.unlink(missing_ok=True)


def team_matches(signup: dict, query: str) -> bool:
    query = query.strip()
    if re.fullmatch(r"\[[^]]+\]", query):
        query = query[1:-1]
    query = query.casefold()
    name = (signup.get("teamName") or "").strip()
    tag = (signup.get("teamTag") or "").strip()
    return query in {tag.casefold(), name.casefold(), re.sub(r"^\[[^]]+\]\s*", "", name).casefold()}


def team_keyword(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def team_search_score(signup, query):
    if team_matches(signup, query):
        return 3
    keyword = team_keyword(query)
    values = [team_keyword(signup.get(k) or "") for k in ("teamTag", "teamName")]
    if keyword and keyword in values:
        return 2
    return 1 if keyword and any(keyword in value for value in values) else 0


class TournamentClient:
    def __init__(self, tournament: Tournament, network: Network):
        self.tournament, self.net = tournament, network
        self.match_data = {}
        self.game_data = {}
        self.draft_data = {}

    def load_match(self, match_id):
        from .drafts import normalize_draft
        if match_id in self.match_data:
            return self.match_data[match_id]
        match = self.net.json("GET", f"https://wows-tournaments.com/api/matches/match?id={match_id}", headers=self.tournament.headers)
        games = {}
        for game in match.get("matchGames", []):
            detail = self.net.json("GET", f"https://wows-tournaments.com/api/matches/game?id={game['id']}&matchId={match_id}", headers=self.tournament.headers)
            games[game["id"]] = detail
            self.game_data[game["id"]] = detail
        vote, error = None, ""
        if match.get("hasVote"):
            try:
                vote = self.net.json("GET", f"https://wows-tournaments.com/api/votes/info?matchId={match_id}", headers=self.tournament.headers)
            except AppError as e:
                error = str(e)
                self.net.log(f"比赛 {match_id} 的 BAN/PICK 信息暂未读到：{e}")
        self.match_data[match_id] = match
        self.draft_data[match_id] = normalize_draft(match, games, vote, error)
        return match

    def drafts(self, matches):
        result = []
        for n, short in enumerate(matches, 1):
            self.net.check()
            self.load_match(short["id"])
            result.append(self.draft_data[short["id"]])
            self.net.log(f"BAN/PICK 查询：{n}/{len(matches)} 场")
        return result

    def _search_matches(self, team: str) -> list[dict]:
        if not team.strip():
            raise AppError("请填写队伍名称或队标，例如 DEMO。")
        # The public SPA requires an anonymous anti-forgery cookie even for search.
        self.net.json("GET", "https://wows-tournaments.com/api/identity/userinfo")
        token = self.net.session.cookies.get("wows-tournaments.antiforgery-request")
        if not token:
            raise AppError("未获取到网站的匿名访问令牌，请稍后重试。")
        headers = {**self.tournament.headers, "X-XSRF-TOKEN": token}
        found, seen, total = [], set(), None
        for page in range(1, 1001):
            self.net.check()
            body = {"signupId": None, "stageId": None, "groupId": None, "roundId": None,
                    "team": team.strip(), "matchStatus": [], "page": page, "pageSize": 21,
                    "sortColumn": None, "sortDirection": 0}
            result = self.net.json("POST", "https://wows-tournaments.com/api/matches/list", headers=headers, json=body)
            if not isinstance(result.get("items"), list) or result.get("local"):
                raise AppError("比赛列表接口返回异常，请重试。")
            total = result.get("totalCount", total)
            new = 0
            for match in result["items"]:
                if match["id"] not in seen:
                    seen.add(match["id"])
                    new += 1
                    found.append(match)
            self.net.log(f"读取比赛列表第 {page} 页，已检查 {len(seen)} 场。")
            # totalPages on this site can be inconsistent with totalCount/pageSize.
            if total is not None and len(seen) >= total:
                break
            if not new:
                if total is not None and len(seen) < total:
                    raise AppError(f"网站分页返回重复或空页：只读到 {len(seen)}/{total} 场。请重新查询。")
                break
        else:
            raise AppError("比赛列表页数异常，已停止查询。")
        return sorted(found, key=lambda m: (m.get("scheduledTime") or "", m["id"]))

    def find_teams(self, query):
        if not team_keyword(query):
            raise AppError("队伍关键词至少需要包含一个文字、字母或数字。")
        matches = self._search_matches(unicodedata.normalize("NFKC", query).strip())
        keyword = team_keyword(query)
        if not matches and keyword != query.strip().casefold():
            matches = self._search_matches(keyword)
        candidates = {}
        for match in matches:
            for opponent in match.get("matchOpponents", []):
                signup = opponent.get("signup") or {}
                score = team_search_score(signup, query)
                if not score:
                    continue
                tag = signup.get("teamTag") or signup.get("teamName") or "TBD"
                key = str(signup.get("id") or tag.casefold())
                candidate = candidates.setdefault(key, {"tag": tag, "name": signup.get("teamName") or tag,
                    "signup_id": signup.get("id"), "score": score, "matches": []})
                candidate["score"] = max(candidate["score"], score)
                if not any(m["id"] == match["id"] for m in candidate["matches"]):
                    candidate["matches"].append(match)
        return sorted(candidates.values(), key=lambda c: (-c["score"], c["tag"].casefold(), c["name"]))

    def search(self, team: str) -> list[dict]:
        candidates = self.find_teams(team)
        best = max((c["score"] for c in candidates), default=0)
        preferred = [c for c in candidates if best >= 2 and c["score"] == best]
        choice = preferred[0] if len(preferred) == 1 else candidates[0] if len(candidates) == 1 else None
        if choice is None and candidates:
            raise AmbiguousTeam(team, candidates)
        self.resolved_team = choice["tag"] if choice else team
        self.resolved_signup_id = choice.get("signup_id") if choice else None
        return choice["matches"] if choice else []

    def discover(self, matches: list[dict], team: str, both=False, signup_id=None) -> tuple[list[dict], list[dict]]:
        from .drafts import game_overlay
        jobs, missing = [], []
        seen = set()
        for n, short in enumerate(matches, 1):
            match = self.load_match(short['id'])
            self.net.log(f"查找回放 {n}/{len(matches)}：" + " vs ".join(o["signup"]["teamTag"] for o in match["matchOpponents"]))
            for game in match.get("matchGames", []):
                detail = self.game_data[game['id']]
                opponents = {o["id"]: o.get("signup") or {} for o in match["matchOpponents"]}
                targets = {oid for oid, s in opponents.items() if s.get("id") == signup_id} if signup_id is not None else {oid for oid, s in opponents.items() if team_matches(s, team)}
                if not targets:
                    raise AppError(f"比赛 {match['id']} 不属于当前选定队伍，请重新查询。")
                wanted = set(opponents) if both else targets
                available = set()
                for key in ("report1", "report2"):
                    report = detail.get(key)
                    if not report or report.get("opponentId") not in wanted or not report.get("replayFileId"):
                        continue
                    available.add(report["opponentId"])
                    if report["id"] in seen:
                        continue
                    seen.add(report["id"])
                    jobs.append({"report_id": report["id"], "match_id": match["id"], "game_id": game["id"],
                                 "game_number": game.get("number", 1), "team": opponents[report["opponentId"]].get("teamTag", team),
                                 "target_team": opponents[next(iter(targets))].get("teamTag", team),
                                 "opponents": [s.get("teamTag", "TBD") for s in opponents.values()],
                                 "stage": match.get("stageName", ""), "group": match.get("groupName", ""),
                                 "round": match.get("roundName", ""), "scheduled_time": match.get("scheduledTime", ""),
                                 "map": report.get("mapName") or game.get("mapName", ""),
                                 "source_url": f"{self.tournament.url}/{match['id']}/game/{game['id']}",
                                 "download_url": f"https://wows-tournaments.com/api/matches/report/replay?id={report['id']}",
                                 "draft": game_overlay(self.draft_data[match["id"]], game["id"])})
                for oid in wanted - available:
                    missing.append({"match_id": match["id"], "game_id": game["id"], "team": opponents[oid].get("teamTag"), "reason": "网站尚未提供该队伍的回放"})
            if not match.get("matchGames"):
                missing.append({"match_id": match["id"], "reason": "比赛尚无可下载对局"})
        return jobs, missing


class Tools:
    def __init__(self, root: Path, net: Network):
        self.root, self.net = root, net
        self.prepared = {}

    def renderer(self, executable="minimap_renderer_cpu.exe") -> Path:
        path = self.root / "tools" / "wows-toolkit" / executable
        if path.is_file():
            return path
        self.net.log(f"首次运行：下载官方 wows-toolkit {TOOL_VERSION} 转换工具（约 28 MB）。")
        archive = self.root / "tools" / "toolkit.zip"
        digest = TOOL_SHA256
        self.net.download(TOOL_URL, archive, expected_hash=digest, validator=lambda p: zipfile.ZipFile(p).close())
        target = path.parent
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as z:
            for name in ("minimap_renderer_cpu.exe", "minimap_renderer.exe", "replayshark.exe"):
                if (target / name).is_file():
                    continue
                with z.open(name) as src, (target / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        write_json(target / "version.json", {"version": TOOL_VERSION, "source": TOOL_URL, "sha256": sha256(archive)})
        archive.unlink(missing_ok=True)
        return path

    def game_data(self, replay: Path) -> Path:
        meta = replay_meta(replay)
        parts = re.split(r"[, .]+", meta["clientVersionFromExe"])
        version, build = ".".join(parts[:3]), int(parts[3])
        if build in self.prepared:
            return self.prepared[build]
        base = self.root / "cache" / "game-data"
        ready = base / f"{version}_{build}" / ".ready.json"
        if ready.exists():
            try:
                checkpoint = json.loads(ready.read_text("utf-8"))
                if checkpoint.get("revision") == CACHE_REVISION and all(
                    (base / "common" / h[:2] / h[2:]).is_file()
                    and sha256(base / "common" / h[:2] / h[2:]).startswith(h)
                    for h in checkpoint["hashes"]
                ):
                    self.prepared[build] = ready.parent
                    return ready.parent
            except (ValueError, OSError, KeyError):
                pass
        self.net.log(f"准备游戏数据 {version}（build {build}），首次运行需要下载并校验素材。")
        with self.net.request("GET", DATA_URL + "/builds.toml") as r:
            index = tomllib.loads(r.content.decode("utf-8"))
        entry = next((b for b in index["builds"] if b["build"] == build), None)
        if not entry:
            # Match the friendly version only; a different patch can change replay schemas.
            choices = [b for b in index["builds"] if b["version"] == version]
            entry = max(choices, key=lambda b: b["build"]) if choices else None
        if not entry:
            raise AppError(f"作者尚未发布 {version} / {build} 的游戏数据。可在设置中指定对应版本的本地游戏目录。")
        directory = entry["dir"]
        if not re.fullmatch(r"[0-9._-]+", directory):
            raise AppError("远程游戏数据目录名无效。")
        target = base / directory
        target.mkdir(parents=True, exist_ok=True)
        with self.net.request("GET", f"{DATA_URL}/{directory}/metadata.toml") as r:
            raw_metadata = r.content
        metadata = tomllib.loads(raw_metadata.decode("utf-8"))
        # A small portable tactical renderer does not need achievement images,
        # 3D models, commander portraits, ship silhouettes or other translations.
        required_prefixes = ("scripts/", "spaces/", "gui/fla/minimap/", "gui/fonts/",
                             "gui/consumables/", "gui/powerups/drops/", "gui/data/constants/",
                             "gui/battle_hud/markers/", "gui/battle_hud/markers_minimap/", "gui/battle_hud/icon_frag/",
                             "gui/battle_hud/capture_points/")
        hashes = {h for p, h in metadata["files"].items() if p.startswith(required_prefixes)}
        derived = metadata.get("derived", {})
        if not derived.get("game_params.rkyv"):
            hashes.add(metadata["files"]["content/GameParams.data"])
        for name in ("game_params.rkyv.zst", "translations/en/LC_MESSAGES/global.mo.zst"):
            if name in derived:
                hashes.add(derived[name])
            elif name.removesuffix(".zst") in derived:
                hashes.add(derived[name.removesuffix(".zst")])
        hashes = sorted(hashes)
        if any(not re.fullmatch(r"[0-9a-f]{20}", h) for h in hashes):
            raise AppError("游戏数据清单中的校验值无效。")

        def fetch(h):
            rel = f"common/{h[:2]}/{h[2:]}"
            self.net.download(f"{DATA_URL}/{rel}", base / rel, expected_hash=h)

        self.net.log(f"游戏素材共 {len(hashes)} 项；已缓存的文件将校验后跳过。")
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(fetch, h) for h in hashes]
            for n, future in enumerate(as_completed(futures), 1):
                try:
                    future.result()
                except BaseException:
                    for pending in futures:
                        pending.cancel()
                    raise
                if n % 40 == 0 or n == len(hashes):
                    self.net.log(f"游戏数据：{n}/{len(hashes)}")
        # v1.0.2 reads raw CAS objects for game parameters and translations;
        # upstream also publishes compressed copies to make transfer smaller.
        for name in ("game_params.rkyv", "translations/en/LC_MESSAGES/global.mo"):
            if name in derived and name + ".zst" in derived:
                packed, raw = derived[name + ".zst"], derived[name]
                unpack_zstd(base / "common" / packed[:2] / packed[2:], base / "common" / raw[:2] / raw[2:], raw)
                hashes.append(raw)
        # The CAS VFS can read the original manifest with a subset of assets;
        # optional UI assets are handled by the renderer's built-in fallback.
        (target / "metadata.toml").write_bytes(raw_metadata)
        constants = f"{DATA_URL}/{directory}/constants.json"
        try:
            with self.net.request("GET", constants) as r:
                (target / "constants.json").write_bytes(r.content)
        except AppError as e:
            self.net.log(f"版本常量未下载，将使用渲染器默认值：{e}")
        write_json(target / ".ready.json", {"build": entry["build"], "version": version, "hashes": hashes, "revision": CACHE_REVISION})
        self.prepared[build] = target
        return target

    def game_params_source(self, data: Path):
        metadata = tomllib.loads((data / "metadata.toml").read_text("utf-8"))
        h = metadata["files"]["content/GameParams.data"]
        rel = f"common/{h[:2]}/{h[2:]}"
        self.net.download(f"{DATA_URL}/{rel}", data.parent / rel, expected_hash=h)


@dataclass
class Options:
    team: str = "DEMO"
    url: str = DEFAULT_URL
    output: str = ""
    both: bool = False
    trails: bool = True
    download_only: bool = False
    game_dir: str = ""
    proxy: str = ""
    show_bans: bool = True
    trail_mode: str = "all"
    trail_selections: dict = field(default_factory=dict)


def mp4_valid(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 1024:
        return False
    with path.open("rb") as f:
        if f.read(12)[4:8] != b"ftyp":
            return False
        size, position, tags = path.stat().st_size, 0, set()
        while position + 8 <= size:
            f.seek(position)
            length, kind = struct.unpack(">I4s", f.read(8))
            header = 8
            if length == 1:
                if position + 16 > size:
                    return False
                length = struct.unpack(">Q", f.read(8))[0]
                header = 16
            elif length == 0:
                length = size - position
            if length < header or position + length > size:
                return False
            tags.add(kind)
            position += length
        return position == size and {b"moov", b"mdat"}.issubset(tags)


def load_library(source):
    path = Path(source).expanduser().resolve()
    if path.is_dir():
        if path.name == "replays" and not (path / "manifest.json").exists():
            path = path.parent
        path = path / "manifest.json"
    try:
        manifest = json.loads(path.read_text("utf-8"))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("jobs"), list):
            raise ValueError("缺少回放清单")
        seen = set()
        target_tags = {}
        try:
            cached_matches = json.loads((path.parent / "matches.json").read_text("utf-8"))
            for match in cached_matches:
                target_tags[match["id"]] = next((o["signup"].get("teamTag", "") for o in match.get("matchOpponents", [])
                                                   if team_matches(o.get("signup") or {}, manifest.get("team", ""))), "")
        except (OSError, ValueError, KeyError, TypeError):
            pass
        for job in manifest["jobs"]:
            job.setdefault("target_team", target_tags.get(job.get("match_id")) or manifest.get("team", ""))
            key = str(job["report_id"])
            if key in seen:
                raise ValueError("回放编号重复")
            seen.add(key)
            for field_name in ("replay", "video"):
                relative = Path(job[field_name])
                if relative.is_absolute() or not (path.parent / relative).resolve().is_relative_to(path.parent):
                    raise ValueError("清单内的文件路径超出回放目录")
        return path, manifest
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise AppError(f"无法读取回放清单：{path}\n{e}\n请选择下载结果目录中的 manifest.json。") from e


class Runner:
    def __init__(self, options: Options, root=None, cancel=None, emit=None, library=None):
        self.options = options
        self.root = Path(root) if root else app_home()
        self.cancel = cancel or threading.Event()
        self.emit = emit or (lambda kind, data: None)
        self.net = Network(self.cancel, self.log, options.proxy)
        self.tournament = Tournament.parse(options.url)
        self.client = TournamentClient(self.tournament, self.net)
        output = Path(options.output).expanduser() if options.output else self.root / "results"
        self.output = Path(library).resolve() if library else output.resolve() / self.tournament.slug / self.tournament.region / safe_name(options.team.strip().upper())
        self.output.mkdir(parents=True, exist_ok=True)
        self.process = None

    @classmethod
    def from_manifest(cls, source, options=None, **kwargs):
        path, manifest = load_library(source)
        options = replace(options or Options(), team=manifest.get("team", "DEMO"),
                          url=manifest.get("tournament_url", DEFAULT_URL))
        return cls(options, library=path.parent, **kwargs)

    def log(self, message):
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        self.output.mkdir(parents=True, exist_ok=True)
        with (self.output / "run.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        self.emit("log", line)

    def replay_path(self, job):
        return self.output / job["replay"]

    def load_manifest(self):
        return load_library(self.output)

    def search(self):
        matches = self.client.search(self.options.team)
        resolved = getattr(self.client, "resolved_team", self.options.team)
        if isinstance(resolved, str) and resolved != self.options.team:
            self.options = replace(self.options, team=resolved)
            self.output = self.output.parent / safe_name(resolved.strip().upper())
            self.output.mkdir(parents=True, exist_ok=True)
        self.emit("matches", matches)
        self.log(f"找到 {len(matches)} 场属于 {self.options.team} 的比赛。")
        return matches

    def run_process(self, command, log_path: Path):
        self.net.check()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with log_path.open("w", encoding="utf-8") as f:
            self.process = subprocess.Popen([str(x) for x in command], stdout=f, stderr=subprocess.STDOUT, creationflags=flags)
            try:
                while self.process.poll() is None:
                    if self.cancel.wait(0.2):
                        self.process.terminate()
                        try:
                            self.process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            self.process.kill()
                            self.process.wait()
                        raise Cancelled()
                if self.process.returncode:
                    tail = log_path.read_text(encoding="utf-8", errors="replace")[-1800:]
                    raise AppError(f"转换工具退出码 {self.process.returncode}，详情见 {log_path.name}\n{tail}")
            finally:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
                self.process = None

    def download(self, matches=None):
        """Discover and download REP files; never initialize a renderer."""
        self.emit("phase", "download")
        matches = self.search() if matches is None else matches
        if not matches:
            raise AppError(f"未找到名称或队标包含「{self.options.team}」的队伍，请缩短或更换关键词。")
        write_json(self.output / "matches.json", matches)
        signup_id = getattr(self, "target_signup_id", None)
        if signup_id is None:
            signup_id = getattr(self.client, "resolved_signup_id", None)
        jobs, missing = self.client.discover(matches, self.options.team, self.options.both,
                                             **({"signup_id": signup_id} if isinstance(signup_id, int) else {}))
        manifest_path = self.output / "manifest.json"
        old_manifest = {}
        if manifest_path.exists():
            _, old_manifest = load_library(manifest_path)
        previous = {str(j["report_id"]): j for j in old_manifest.get("jobs", [])}
        old_drafts = {}
        try:
            old_drafts = {str(d["match_id"]): d for d in json.loads((self.output / "ban-pick.json").read_text("utf-8"))}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        old_drafts.update({str(k): v for k, v in self.client.draft_data.items()})
        write_json(self.output / "ban-pick.json", list(old_drafts.values()))
        current = []
        for discovered in jobs:
            key = str(discovered["report_id"])
            old = previous.get(key, {})
            stem = safe_name(f"{discovered['match_id']}_{'_vs_'.join(discovered['opponents'])}_G{discovered['game_number']}_{discovered['team']}_R{discovered['report_id']}")
            job = {**old, **discovered, "replay": old.get("replay", f"replays/{stem}.wowsreplay"),
                   "video": old.get("video", f"videos/{stem}.mp4"), "download_status": "pending"}
            job.setdefault("status", "pending")
            current.append(job)
        # Changing perspective or downloading one match must retain earlier local files and render history.
        current_ids = {str(j["report_id"]) for j in current}
        library_jobs = current + [j for key, j in previous.items() if key not in current_ids]
        manifest = {**old_manifest, "team": self.options.team, "tournament_url": self.tournament.url,
                    "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "toolkit_version": TOOL_VERSION,
                    "perspectives": "both" if self.options.both else "team", "missing": missing, "jobs": library_jobs}
        write_json(manifest_path, manifest)
        self.emit("jobs", [j.copy() for j in current])
        totals = {"downloaded": 0, "converted": 0, "skipped": 0, "failed": 0, "missing": len(missing)}
        self.log(f"发现 {len(current)} 份可下载回放，最多 3 个连接；下载后请在转换页设置视频。")
        states = {str(j["report_id"]): {"received": 0, "size": 0, "state": "pending"} for j in current}
        progress_lock = threading.Lock()
        def report(job, received=0, size=0, state="downloading"):
            with progress_lock:
                key = str(job["report_id"])
                states[key] = {"received": received, "size": size, "state": state}
                done = sum(s["state"] in ("downloaded", "failed", "cancelled") for s in states.values())
                # A known-size active file contributes its fraction to the overall file-count progress.
                fraction = sum(min(s["received"] / s["size"], 0.99) if s["size"] else 0
                               for s in states.values() if s["state"] == "downloading")
                self.emit("download_progress", {"report_id": job["report_id"], "team": job["team"],
                          "received": received, "size": size, "state": state, "done": done,
                          "total": len(current), "value": done + fraction,
                          "bytes": sum(s["received"] for s in states.values()),
                          "active": {k: dict(v) for k, v in states.items() if v["state"] == "downloading"}})
        def fetch(job):
            report(job)
            replay = self.output / job["replay"]
            self.net.download(job["download_url"], replay, validator=replay_meta,
                              headers=self.tournament.headers, replay_job=job,
                              cache_hash=previous.get(str(job["report_id"]), {}).get("replay_sha256", ""),
                              progress=lambda received, size: report(job, received, size))
            return sha256(replay), replay_meta(replay), replay.stat().st_size
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(fetch, job): job for job in current}
            try:
                for future in as_completed(futures):
                    job = futures[future]
                    try:
                        digest, meta, size = future.result()
                        old = previous.get(str(job["report_id"]), {})
                        unchanged = old.get("replay_sha256") == digest
                        job.update(replay_sha256=digest, game_version=meta["clientVersionFromExe"],
                                   replay_size=size, download_status="downloaded")
                        if not unchanged or job.get("status") not in ("completed", "skipped"):
                            job["status"] = "downloaded"
                        if not unchanged:
                            job.pop("fingerprint", None)
                            job.pop("video_sha256", None)
                        job.pop("error", None)
                        totals["downloaded"] += 1
                        report(job, size, size, "downloaded")
                    except Cancelled:
                        raise
                    except Exception as e:
                        job.update(status="failed", download_status="failed", error=str(e))
                        totals["failed"] += 1
                        report(job, state="failed")
                        self.log(f"下载失败，继续下一份：{e}")
                    self.emit("job", job.copy())
                    write_json(manifest_path, manifest)
            except BaseException:
                self.cancel.set()
                for future, job in futures.items():
                    future.cancel()
                    if job.get("download_status") == "pending":
                        job.update(download_status="cancelled")
                        if job.get("status") not in ("completed", "skipped"):
                            job["status"] = "cancelled"
                        self.emit("job", job.copy())
                write_json(manifest_path, manifest)
                raise
        manifest.update(finished=time.strftime("%Y-%m-%dT%H:%M:%S%z"), download_summary=totals, summary=totals,
                        last_download_report_ids=[j["report_id"] for j in current])
        write_json(manifest_path, manifest)
        self.log(f"下载结束：成功 {totals['downloaded']}，失败 {totals['failed']}；尚无回放 {totals['missing']} 项。")
        self.emit("complete", {**totals, "phase": "download", "output": str(self.output)})
        return manifest

    def convert(self, report_ids=None):
        """Convert an existing local library without querying the tournament or downloading REP."""
        self.emit("phase", "convert")
        manifest_path, manifest = self.load_manifest()
        selected = None if report_ids is None else {str(key) for key in report_ids}
        jobs = [j for j in manifest["jobs"] if selected is None or str(j["report_id"]) in selected]
        if not jobs:
            raise AppError("请先在转换页勾选至少一份回放。")
        if selected is not None and selected - {str(j["report_id"]) for j in jobs}:
            raise AppError("所选回放不在此清单中，请重新读取目录。")
        self.emit("jobs", [j.copy() for j in jobs])
        totals = {"downloaded": 0, "converted": 0, "skipped": 0, "failed": 0,
                  "missing": len(manifest.get("missing", []))}
        tools = Tools(self.root, self.net)
        for index, job in enumerate(jobs, 1):
            old = job.copy()
            try:
                self.net.check()
                replay = self.replay_path(job)
                if not replay.is_file():
                    hint = "重新读取本地文件夹" if getattr(self, "is_local", False) else "返回下载页补充下载"
                    raise AppError(f"本地回放不存在，请{hint}：{replay.name}")
                meta, digest = replay_meta(replay), sha256(replay)
                if old.get("replay_sha256") and old["replay_sha256"] != digest:
                    hint = "重新读取本地文件夹" if getattr(self, "is_local", False) else "返回下载页重新下载"
                    raise AppError(f"回放校验失败，请{hint}：{replay.name}")
                job.update(replay_sha256=digest, game_version=meta["clientVersionFromExe"])
                job.pop("error", None)
                from .video import fingerprint as render_fingerprint
                fingerprint, _, _ = render_fingerprint(self.options, job)
                video = self.output / job["video"]
                if (old.get("fingerprint") == fingerprint and old.get("replay_sha256") == digest
                        and mp4_valid(video) and old.get("video_sha256") == sha256(video)):
                    job["status"] = "skipped"
                    totals["skipped"] += 1
                    self.log(f"[{index}/{len(jobs)}] 视频与当前选项一致，跳过：{video.name}")
                else:
                    self.render_job(job, old, tools, index, len(jobs))
                    totals["converted"] += 1
            except Cancelled:
                job["status"] = "cancelled"
                self.log("转换已停止，已下载 REP 和已完成的视频已保留。")
                raise
            except Exception as e:
                job.update(status="failed", error=str(e))
                totals["failed"] += 1
                self.log(f"该项转换失败，继续下一份：{e}")
            finally:
                self.emit("job", job.copy())
                self.emit("progress", {"done": index, "total": len(jobs), **totals})
                write_json(manifest_path, manifest)
        manifest.update(finished=time.strftime("%Y-%m-%dT%H:%M:%S%z"), summary=totals,
                        conversion_summary={**totals, "report_ids": [j["report_id"] for j in jobs]})
        write_json(manifest_path, manifest)
        self.log(f"转换结束：新转换 {totals['converted']}，已完成跳过 {totals['skipped']}，失败 {totals['failed']}。")
        self.emit("complete", {**totals, "phase": "convert", "output": str(self.output)})
        return manifest

    def render_job(self, job, old, tools, index, total):
        from .video import compose_video, fingerprint as render_fingerprint
        replay, video = self.replay_path(job), self.output / job["video"]
        fingerprint, mode, selected_ids = render_fingerprint(self.options, job)
        legacy_fingerprint = hashlib.sha256(json.dumps({"toolkit": TOOL_VERSION, "assets": CACHE_REVISION,
                            "trails": self.options.trails, "codec": "h264"}, sort_keys=True).encode()).hexdigest()
        renderer = tools.renderer()
        data = None if self.options.game_dir else tools.game_data(replay)
        video.parent.mkdir(parents=True, exist_ok=True)
        temp_video = video.with_name(video.stem + ".partial.mp4")
        native_trails = mode == "all"
        base = self.root / "cache" / "base-videos" / f"{job['replay_sha256']}_{TOOL_VERSION}_{int(native_trails)}.mp4"
        base.parent.mkdir(parents=True, exist_ok=True)
        # Preserve the already-tested v1 videos as base renders.
        if not base.exists() and old.get("fingerprint") == legacy_fingerprint and native_trails == self.options.trails and mp4_valid(video) and old.get("video_sha256") == sha256(video):
            shutil.copy2(video, base)
        native_temp = base.with_name(base.stem + ".partial.mp4")
        command = [renderer, "--game" if self.options.game_dir else "--extracted-dir", self.options.game_dir or data.parent,
                   "--cpu", "--codec", "h264", "--no-progress", "--no-stats-panel", "--recreate-game-params",
                   "--game-params-cache", self.root / "cache" / "rebuilt", "-o", native_temp]
        if native_trails:
            command += ["--show-trails"]
        command += [replay]
        job["status"] = "converting"
        self.emit("job", job.copy())
        self.log(f"[{index}/{total}] 生成小地图视频：{video.name}")
        render_log = self.output / "logs" / (video.stem + ".log")
        if not mp4_valid(base):
            try:
                self.run_process(command, render_log)
            except AppError:
                if data and "Failed to load GameParams from VFS" in render_log.read_text("utf-8", errors="replace"):
                    self.log("上游参数缓存不兼容，下载原始游戏参数并重新生成本地缓存。")
                    tools.game_params_source(data)
                    self.run_process(command, render_log)
                else:
                    raise
            if not mp4_valid(native_temp):
                raise AppError("未生成完整的小地图基础视频。")
            native_temp.replace(base)
        if self.options.show_bans or mode == "selected":
            if mode == "selected" and data is None:
                data = tools.game_data(replay)
            content = "BAN 信息与舰船轨迹" if self.options.show_bans else "指定舰船轨迹"
            self.log(f"[{index}/{total}] 合成{content}（模式：{mode}）。")
            compose_video(self, base, temp_video, job, data, mode, selected_ids)
            recipe = temp_video.with_suffix(".render.json")
            if recipe.exists():
                recipe.replace(video.with_suffix(".render.json"))
        else:
            shutil.copy2(base, temp_video)
        if not mp4_valid(temp_video):
            raise AppError("渲染器没有生成完整的 MP4 文件，请查看转换日志。")
        temp_video.replace(video)
        if not self.options.show_bans and mode != "selected":
            video.with_suffix(".render.json").unlink(missing_ok=True)
        job.update({"status": "completed", "fingerprint": fingerprint, "video_sha256": sha256(video)})

    def run(self, matches=None):
        """Compatibility entrypoint for the original combined CLI workflow."""
        manifest = self.download(matches)
        if self.options.download_only:
            return manifest
        download_summary = manifest["download_summary"]
        available = [j["report_id"] for j in manifest["jobs"] if j.get("download_status") == "downloaded"
                     and j["report_id"] in manifest["last_download_report_ids"]]
        if not available:
            return manifest
        result = self.convert(available)
        result["summary"]["downloaded"] = download_summary["downloaded"]
        result["summary"]["failed"] += download_summary["failed"]
        write_json(self.output / "manifest.json", result)
        return result
