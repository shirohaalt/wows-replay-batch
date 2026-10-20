"""Read public GitHub releases and download a verified portable update package."""
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from urllib.parse import unquote, urlsplit
import zipfile

from .core import AppError, Network

DEFAULT_REPOSITORY = "shirohaalt/wows-replay-batch"


def repository_name(value):
    value = value.strip().rstrip("/")
    if "://" in value:
        url = urlsplit(value)
        if url.scheme != "https" or url.netloc.casefold() != "github.com" or url.query or url.fragment:
            raise AppError("请填写 GitHub 仓库地址或 owner/repository。")
        value = url.path.strip("/")
    if value.endswith(".git"):
        value = value[:-4]
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}", value) or value.split("/")[-1] in (".", ".."):
        raise AppError("仓库格式应为 owner/repository，或对应的 GitHub HTTPS 地址。")
    return value


def version_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"v?\d{1,6}\.\d{1,6}(?:\.\d{1,6})?", value, re.I):
        raise AppError("更新版本格式应为 v主版本.次版本.修订号。")
    values = tuple(int(part) for part in value.lstrip("vV").split("."))
    return values + (0,) * (3 - len(values))


def asset_url(value, repository):
    if not isinstance(value, str):
        raise AppError("更新包下载地址无效。")
    url = urlsplit(value)
    prefix = "/" + repository.casefold() + "/releases/download/"
    if url.scheme != "https" or url.netloc.casefold() != "github.com" or not unquote(url.path).casefold().startswith(prefix) or ".." in PurePosixPath(unquote(url.path)).parts or url.query or url.fragment:
        raise AppError("更新文件必须来自所选 GitHub 仓库的 Release。")
    return value


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    filename: str
    url: str
    size: int
    sha256: str

    def newer_than(self, current):
        return version_key(self.version) > version_key(current)


class UpdateClient:
    def __init__(self, root, *, cancel=None, proxy=""):
        self.root = Path(root)
        self.net = Network(cancel=cancel, proxy=proxy)

    def check(self, repository):
        repository = repository_name(repository)
        try:
            data = self.net.json("GET", f"https://api.github.com/repos/{repository}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json"})
        except AppError as error:
            raise AppError("无法读取 GitHub 正式版本。请确认仓库公开且已发布 Release，或检查网络与代理。\n" + str(error)) from error
        if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
            raise AppError("没有可用的 GitHub 正式版本。")
        version = data.get("tag_name", "")
        key = version_key(version)
        assets = data.get("assets")
        if not isinstance(assets, list):
            raise AppError("GitHub 版本附件格式无效。")
        choices = []
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = asset.get("name", "")
            match = re.fullmatch(r"WoWSReplayBatch-v(\d+\.\d+(?:\.\d+)?)-Windows\.zip", name) if isinstance(name, str) else None
            if match and version_key(match[1]) == key:
                choices.append(asset)
        if len(choices) != 1:
            raise AppError("该版本需要提供一个与版本号对应的 WoWSReplayBatch-v版本号-Windows.zip 附件。")
        package = choices[0]
        url = asset_url(package.get("browser_download_url"), repository)
        size = package.get("size")
        if type(size) is not int or not 0 < size <= 4 * 1024 ** 3:
            raise AppError("更新包大小无效。")
        digest = package.get("digest") or ""
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            sums = next((a for a in assets if isinstance(a, dict) and a.get("name") == "SHA256SUMS.txt"), None)
            if not sums:
                raise AppError("更新包缺少 SHA-256 校验值，请发布者补充 SHA256SUMS.txt。")
            with self.net.request("GET", asset_url(sums.get("browser_download_url"), repository), stream=True) as response:
                content = bytearray()
                for chunk in response.iter_content(4096):
                    self.net.check()
                    content.extend(chunk)
                    if len(content) > 64 * 1024:
                        raise AppError("更新校验文件过大。")
            try:
                lines = content.decode("utf-8-sig").splitlines()
            except UnicodeError as error:
                raise AppError("更新校验文件编码无效。") from error
            hashes = [m[1] for line in lines if (m := re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line)) and m[2] == package["name"]]
            if len(hashes) != 1:
                raise AppError("更新校验文件中没有唯一对应的更新包记录。")
            digest = "sha256:" + hashes[0]
        notes = data.get("body") or "发布者未填写更新说明。"
        if not isinstance(notes, str):
            raise AppError("更新说明格式无效。")
        return Release(version.lstrip("vV"), notes[:40000], package["name"], url, size, digest[7:].lower())

    def download(self, release, progress=None):
        version_key(release.version)
        if not re.fullmatch(r"WoWSReplayBatch-v\d+\.\d+(?:\.\d+)?-Windows\.zip", release.filename):
            raise AppError("更新包文件名无效。")
        if not re.fullmatch(r"[0-9a-f]{64}", release.sha256):
            raise AppError("更新包 SHA-256 校验值无效。")
        destination = self.root / "updates" / release.version / release.filename
        def validate(path):
            if path.stat().st_size != release.size:
                raise AppError("更新包大小与发布信息不一致。")
            try:
                with zipfile.ZipFile(path) as archive:
                    names = archive.namelist()
                    for name in names:
                        item = PurePosixPath(name.replace("\\", "/"))
                        if item.is_absolute() or ".." in item.parts or ":" in name:
                            raise AppError("更新包包含无效的文件路径。")
                    if not any(PurePosixPath(name).name == "WoWSReplayBatch.exe" for name in names) or archive.testzip():
                        raise AppError("更新包内容不完整。")
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
                raise AppError("下载内容不是有效的程序 ZIP 更新包。") from error
        def received(done, total):
            if progress:
                progress(done, total or release.size)
        self.net.download(release.url, destination, expected_hash=release.sha256, validator=validate, progress=received)
        return destination
