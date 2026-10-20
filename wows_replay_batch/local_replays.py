"""Import raw local replays without a tournament manifest or any tournament requests."""
import hashlib
from dataclasses import replace
from pathlib import Path

from .core import AppError, Cancelled, DEFAULT_URL, Options, Runner, replay_meta, sha256, write_json

OUTPUT_DIRECTORY = "WoWSReplayBatch-output"
LIBRARY_KIND = "local-replays"


def source_path(folder, relative):
    base = Path(folder).resolve()
    relative = Path(relative)
    path = (base / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(base) or path.suffix.casefold() != ".wowsreplay":
        raise AppError("本地回放路径超出所选文件夹，或文件格式无效。")
    return path


def read_local_library(folder):
    folder = Path(folder).resolve()
    output = folder / OUTPUT_DIRECTORY
    manifest_path = output / "manifest.json"
    try:
        import json
        manifest = json.loads(manifest_path.read_text("utf-8"))
        if not isinstance(manifest, dict) or manifest.get("kind") != LIBRARY_KIND or not isinstance(manifest.get("jobs"), list):
            raise ValueError("不是本地回放转换清单")
        seen = set()
        for job in manifest["jobs"]:
            identity = str(job["report_id"])
            if not identity.startswith("local-") or identity in seen:
                raise ValueError("本地回放标识无效或重复")
            seen.add(identity)
            source_path(folder, job["replay"])
            video = Path(job["video"])
            if video.is_absolute() or not (output / video).resolve().is_relative_to(output.resolve()):
                raise ValueError("视频路径超出输出目录")
        return manifest_path, manifest
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise AppError(f"无法读取本地回放转换记录：{error}。请重新读取文件夹。") from error


class LocalRunner(Runner):
    is_local = True

    def __init__(self, folder, options=None, **kwargs):
        if not str(folder).strip():
            raise AppError("请先选择本地回放文件夹。")
        self.source_dir = Path(folder).expanduser().resolve()
        if not self.source_dir.is_dir():
            raise AppError("本地回放文件夹不存在。")
        output = self.source_dir / OUTPUT_DIRECTORY
        if output.is_symlink():
            raise AppError("本地转换输出目录不能是符号链接。")
        if (output / "manifest.json").exists():
            read_local_library(self.source_dir)
        options = replace(options or Options(), team="", url=DEFAULT_URL, show_bans=False)
        super().__init__(options, library=output, **kwargs)

    def replay_path(self, job):
        return source_path(self.source_dir, job["replay"])

    def load_manifest(self):
        return read_local_library(self.source_dir)

    def scan(self):
        previous = {}
        if (self.output / "manifest.json").is_file():
            previous = {str(j["report_id"]): j for j in self.load_manifest()[1]["jobs"]}
        files = sorted((p for p in self.source_dir.rglob("*") if p.is_file() and p.suffix.casefold() == ".wowsreplay"
                        and not p.is_relative_to(self.output)), key=lambda p: p.relative_to(self.source_dir).as_posix().casefold())
        if not files:
            raise AppError("所选文件夹及其子文件夹中没有 .wowsreplay 回放。")
        jobs, invalid = [], []
        for index, path in enumerate(files, 1):
            self.net.check()
            relative = path.relative_to(self.source_dir).as_posix()
            try:
                replay = source_path(self.source_dir, relative)
                meta = replay_meta(replay)
                digest = sha256(replay)
                identity = "local-" + hashlib.sha256((relative + "\0" + digest).encode("utf-8")).hexdigest()[:24]
                old = previous.get(identity, {})
                from .core import safe_name
                stem = safe_name(path.stem)[:65] + "_" + identity[6:18]
                ship = str(meta.get("playerVehicle") or "").split("-", 1)[-1]
                jobs.append({**old, "report_id": identity, "match_id": identity, "game_id": identity, "game_number": 1,
                             "local_file": True, "replay": relative, "video": f"videos/{stem}.mp4",
                             "opponents": [path.name], "team": str(meta.get("playerName") or "未记录玩家"),
                             "map": str(meta.get("mapDisplayName") or meta.get("mapName") or "未记录地图"),
                             "ship": ship, "battle_time": str(meta.get("dateTime") or ""),
                             "game_version": str(meta["clientVersionFromExe"]), "replay_sha256": digest,
                             "replay_size": replay.stat().st_size, "stage": "本地回放", "round": "",
                             "download_status": "downloaded", "status": old.get("status", "downloaded")})
            except (AppError, OSError, ValueError, TypeError) as error:
                detail = str(error).replace("下载内容不是有效的", "文件不是有效的")
                invalid.append({"file": relative, "error": detail})
                self.log(f"跳过无法读取的本地文件：{relative}（{detail}）")
            self.emit("scan_progress", {"done": index, "total": len(files), "valid": len(jobs), "invalid": len(invalid)})
        self.net.check()
        if not jobs:
            raise AppError(f"找到 {len(files)} 个回放文件，但均无法读取；请查看日志。")
        manifest = {"kind": LIBRARY_KIND, "team": "", "tournament_url": DEFAULT_URL, "jobs": jobs,
                    "missing": [], "invalid_files": invalid}
        write_json(self.output / "manifest.json", manifest)
        self.emit("local_scanned", {"folder": str(self.source_dir), "manifest": manifest})
        return manifest
