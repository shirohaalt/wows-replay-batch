"""BAN captions and per-ship trails composed over the upstream minimap video."""
from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import re
import shutil
import subprocess
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .core import AppError, Cancelled, mp4_valid, replay_meta, sha256, write_json

COMPOSITOR_VERSION = 1
COLORS = [(74, 226, 202), (255, 195, 83), (186, 137, 255), (255, 120, 154),
          (103, 187, 255), (199, 234, 113), (255, 157, 91), (240, 143, 255)]


def ffmpeg_path(root: Path):
    portable = root / "tools" / "ffmpeg" / "ffmpeg.exe"
    if portable.is_file():
        return portable
    try:
        return Path(importlib.import_module("imageio_ffmpeg").get_ffmpeg_exe())
    except (ImportError, RuntimeError):
        found = shutil.which("ffmpeg")
        if found:
            return Path(found)
    raise AppError("缺少视频合成工具。请解压完整软件包，或在源码环境安装 imageio-ffmpeg。")


def read_roster(runner, replay: Path, data: Path | None = None):
    from .core import Tools
    tools = Tools(runner.root, runner.net)
    parser = tools.renderer("replayshark.exe")
    digest = sha256(replay)
    cache = runner.root / "cache" / "replay-info" / (digest + ".roster.json")
    if cache.exists():
        return json.loads(cache.read_text("utf-8"))
    if not runner.options.game_dir:
        data = data or tools.game_data(replay)
    output = cache.with_suffix(".query.log")
    command = [parser, "-g" if runner.options.game_dir else "-e", runner.options.game_dir or data,
               "query", "players", "--json", replay]
    runner.run_process(command, output)
    result = []
    for line in output.read_text("utf-8-sig", errors="replace").splitlines():
        if line.startswith("{"):
            item = json.loads(line)
            if "entity_id" in item:
                result.append(item)
    if not result:
        raise AppError("回放没有可读取的舰船名单。")
    write_json(cache, result)
    return result


def map_space_size(data: Path, map_name: str):
    metadata = tomllib.loads((data / "metadata.toml").read_text("utf-8"))
    relative = map_name.rstrip("/") + "/space.settings"
    h = metadata["files"].get(relative)
    if not h:
        raise AppError("未找到该地图的坐标参数，已停止轨迹合成。")
    xml = ET.fromstring((data.parent / "common" / h[:2] / h[2:]).read_bytes())
    bounds = xml.find("bounds")
    if bounds is None:
        raise AppError("地图坐标参数不完整。")
    def number(name):
        value = bounds.get(name) or bounds.findtext(name)
        return int(value.strip())
    x_span = number("maxX") - number("minX") + 1
    z_span = number("maxY") - number("minY") + 1
    chunk = float(xml.findtext(".//chunkSize", "100").strip())
    # Match wows-toolkit's spaces.py formula, including its four border chunks.
    size = (max(x_span, z_span) - 4) * chunk
    if size <= 0:
        raise AppError("地图坐标范围无效。")
    return size


def normalized_pixel(position, space_size, width=768, hud=32):
    # The replay decoder exposes the 11-bit minimap coordinates in this form.
    x = (float(position["x"]) + 1.5) * 512.0 / 2047.0 * 5000.0 - 2500.0
    z = (float(position["y"]) + 1.5) * 512.0 / 2047.0 * 5000.0 - 2500.0
    return ((x / space_size + 0.5) * width, (-z / space_size + 0.5) * width + hud)


def extract_tracks(packet_path, space_size, width=768, hud=32):
    tracks, last_visible = {}, {}
    battle_start = None
    with Path(packet_path).open(encoding="utf-8-sig") as source:
        for line in source:
            if not line.strip() or not line.lstrip().startswith("{"):
                continue
            packet = json.loads(line)
            payload = packet.get("payload")
            if not isinstance(payload, dict):
                continue
            clock = float(packet.get("clock", 0))
            prop = payload.get("EntityProperty")
            if prop and prop.get("property") == "battleStage" and prop.get("value") == 0 and battle_start is None:
                battle_start = clock
            update = payload.get("MinimapUpdate")
            if not update:
                continue
            for item in update.get("updates", []):
                entity = int(item["entity_id"])
                if item.get("is_sentinel") or item.get("disappearing") or not item.get("position"):
                    last_visible.pop(entity, None)
                    continue
                x, y = normalized_pixel(item["position"], space_size, width, hud)
                if not math.isfinite(x + y) or not (0 <= x <= width and hud <= y <= hud + width):
                    last_visible.pop(entity, None)
                    continue
                prior = last_visible.get(entity)
                connect = prior is not None and 0 <= clock - prior <= 5.0
                tracks.setdefault(str(entity), []).append([clock, round(x, 3), round(y, 3), connect])
                last_visible[entity] = clock
    return {"battle_start": battle_start or 0.0, "tracks": tracks}


def replay_tracks(runner, replay, data, width=768, hud=32):
    from .core import Tools
    key = sha256(replay)
    target = runner.root / "cache" / "replay-info" / (key + f".tracks-v1-{width}.json")
    if target.exists():
        return json.loads(target.read_text("utf-8"))
    tools = Tools(runner.root, runner.net)
    parser = tools.renderer("replayshark.exe")
    packets = target.with_suffix(".packets.jsonl")
    runner.run_process([parser, "-g" if runner.options.game_dir else "-e", runner.options.game_dir or data,
                        "dump", "--no-meta", "-o", packets, replay], target.with_suffix(".dump.log"))
    try:
        size = map_space_size(data, replay_meta(replay)["mapName"])
        result = extract_tracks(packets, size, width, hud)
        result["window_duration"] = max(0.0, float(replay_meta(replay)["duration"]) - result["battle_start"])
        if not result["tracks"]:
            raise AppError("该回放没有可用的小地图位置记录，不能生成指定舰船轨迹。")
        write_json(target, result)
        return result
    finally:
        packets.unlink(missing_ok=True)


def font(size):
    for path in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/segoeui.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def wrap_text(draw, text, face, limit):
    lines, line = [], ""
    for char in str(text):
        if line and draw.textlength(line + char, font=face) > limit:
            lines.append(line)
            line = ""
        line += char
    return lines + ([line] if line else [])


def footer_panel(width, draft, show_bans, selected_roster, mode):
    probe = Image.new("RGB", (width, 2))
    draw = ImageDraw.Draw(probe)
    heading, normal, small = font(19), font(17), font(14)
    teams = (draft or {}).get("teams", [])
    sections, max_lines = [], 1
    if show_bans:
        for team in teams:
            names = list(dict.fromkeys(team.get("bans", [])))
            if names:
                text = " / ".join(names)
            elif not (draft or {}).get("enabled"):
                text = "本阶段未启用禁船"
            elif (draft or {}).get("error"):
                text = "BAN 信息暂不可用，请重试查询"
            else:
                text = "尚无公开禁船记录"
            lines = wrap_text(draw, text, normal, (width - 72) / 2)
            max_lines = max(max_lines, len(lines))
            sections.append((team.get("tag", "队伍"), lines))
    ban_height = (52 + max_lines * 27 + 14) if show_bans else 0
    entries = []
    if mode == "selected":
        for index, row in enumerate(selected_roster):
            label = f"{row['player_name']} · {row.get('ship_name', '')}"
            lines = wrap_text(draw, label, small, (width - 86) / 2)
            entries.append((COLORS[index % len(COLORS)], lines))
    legend_rows = (len(entries) + 1) // 2
    row_heights = [max(len(entries[i * 2][1]), len(entries[i * 2 + 1][1]) if i * 2 + 1 < len(entries) else 1) * 21 + 8 for i in range(legend_rows)]
    legend_height = 34 + sum(row_heights)
    height = ban_height + legend_height + 12
    height += height % 2
    panel = Image.new("RGB", (width, height), "#111e30")
    draw = ImageDraw.Draw(panel)
    draw.line((0, 0, width, 0), fill="#3b536f", width=2)
    for index, (tag, lines) in enumerate(sections[:2]):
        x = 20 + index * width // 2
        draw.text((x, 14), f"{tag} 提交的 BAN", font=heading, fill="#69e3c5" if index == 0 else "#ffb79a")
        for n, line in enumerate(lines):
            draw.text((x, 47 + n * 27), line, font=normal, fill="#e5edf8")
    y = ban_height + 8
    title = {"all": "轨迹：全部舰船", "none": "轨迹：关闭", "selected": f"轨迹：已选择 {len(entries)} 条舰船"}[mode]
    if mode == "selected" and not entries:
        title += "（本局未勾选，不显示轨迹）"
    draw.text((20, y), title, font=small, fill="#a6b7cf")
    y += 28
    for row, row_height in enumerate(row_heights):
        for col in range(2):
            index = row * 2 + col
            if index >= len(entries):
                continue
            color, lines = entries[index]
            x = 20 + col * width // 2
            draw.rectangle((x, y + 5, x + 9, y + 14), fill=color)
            for n, line in enumerate(lines):
                draw.text((x + 16, y + n * 21), line, font=small, fill="#dce6f6")
        y += row_height
    return panel


def probe_video(executable, video):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run([str(executable), "-hide_banner", "-i", str(video)], capture_output=True, creationflags=flags, timeout=30)
    text = result.stderr.decode("utf-8", errors="replace")
    dimensions = re.search(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b", text)
    fps = re.search(r"\b([\d.]+) fps", text)
    start = re.search(r"start:\s*([\d.-]+)", text)
    if not dimensions or not fps:
        raise AppError("无法读取视频尺寸或帧率。")
    return int(dimensions[1]), int(dimensions[2]), float(fps[1]), max(0.0, float(start[1])) if start else 0.0


def compose_video(runner, base, output, job, data, mode, selected_ids):
    executable = ffmpeg_path(runner.root)
    width, height, fps, offset = probe_video(executable, base)
    if width != 768 or height != 800:
        raise AppError(f"当前视频合成需要标准 768×800 小地图，实际为 {width}×{height}。")
    roster, track_data = [], None
    if mode == "selected" and selected_ids:
        roster = read_roster(runner, runner.replay_path(job), data)
        roster = [r for r in roster if int(r["entity_id"]) in selected_ids]
        if len(roster) != len(selected_ids):
            raise AppError("所选舰船不属于此回放，请重新打开「选择舰船」检查名单。")
        track_data = replay_tracks(runner, runner.replay_path(job), data)
    panel = footer_panel(width, job.get("draft"), runner.options.show_bans, roster, mode)
    output.parent.mkdir(parents=True, exist_ok=True)
    trail_image = Image.new("RGBA", (width, height))
    trail_draw = ImageDraw.Draw(trail_image)
    cursor = {str(r["entity_id"]): 0 for r in roster}
    colors = {str(r["entity_id"]): COLORS[n % len(COLORS)] for n, r in enumerate(roster)}
    log_path = runner.output / "logs" / (output.stem + ".compose.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    decoder = encoder = None
    size = width * height * 3
    frames = 0
    try:
        with log_path.open("wb") as log:
            decoder = subprocess.Popen([str(executable), "-v", "error", "-i", str(base), "-map", "0:v:0", "-vsync", "0",
                                        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], stdout=subprocess.PIPE, stderr=log, creationflags=flags)
            encoder = subprocess.Popen([str(executable), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                                        "-s", f"{width}x{height + panel.height}", "-r", str(fps), "-i", "pipe:0", "-an",
                                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                                        "-movflags", "+faststart", str(output)], stdin=subprocess.PIPE, stdout=log, stderr=log, creationflags=flags)
            while True:
                runner.net.check()
                raw = decoder.stdout.read(size)
                if not raw:
                    break
                if len(raw) != size:
                    raise AppError("视频解码提前结束，未得到完整画面。")
                frame = Image.frombytes("RGB", (width, height), raw)
                if track_data:
                    clock = track_data["battle_start"] + (frames / fps + offset) * track_data["window_duration"] / 60.0
                    for entity, index in list(cursor.items()):
                        points = track_data["tracks"].get(entity, [])
                        while index < len(points) and points[index][0] <= clock:
                            point = points[index]
                            if index > 0 and point[3] and points[index - 1][0] >= track_data["battle_start"]:
                                previous = points[index - 1]
                                trail_draw.line((previous[1], previous[2], point[1], point[2]), fill=(*colors[entity], 205), width=3)
                            index += 1
                        cursor[entity] = index
                    frame.paste(trail_image, (0, 0), trail_image)
                canvas = Image.new("RGB", (width, height + panel.height))
                canvas.paste(frame, (0, 0))
                canvas.paste(panel, (0, height))
                encoder.stdin.write(canvas.tobytes())
                frames += 1
                if frames % 150 == 0:
                    runner.emit("compose_progress", {"frames": frames})
            encoder.stdin.close()
            encoder.stdin = None
            decoder.stdout.close()
            decoder.wait(timeout=30)
            encoder.wait(timeout=60)
            if decoder.returncode or encoder.returncode or not frames or not mp4_valid(output):
                raise AppError("BAN 信息或指定舰船轨迹合成失败，请查看 compose.log。")
    except BrokenPipeError as e:
        raise AppError("视频合成工具提前退出，请查看 compose.log。") from e
    finally:
        for process in (decoder, encoder):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process:
                for pipe in (process.stdin, process.stdout):
                    if pipe:
                        try:
                            pipe.close()
                        except OSError:
                            pass
    write_json(output.with_suffix(".render.json"), {"mode": mode, "selected_ids": sorted(selected_ids),
               "selected_ships": roster, "draft": job.get("draft"), "show_bans": runner.options.show_bans, "frames": frames,
               "width": width, "height": height + panel.height, "tracks": list(cursor), "version": COMPOSITOR_VERSION})


def fingerprint(options, job):
    mode = options.trail_mode if options.trails else "none"
    ids = sorted(set(int(x) for x in options.trail_selections.get(str(job["report_id"]), []))) if mode == "selected" else []
    fields = {"compositor": COMPOSITOR_VERSION, "mode": mode, "ids": ids, "show_bans": options.show_bans,
              "draft": job.get("draft") if options.show_bans else None}
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(), mode, ids
