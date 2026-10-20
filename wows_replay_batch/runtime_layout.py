"""Files owned by the portable application; user data is never an update target."""
import re

RUNTIME_ROOT_FILES = ("WoWSReplayBatch.exe", "使用说明.md", "THIRD_PARTY_NOTICES.md", "LICENSE", "docs/RELEASING.md")
TOOL_FILES = {
    "wows-toolkit": ("minimap_renderer_cpu.exe", "minimap_renderer.exe", "replayshark.exe", "LICENSE.txt", "version.json"),
    "ffmpeg": ("ffmpeg.exe", "BUILD.txt", "COPYING.GPLv3", "FFmpeg-LICENSE.md", "SOURCE.txt"),
}


def application_file(name):
    if name in RUNTIME_ROOT_FILES:
        return True
    parts = name.split("/")
    if len(parts) == 2 and parts[0] == "licenses":
        return bool(re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:txt|md)", parts[1]))
    if len(parts) == 3 and parts[0] == "tools":
        return parts[2] in TOOL_FILES.get(parts[1], ())
    if parts[:2] == ["cache", "game-data"]:
        if len(parts) == 5 and parts[2] == "common":
            return bool(re.fullmatch(r"[0-9a-f]{2}", parts[3]) and re.fullmatch(r"[0-9a-f]{18}", parts[4]))
        if len(parts) == 4:
            return bool(re.fullmatch(r"\d+\.\d+\.\d+_\d+", parts[2]) and parts[3] in ("metadata.toml", "constants.json", ".ready.json"))
    return False
