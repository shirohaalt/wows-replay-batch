import io
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import zipfile

import requests

from wows_replay_batch.core import AppError, Cancelled, Network, replay_meta
from wows_replay_batch.replay_archives import replay_member, sharepoint_link


NAME = 'swiss-stage-a_swiss-group-a_round-1_sleeping-giant_demo-vs-rival_game-1_demo.wowsreplay'


def replay_bytes():
    metadata = json.dumps({'clientVersionFromExe': '14,10,0,123456', 'playerName': 'Demo'}).encode()
    return b'\x12\x32\x34\x11' + struct.pack('<II', 3, len(metadata)) + metadata + os.urandom(96 * 1024)


def job(report=101, team='DEMO'):
    return {'report_id': report, 'stage': 'Swiss Stage A', 'group': 'Swiss Group A', 'round': 'Round 1',
            'map': 'Sleeping Giant', 'opponents': ['DEMO', 'RIVAL'], 'game_number': 1, 'team': team}


class FastCancel(threading.Event):
    def wait(self, timeout=None):
        return self.is_set()


class ArchiveFixture:
    def __init__(self):
        self.replay = replay_bytes()
        self.rival = replay_bytes()
        self.no_range = self.bad_range = False
        self.sent = 0
        self.requests = []
        self.lock = threading.Lock()
        self.set_members([(NAME, self.replay), (NAME.replace('_demo.wowsreplay', '_rival.wowsreplay'), self.rival)])
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                with fixture.lock:
                    fixture.requests.append((parsed.path, query, dict(self.headers)))
                code, headers, body = 200, {}, b''
                if parsed.path == '/api/matches/report/replay':
                    if query.get('id') == ['102']:
                        body, headers = fixture.replay, {'Content-Type': 'application/octet-stream'}
                    else:
                        code, headers = 302, {'Location': 'https://archive.example.sharepoint.com/share/fixture?e=fixture&web=1'}
                elif parsed.path == '/share/fixture':
                    if query.get('download') == ['1']:
                        code, headers = 302, {'Location': 'https://archive.example.sharepoint.com/archive.zip'}
                    else:
                        body, headers = b'<html>OneDrive archive preview</html>', {'Content-Type': 'text/html'}
                elif parsed.path == '/archive.zip':
                    body = fixture.archive
                    headers = {'Content-Type': 'application/zip', 'ETag': '"fixture"'}
                    match = re.fullmatch(r'bytes=(\d+)-(\d+)', self.headers.get('Range', ''))
                    if match and not fixture.no_range:
                        start, end = map(int, match.groups())
                        code = 206
                        headers['Content-Range'] = f'bytes {start}-{end}/{len(body)}'
                        if fixture.bad_range and start != end:
                            headers['Content-Range'] = f'bytes 0-{end}/{len(body)}'
                        body = body[start:end + 1]
                else:
                    code = 404
                self.send_response(code)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                    with fixture.lock:
                        fixture.sent += len(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
            def log_message(self, *_):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.address = 'http://127.0.0.1:' + str(self.server.server_port)

    def set_members(self, entries):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('unselected-padding.bin', b'x' * (4 * 1024 ** 2))
            for name, content in entries:
                archive.writestr('Swiss Stage A/Swiss Group A/' + name, content, compress_type=zipfile.ZIP_DEFLATED)
        self.archive = data.getvalue()

    def network(self):
        fixture = self
        class RouteAdapter(requests.adapters.HTTPAdapter):
            def send(self, request, **kwargs):
                logical = request.url
                parsed = urlsplit(logical)
                request = request.copy()
                request.url = fixture.address + parsed.path + ('?' + parsed.query if parsed.query else '')
                kwargs['proxies'] = {}
                response = super().send(request, **kwargs)
                response.url = logical
                return response
        class RoutedNetwork(Network):
            @property
            def session(self):
                value = super().session
                if not getattr(value, 'routed', False):
                    value.mount('https://', RouteAdapter())
                    value.trust_env = False
                    value.routed = True
                return value
        return RoutedNetwork(cancel=FastCancel())

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class ArchivedReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = ArchiveFixture()
        self.addCleanup(self.fixture.close)
        self.net = self.fixture.network()

    def download(self, report=101, team='DEMO', **kwargs):
        path = self.root / f'{report}.wowsreplay'
        self.net.download(f'https://wows-tournaments.com/api/matches/report/replay?id={report}', path,
                          validator=replay_meta, replay_job=job(report, team), **kwargs)
        return path

    def test_archive_preview_redirect_extracts_only_the_matching_replay_with_ranges(self):
        path = self.download()
        self.assertEqual(path.read_bytes(), self.fixture.replay)
        self.assertLess(self.fixture.sent, len(self.fixture.archive) // 2)
        self.assertEqual(len(self.net.replay_archives), 1)
        self.assertEqual(list(self.root.iterdir()), [path])
        self.assertTrue(any(query.get('e') == ['fixture'] and query.get('download') == ['1'] and 'web' not in query
                            for _, query, _ in self.fixture.requests))

    def test_parallel_perspectives_reuse_the_archive_and_never_mix_replays(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            own = pool.submit(self.download, 101, 'DEMO')
            other = pool.submit(self.download, 103, 'RIVAL')
            self.assertEqual(own.result().read_bytes(), self.fixture.replay)
            self.assertEqual(other.result().read_bytes(), self.fixture.rival)
        self.assertEqual(len(self.net.replay_archives), 1)
        probes = [headers for path, _, headers in self.fixture.requests if path == '/archive.zip' and headers.get('Range') == 'bytes=0-0']
        self.assertEqual(len(probes), 1)

    def test_current_direct_replays_still_download_and_receive_tournament_headers(self):
        self.assertEqual(self.download(102, headers={'x-tournament-name': 'demo-cup', 'x-tournament-region': 'INT'}).read_bytes(), self.fixture.replay)
        self.assertFalse(self.net.replay_archives)
        self.assertEqual(len(self.fixture.requests), 1)
        self.assertEqual(self.fixture.requests[0][2]['x-tournament-region'], 'INT')

    def test_progress_reports_the_extracted_replay_size_and_cached_file_is_reused(self):
        updates = []
        path = self.download(progress=lambda done, total: updates.append((done, total)))
        self.assertEqual(updates[0], (0, len(self.fixture.replay)))
        self.assertEqual(updates[-1], (len(self.fixture.replay), len(self.fixture.replay)))
        before = len(self.fixture.requests)
        self.download()
        self.assertEqual(before, len(self.fixture.requests))
        self.assertTrue(path.is_file())

    def test_unknown_or_ambiguous_member_does_not_save_the_wrong_game(self):
        for entries in ([(NAME.replace('game-1', 'game-2'), self.fixture.replay)],
                        [(NAME, self.fixture.replay), ('duplicate/' + NAME, self.fixture.rival)]):
            self.fixture.set_members(entries)
            self.net.replay_archives.clear()
            with self.assertRaisesRegex(AppError, '唯一对应'):
                self.download()
            self.assertFalse(list(self.root.iterdir()))

    def test_range_unavailable_returns_a_useful_error_and_preserves_existing_file(self):
        self.fixture.no_range = True
        old = self.root / '101.wowsreplay'
        old.write_bytes(b'previous file')
        with self.assertRaisesRegex(AppError, '分段下载'):
            self.download()
        self.assertEqual(old.read_bytes(), b'previous file')
        self.assertFalse(list(self.root.glob('*.part')))

    def test_mismatched_range_is_rejected_without_creating_a_replay(self):
        self.fixture.bad_range = True
        with self.assertRaisesRegex(AppError, '数据分段'):
            self.download()
        self.assertFalse(list(self.root.iterdir()))

    def test_cancellation_during_extraction_cleans_the_partial_file(self):
        def progress(done, _):
            if done:
                self.net.cancel.set()
        with self.assertRaises(Cancelled):
            self.download(progress=progress)
        self.assertFalse(list(self.root.iterdir()))

    def test_invalid_replay_inside_valid_zip_still_fails_validation(self):
        self.fixture.set_members([(NAME, b'<html>not a replay</html>')])
        with self.assertRaisesRegex(AppError, '有效的 .wowsreplay'):
            self.download()
        self.assertFalse(list(self.root.iterdir()))

    def test_corrupt_compressed_member_cannot_be_committed_as_a_replay(self):
        with zipfile.ZipFile(io.BytesIO(self.fixture.archive)) as archive:
            info = replay_member(archive, job())
            offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
        damaged = bytearray(self.fixture.archive)
        damaged[offset + info.compress_size // 2] ^= 0xFF
        self.fixture.archive = bytes(damaged)
        with self.assertRaisesRegex(AppError, '完整性校验'):
            self.download()
        self.assertFalse(list(self.root.iterdir()))

    def test_opponent_order_and_punctuation_normalization_keep_game_and_perspective(self):
        with zipfile.ZipFile(io.BytesIO(self.fixture.archive)) as archive:
            metadata = {**job(), 'opponents': ['RIVAL', 'DEMO'], 'map': 'Sleeping-Giant'}
            self.assertTrue(replay_member(archive, metadata).filename.endswith(NAME))
            metadata['game_number'] = 2
            with self.assertRaises(AppError):
                replay_member(archive, metadata)

    def test_unrelated_or_insecure_hosts_are_never_rewritten_as_sharepoint(self):
        for url in ('https://example.sharepoint.com.evil.invalid/file', 'http://example.sharepoint.com/file',
                    'https://user@example.sharepoint.com/file'):
            response = requests.Response()
            response.url, response.history = url, []
            self.assertIsNone(sharepoint_link(response))
