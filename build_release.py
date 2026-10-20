"""Build the portable Windows application without changing system configuration."""
import json
import importlib.metadata as metadata
import shutil
import subprocess
import sys
from pathlib import Path
from wows_replay_batch import __version__

ROOT = Path(__file__).resolve().parent
DEST = ROOT / "release" / f"WoWSReplayBatch-v{__version__}"


def main():
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
                    "--name", "WoWSReplayBatch", "--collect-data", "certifi", "--exclude-module", "numpy", "main.py"], cwd=ROOT, check=True)
    DEST.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "dist" / "WoWSReplayBatch.exe", DEST)
    shutil.copy2(ROOT / "README.md", DEST / "使用说明.md")
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", DEST)
    shutil.copy2(ROOT / "LICENSE", DEST)
    (DEST / "docs").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "docs/RELEASING.md", DEST / "docs/RELEASING.md")
    licenses = DEST / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.exists():
        shutil.copy2(python_license, licenses / "Python.txt")
    tk_license = Path(sys.base_prefix) / "tcl" / "tk8.6" / "license.terms"
    if tk_license.exists():
        shutil.copy2(tk_license, licenses / "TclTk.txt")
    for package in ("requests", "urllib3", "certifi", "charset-normalizer", "idna", "cryptography", "cffi", "pyinstaller", "Pillow", "imageio-ffmpeg"):
        try:
            distribution = metadata.distribution(package)
        except metadata.PackageNotFoundError:
            continue
        for item in distribution.files or []:
            if ".dist-info/" in str(item) and any(word in item.name.lower() for word in ("license", "copying", "notice")):
                source = Path(distribution.locate_file(item))
                if source.is_file():
                    shutil.copy2(source, licenses / (package + "_" + item.name))
    toolkit = ROOT / "tools" / "wows-toolkit"
    if toolkit.exists():
        target = DEST / "tools" / "wows-toolkit"
        target.mkdir(parents=True, exist_ok=True)
        for name in ("minimap_renderer_cpu.exe", "minimap_renderer.exe", "replayshark.exe", "LICENSE.txt", "version.json"):
            if (toolkit / name).exists():
                shutil.copy2(toolkit / name, target / name)
    import imageio_ffmpeg
    ffmpeg_target = DEST / "tools" / "ffmpeg"
    ffmpeg_target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), ffmpeg_target / "ffmpeg.exe")
    build_info = subprocess.run([str(ffmpeg_target / "ffmpeg.exe"), "-version"], capture_output=True, text=True, check=True).stdout
    (ffmpeg_target / "BUILD.txt").write_text(build_info, encoding="utf-8")
    for filename in ("COPYING.GPLv3", "FFmpeg-LICENSE.md", "SOURCE.txt"):
        source = ROOT / "tools" / "ffmpeg" / filename
        if source.exists():
            shutil.copy2(source, ffmpeg_target / filename)
    print(DEST / "WoWSReplayBatch.exe")


if __name__ == "__main__":
    main()
