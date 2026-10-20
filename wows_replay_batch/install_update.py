"""Stage verified updates, replace application files in a separate process, and roll back."""
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
import zipfile

from .core import AppError, Cancelled, write_json as atomic_json
from .runtime_layout import application_file
from .updates import version_key

EXE = "WoWSReplayBatch.exe"


def write_json(path, value):
    # Windows scanners and the status reader can briefly hold an open file.
    deadline = time.monotonic() + 5
    while True:
        try:
            atomic_json(path, value)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.05)


def read_json(path):
    deadline = time.monotonic() + 5
    while True:
        try:
            return json.loads(Path(path).read_text("utf-8"))
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.05)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_path(root, name):
    """Reject junctions as well as links, including existing ancestor directories."""
    parts = PurePosixPath(name).parts
    if not parts or name != "/".join(parts) or "\\" in name or ":" in name or any(p in (".", "..") or p.endswith((" ", ".")) for p in parts) or PurePosixPath(name).is_absolute():
        raise AppError("更新文件路径无效。")
    path = root
    for part in parts:
        if re.search(r'[<>"|?*\x00-\x1f]', part) or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            raise AppError("更新文件名无效。")
        path = path / part
        reparse = os.name == "nt" and path.exists() and bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        if path.is_symlink() or reparse:
            raise AppError("更新目录中存在链接或目录联接，请使用普通文件夹。")
    if not path.resolve().is_relative_to(root.resolve()):
        raise AppError("更新文件超出程序目录。")
    return path


def check_cancel(cancel):
    if cancel and cancel.is_set():
        raise Cancelled()


def stage_update(root, package, release, *, cancel=None, progress=None):
    """No installed file is changed here; cancellation only discards this staging area."""
    root, package = Path(root).resolve(), Path(package).resolve()
    version_key(release.version)
    if not (root / EXE).is_file():
        raise AppError("请在完整解压的 Windows 程序目录内安装更新。")
    check_cancel(cancel)
    if package.stat().st_size != release.size or digest(package) != release.sha256:
        raise AppError("安装前校验失败，请重新下载更新包。")
    updates = safe_path(root, "updates")
    updates.mkdir(exist_ok=True)
    session = updates / ("install-" + uuid.uuid4().hex)
    session.mkdir()
    stage = session / "stage"
    stage.mkdir()
    try:
        with zipfile.ZipFile(package) as archive:
            files, seen = [], set()
            prefix = f"WoWSReplayBatch-v{release.version}/"
            for info in archive.infolist():
                name = info.filename
                if not name.startswith(prefix):
                    raise AppError("更新包目录与版本号不一致。")
                relative = name[len(prefix):]
                if info.is_dir():
                    if relative:
                        safe_path(stage, relative.rstrip("/"))
                    continue
                safe_path(stage, relative)
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or info.flag_bits & 1 or not application_file(relative):
                    raise AppError("更新包含有非程序文件或不支持的内容，安装已停止。")
                key = relative.casefold()
                if key in seen:
                    raise AppError("更新包含有重复文件。")
                seen.add(key)
                if safe_path(root, relative).exists() and not safe_path(root, relative).is_file():
                    raise AppError("更新目标被同名文件夹占用。")
                files.append((relative, info))
            total = sum(info.file_size for _, info in files)
            if EXE.casefold() not in seen or total > 8 * 1024 ** 3 or len(files) > 50000:
                raise AppError("更新包不完整或解压后大小超出限制。")
            if shutil.disk_usage(root).free < total * 2 + 64 * 1024 ** 2:
                raise AppError("程序所在磁盘空间不足，需预留解压和备份空间。")
            records, done = [], 0
            for relative, info in files:
                check_cancel(cancel)
                target = safe_path(stage, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as output:
                    while chunk := source.read(1024 ** 2):
                        check_cancel(cancel)
                        output.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
                records.append({"name": relative, "sha256": digest(target)})
        check_cancel(cancel)
        job = {"root": str(root), "version": release.version, "token": uuid.uuid4().hex, "files": records,
               "parent_pid": os.getpid(), "state": "prepared", "changed": []}
        write_json(session / "job.json", job)
        return session / "job.json"
    except BaseException:
        # This is the freshly created, checked staging directory, never the installation.
        shutil.rmtree(session)
        raise


def load_job(job_path):
    job_path = Path(job_path).absolute()
    job = read_json(job_path)
    root = Path(job["root"]).resolve()
    if job_path.name != "job.json" or not re.fullmatch(r"install-[0-9a-f]{32}", job_path.parent.name):
        raise AppError("安装记录路径无效。")
    expected = safe_path(root, "updates/" + job_path.parent.name + "/job.json")
    if expected != job_path or not re.fullmatch(r"[0-9a-f]{32}", job["token"]):
        raise AppError("安装记录不属于此程序目录。")
    version_key(job["version"])
    names = set()
    for record in job["files"]:
        name = record["name"]
        if not application_file(name) or name.casefold() in names or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"]):
            raise AppError("安装文件清单无效。")
        safe_path(root, name)
        names.add(name.casefold())
    if EXE.casefold() not in names:
        raise AppError("安装记录缺少主程序。")
    return job_path, job


def replace_file(source, target, timeout=20):
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.25)


def rollback(job_path, job):
    root, session = Path(job["root"]), Path(job_path).parent
    errors = []
    for record in reversed(job["changed"]):
        try:
            if not application_file(record["name"]):
                raise AppError("备份文件路径无效。")
            target = safe_path(root, record["name"])
            if record["existed"]:
                backup = safe_path(session / "backup", record["name"])
                if target.is_file() and digest(target) == digest(backup):
                    continue
                temporary = safe_path(session, "restore.tmp")
                shutil.copy2(backup, temporary)
                replace_file(temporary, target)
            else:
                target.unlink(missing_ok=True)
        except Exception as error:
            errors.append(str(error))
    job["state"] = "rollback_failed" if errors else "rolled_back"
    write_json(Path(job_path), job)
    if errors:
        raise AppError("部分程序文件未能恢复。旧文件备份保留在：" + str(session / "backup") + "\n" + "\n".join(errors))


def install_files(job_path):
    job_path, job = load_job(job_path)
    if job["state"] != "prepared":
        raise AppError("此安装记录已处理，请重新开始更新。")
    session, root = job_path.parent, Path(job["root"])
    # Verify all extracted files before touching any existing file.
    for record in job["files"]:
        if digest(safe_path(session / "stage", record["name"])) != record["sha256"]:
            raise AppError("暂存更新文件校验失败，原程序未被修改。")
    job["state"] = "installing"
    write_json(job_path, job)
    try:
        # Replace the executable last, after all supporting files are available.
        for record in sorted(job["files"], key=lambda item: item["name"] == EXE):
            name = record["name"]
            target = safe_path(root, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            existed = target.exists()
            if existed:
                backup = safe_path(session / "backup", name)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
            job["changed"].append({"name": name, "existed": existed})
            write_json(job_path, job)
            replace_file(safe_path(session / "stage", name), target)
        job["state"] = "awaiting_health"
        write_json(job_path, job)
    except BaseException:
        rollback(job_path, job)
        raise
    return job


def independent_environment():
    return {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}


def start_installer(job_path):
    if os.name != "nt" or not getattr(sys, "frozen", False):
        raise AppError("自动安装仅用于 Windows 发布版；源码版请手动更新源码。")
    job_path, job = load_job(job_path)
    if Path(sys.executable).resolve() != Path(job["root"]) / EXE:
        raise AppError("请从原程序目录启动更新。")
    helper_dir = safe_path(job_path.parent, "helper")
    helper_dir.mkdir(exist_ok=True)
    helper = helper_dir / EXE
    try:
        shutil.copy2(sys.executable, helper)
        return subprocess.Popen([str(helper), "--apply-update", str(job_path)], cwd=helper_dir,
                                env=independent_environment(), creationflags=subprocess.CREATE_NO_WINDOW)
    except BaseException:
        shutil.rmtree(helper_dir)
        raise


def mark_healthy(job_path, root):
    path, job = load_job(job_path)
    if Path(job["root"]) != Path(root).resolve() or job["state"] != "awaiting_health":
        raise AppError("更新启动确认无效。")
    write_json(path.parent / "healthy.json", {"token": job["token"]})


def finish_install(job_path, *, restart=None, health_timeout=60):
    """Rollback on both file errors and a new executable that cannot reach its GUI."""
    path, _ = load_job(job_path)
    process = None
    try:
        job = install_files(path)
        command = [str(Path(job["root"]) / EXE), "--update-health", str(path)]
        restart = restart or (lambda args, root: subprocess.Popen(args, cwd=root, env=independent_environment()))
        process = restart(command, job["root"])
        job["restart_pid"] = process.pid
        write_json(path, job)
        deadline = time.monotonic() + health_timeout
        while time.monotonic() < deadline:
            health = path.parent / "healthy.json"
            if health.is_file() and read_json(health).get("token") == job["token"]:
                job["state"] = "completed"
                write_json(path, job)
                return
            if process.poll() is not None:
                raise AppError("新版程序未能正常启动。")
            time.sleep(.2)
        raise AppError("新版程序启动超时。")
    except Exception as error:
        if process and process.poll() is None:
            # Only the child created above is stopped, including its onefile child.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            process.wait(timeout=15)
        job = read_json(path)
        if job["state"] == "awaiting_health":
            rollback(path, job)
        job["error"] = str(error)
        write_json(path, job)
        raise


def helper_main(job_path):
    """The helper runs from a temporary copy, with its own PyInstaller runtime."""
    path, job = load_job(job_path)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    parent = kernel.OpenProcess(0x00100000, False, job["parent_pid"])
    try:
        if not parent:
            raise AppError("无法等待原程序退出，安装未开始。")
        write_json(path.parent / "ready.json", {"token": job["token"]})
        deadline = time.monotonic() + 90
        while not (path.parent / "proceed.json").exists():
            if (path.parent / "abort.json").exists():
                return
            if time.monotonic() >= deadline or kernel.WaitForSingleObject(parent, 0) == 0:
                raise AppError("安装未得到原程序的退出确认，未修改程序文件。")
            time.sleep(.1)
        proceed = read_json(path.parent / "proceed.json")
        if proceed.get("token") != job["token"] or kernel.WaitForSingleObject(parent, 90000) != 0:
            raise AppError("原程序未能退出，安装未开始。")
        finish_install(path)
    except Exception as error:
        write_json(path.parent / "error.json", {"error": str(error)})
        if not (path.parent / "proceed.json").exists():
            return
        # Surface the result even though the original GUI has already exited.
        from tkinter import Tk, messagebox
        window = Tk()
        window.withdraw()
        state = read_json(path).get("state")
        result = "已恢复旧版程序，设置与回放保留。" if state == "rolled_back" else "请保留程序目录及更新备份。"
        messagebox.showerror("自动更新未完成", str(error) + "\n" + result, parent=window)
        window.destroy()
    finally:
        if parent:
            kernel.CloseHandle(parent)
