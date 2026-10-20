"""Package only redistributable files, regardless of local application history."""
import os
import hashlib
import re
import zipfile
from pathlib import Path
from wows_replay_batch import __version__
from wows_replay_batch.runtime_layout import RUNTIME_ROOT_FILES, TOOL_FILES

ROOT = Path(__file__).resolve().parent
DEST = ROOT / "release" / f"WoWSReplayBatch-v{__version__}"
SOURCE_FILES = ("main.py", "requirements.txt", "build_release.py", "release_package.py", "verify_results.py", "README.md", "THIRD_PARTY_NOTICES.md", "LICENSE", ".gitignore", "docs/RELEASING.md", ".github/workflows/tests.yml", ".github/workflows/live-replay-check.yml")


def public_game_files(root):
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        parts = path.relative_to(root).parts
        if len(parts) == 3 and parts[0] == "common" and re.fullmatch(r"[0-9a-f]{2}", parts[1]) and re.fullmatch(r"[0-9a-f]{18}", parts[2]):
            yield path
        elif len(parts) == 2 and re.fullmatch(r"\d+\.\d+\.\d+_\d+", parts[0]) and parts[1] in ("metadata.toml", "constants.json", ".ready.json"):
            yield path


def runtime_files(root):
    files = [root / name for name in RUNTIME_ROOT_FILES]
    files.extend(path for path in (root / "licenses").glob("*") if path.suffix.lower() in (".txt", ".md"))
    for folder, names in TOOL_FILES.items():
        files.extend(root / "tools" / folder / name for name in names)
    files.extend(public_game_files(root / "cache" / "game-data"))
    return sorted(path for path in files if path.is_file() and not path.is_symlink())


def verify_no_local_paths(files):
    private_roots = {str(Path.home()), str(ROOT), os.environ.get("USERPROFILE", "")}
    markers = set()
    for root in private_roots - {""}:
        for value in (root, root.replace("\\", "/"), root.replace("\\", "\\\\")):
            markers.add(value.casefold().encode("utf-8"))
            markers.add(value.casefold().encode("utf-16-le"))
    for path in files:
        data = path.read_bytes().lower()
        if any(marker in data for marker in markers):
            raise ValueError(f"发布文件含有本机路径，请清理后重试：{path.name}")


def write_archive(archive, files, base):
    files = list(files)
    verify_no_local_paths(files)
    temp = archive.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as output:
            for path in files:
                output.write(path, path.relative_to(base))
        with zipfile.ZipFile(temp) as check:
            if check.testzip() is not None:
                raise ValueError("发布包完整性校验失败")
        temp.replace(archive)
    finally:
        temp.unlink(missing_ok=True)


def main():
    if not (DEST / "WoWSReplayBatch.exe").is_file():
        raise SystemExit("请先运行 python build_release.py")
    windows = ROOT / "release" / f"{DEST.name}-Windows.zip"
    write_archive(windows, runtime_files(DEST), DEST.parent)
    sources = [ROOT / name for name in SOURCE_FILES]
    sources += sorted(ROOT.glob("wows_replay_batch/*.py")) + sorted(ROOT.glob("tests/*.py"))
    source = ROOT / "release" / f"{DEST.name}-source.zip"
    write_archive(source, sources, ROOT)
    sums = []
    for path in (windows, source):
        with path.open("rb") as file:
            sums.append(f"{hashlib.file_digest(file, 'sha256').hexdigest()}  {path.name}\n")
    (ROOT / "release" / "SHA256SUMS.txt").write_text("".join(sums), encoding="utf-8")
    print("WINDOWS_PACKAGE_READY", windows.name, windows.stat().st_size, flush=True)
    print("SOURCE_PACKAGE_READY", source.name, source.stat().st_size, flush=True)


if __name__ == "__main__":
    main()
