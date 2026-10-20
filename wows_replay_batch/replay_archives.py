"""Read selected replays from the official site's public SharePoint ZIP archives."""
from collections import OrderedDict
from contextlib import contextmanager
import io
from pathlib import PurePosixPath
import re
import stat
import unicodedata
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
import zipfile
import zlib

from .core import AppError

BLOCK_SIZE = 1024 ** 2
MAX_REPLAY = 128 * 1024 ** 2


def sharepoint_link(response):
    for item in [*response.history, response]:
        for url in (urljoin(item.url, item.headers.get("Location", "")), item.url):
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            if parsed.scheme == "https" and host.endswith(".sharepoint.com") and parsed.netloc == host:
                query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.casefold() not in ("download", "web")]
                query.append(("download", "1"))
                return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
    return None


class RangeFile(io.RawIOBase):
    """Seek through a remote ZIP with bounded memory and checked byte ranges."""
    def __init__(self, network, url):
        self.net, self.url, self.position = network, url, 0
        self.blocks = OrderedDict()
        with network.request("GET", url, stream=True, headers={"Range": "bytes=0-0", "Accept-Encoding": "identity"}) as response:
            match = re.fullmatch(r"bytes 0-0/(\d+)", response.headers.get("Content-Range", ""))
            if response.status_code != 206 or not match:
                raise AppError("历史回放已归档，但网盘未允许分段下载。链接可能已过期或需要登录，请打开原站记录检查。")
            self.size = int(match[1])
            if not 22 <= self.size <= 16 * 1024 ** 3:
                raise AppError("历史回放归档大小无效。")
            self.url = response.url
            self.etag = response.headers.get("ETag", "")

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        if whence not in (0, 1, 2):
            raise ValueError("Invalid seek mode")
        position = offset if whence == 0 else self.position + offset if whence == 1 else self.size + offset
        if position < 0:
            raise ValueError("Negative ZIP offset")
        self.position = position
        return position

    def read(self, size=-1):
        self.net.check()
        size = min(self.size - self.position, size if size >= 0 else self.size)
        if size <= 0:
            return b""
        if size > 16 * 1024 ** 2:
            raise AppError("历史回放归档索引过大，已停止读取。")
        output = bytearray()
        while len(output) < size:
            self.net.check()
            block = self.position // BLOCK_SIZE
            if block not in self.blocks:
                start, end = block * BLOCK_SIZE, min((block + 1) * BLOCK_SIZE, self.size) - 1
                headers = {"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}
                if self.etag and not self.etag.startswith("W/"):
                    headers["If-Range"] = self.etag
                with self.net.request("GET", self.url, stream=True, headers=headers) as response:
                    expected = f"bytes {start}-{end}/{self.size}"
                    if response.status_code != 206 or response.headers.get("Content-Range") != expected:
                        raise AppError("网盘未返回所需的数据分段，或归档已变化，请重新下载。")
                    data = bytearray()
                    for chunk in response.iter_content(64 * 1024):
                        self.net.check()
                        data.extend(chunk)
                        if len(data) > end - start + 1:
                            raise AppError("归档分段大小不一致。")
                    if len(data) != end - start + 1:
                        raise AppError("归档分段传输不完整。")
                self.blocks[block] = bytes(data)
                if len(self.blocks) > 8:
                    self.blocks.popitem(last=False)
            self.blocks.move_to_end(block)
            data = self.blocks[block]
            offset = self.position % BLOCK_SIZE
            count = min(size - len(output), len(data) - offset)
            output.extend(data[offset:offset + count])
            self.position += count
        return bytes(output)


def normalized(value):
    return "".join(c for c in unicodedata.normalize("NFKC", str(value)).casefold() if c.isalnum())


def replay_member(archive, job):
    opponents = job.get("opponents") or []
    if len(opponents) != 2 or not job.get("team") or not job.get("game_number"):
        raise AppError("此回放缺少归档定位信息，请重新查询比赛后下载。")
    prefix = "_".join(str(job.get(key, "")) for key in ("stage", "group", "round", "map"))
    suffix = f"_game-{job['game_number']}_{job['team']}.wowsreplay"
    expected = {normalized(prefix + "_" + "-vs-".join(order) + suffix) for order in (opponents, opponents[::-1])}
    found = [item for item in archive.infolist() if not item.is_dir() and item.filename.lower().endswith(".wowsreplay")
             and normalized(PurePosixPath(item.filename.replace("\\", "/")).name) in expected]
    if len(found) != 1:
        raise AppError("在历史赛区归档中未找到唯一对应的比赛、对局和视角。请打开原站记录检查，或重新查询后重试。")
    item = found[0]
    path = PurePosixPath(item.filename.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or ":" in item.filename or stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1:
        raise AppError("归档回放文件路径或格式无效。")
    if not 12 <= item.file_size <= MAX_REPLAY or item.compress_size > MAX_REPLAY:
        raise AppError("归档回放文件大小无效。")
    return item


class ReplayStream:
    def __init__(self, stream, info):
        self.stream = stream
        self.headers = {"Content-Length": str(info.file_size)}

    def iter_content(self, size):
        while chunk := self.stream.read(size):
            yield chunk


@contextmanager
def replay_response(network, response, job):
    url = sharepoint_link(response)
    if url is None:
        yield response
        return
    response.close()
    try:
        with network.archive_lock:
            archive = network.replay_archives.get(url)
            if archive is None:
                archive = zipfile.ZipFile(RangeFile(network, url))
                if len(archive.infolist()) > 50000:
                    archive.close()
                    raise AppError("历史回放归档文件数量异常。")
                network.replay_archives[url] = archive
                if len(network.replay_archives) > 4:
                    network.replay_archives.pop(next(iter(network.replay_archives)))
        info = replay_member(archive, job)
        with archive.open(info) as stream:
            yield ReplayStream(stream, info)
    except (zipfile.BadZipFile, RuntimeError, zlib.error, AppError) as error:
        with network.archive_lock:
            network.replay_archives.pop(url, None)
        if isinstance(error, AppError):
            raise
        raise AppError("历史回放归档读取或完整性校验失败，请重试。") from error
