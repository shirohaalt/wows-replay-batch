import copy
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from wows_replay_batch.core import AppError, Cancelled
from wows_replay_batch.updates import UpdateClient, repository_name, version_key

REPO = 'example/project'
FILENAME = 'WoWSReplayBatch-v1.4.0-Windows.zip'


class RouteAdapter(requests.adapters.HTTPAdapter):
    """Route test GitHub requests through a real local HTTP server."""
    def __init__(self, address):
        super().__init__()
        self.address = address

    def send(self, request, **kwargs):
        request = request.copy()
        request.url = self.address + ('/api' if request.url.startswith('https://api.github.com/') else '/sums' if request.url.endswith('SHA256SUMS.txt') else '/file')
        kwargs['proxies'] = {}
        return super().send(request, **kwargs)


class FastCancel(threading.Event):
    def wait(self, timeout=None):
        return self.is_set()


class ServerFixture:
    def __init__(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_STORED) as archive:
            archive.writestr('WoWSReplayBatch/WoWSReplayBatch.exe', os.urandom(256 * 1024))
        self.payload = output.getvalue()
        self.hash = hashlib.sha256(self.payload).hexdigest()
        self.data = {'tag_name': 'v1.4.0', 'draft': False, 'prerelease': False, 'body': '测试更新说明',
                     'assets': [{'name': FILENAME, 'size': len(self.payload), 'digest': 'sha256:' + self.hash,
                                 'browser_download_url': f'https://github.com/{REPO}/releases/download/v1.4.0/{FILENAME}'}]}
        self.sums = f'{self.hash}  {FILENAME}\n'.encode()
        self.length_header = True
        self.requests = []
        self.delay = 0
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fixture.requests.append(self.path)
                body = json.dumps(fixture.data).encode() if self.path == '/api' else fixture.sums if self.path == '/sums' else fixture.payload
                self.send_response(200)
                if fixture.length_header or self.path != '/file':
                    self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    if fixture.delay and self.path == '/file':
                        for offset in range(0, len(body), 4096):
                            self.wfile.write(body[offset:offset + 4096])
                            self.wfile.flush()
                            time.sleep(fixture.delay)
                    else:
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
            def log_message(self, *_):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def client(self, root):
        client = UpdateClient(root, cancel=FastCancel())
        client.net.session.trust_env = False
        adapter = RouteAdapter('http://127.0.0.1:' + str(self.server.server_port))
        client.net.session.mount('https://', adapter)
        return client

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class UpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = ServerFixture()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.client = self.fixture.client(self.root)
        self.original = copy.deepcopy(self.fixture.data)
        self.original_payload = self.fixture.payload

    def tearDown(self):
        self.client.net.session.close()
        self.fixture.data = self.original
        self.fixture.payload = self.original_payload
        self.fixture.length_header = True
        self.temp.cleanup()

    def test_repository_accepts_name_and_github_url(self):
        for value in ['example/project', 'https://github.com/example/project/', 'https://github.com/example/project.git']:
            self.assertEqual(repository_name(value), REPO)

    def test_repository_rejects_unsafe_and_non_github_inputs(self):
        for value in ['', 'https://example.com/a/b', 'https://github.com@evil.example/a/b', '../foo', 'owner/..', 'a/b?token=secret', 'https://github.com/a/b?x=1']:
            with self.subTest(value=value), self.assertRaises(AppError):
                repository_name(value)

    def test_versions_are_numeric_and_two_part_versions_work(self):
        self.assertGreater(version_key('1.10.0'), version_key('1.9.9'))
        self.assertEqual(version_key('v1.4'), version_key('1.4.0'))
        for value in ['latest', '../1.4', '1.4.0-beta', None]:
            with self.assertRaises(AppError):
                version_key(value)

    def test_latest_release_metadata_and_current_version(self):
        release = self.client.check(REPO)
        self.assertTrue(release.newer_than('1.3.0'))
        self.assertFalse(release.newer_than('1.4.0'))
        self.assertFalse(release.newer_than('2.0.0'))
        self.assertEqual(release.notes, '测试更新说明')
        self.assertEqual(release.sha256, self.fixture.hash)
        self.assertFalse(list(self.root.iterdir()))

    def test_source_asset_and_other_versions_are_not_selected(self):
        self.fixture.data['assets'] += [{'name': 'WoWSReplayBatch-v1.4.0-source.zip'}, {'name': 'WoWSReplayBatch-v1.3.0-Windows.zip'}]
        self.assertEqual(self.client.check(REPO).filename, FILENAME)

    def test_draft_and_prerelease_are_rejected(self):
        for field in ['draft', 'prerelease']:
            self.fixture.data[field] = True
            with self.assertRaises(AppError):
                self.client.check(REPO)
            self.fixture.data[field] = False

    def test_checksum_required_and_sums_fallback(self):
        self.fixture.data['assets'][0]['digest'] = None
        with self.assertRaisesRegex(AppError, 'SHA-256'):
            self.client.check(REPO)
        self.fixture.data['assets'].append({'name': 'SHA256SUMS.txt', 'browser_download_url': f'https://github.com/{REPO}/releases/download/v1.4.0/SHA256SUMS.txt'})
        self.assertEqual(self.client.check(REPO).sha256, self.fixture.hash)

    def test_release_asset_must_belong_to_selected_repository(self):
        for url in ['http://github.com/example/project/releases/download/v1/a.zip', 'https://github.com/other/project/releases/download/v1/a.zip', 'https://evil.example/file.zip', 'https://github.com/example/project/releases/download/../../../bad.zip']:
            self.fixture.data['assets'][0]['browser_download_url'] = url
            with self.assertRaises(AppError):
                self.client.check(REPO)

    def test_duplicate_package_and_invalid_size_are_rejected(self):
        self.fixture.data['assets'].append(copy.deepcopy(self.fixture.data['assets'][0]))
        with self.assertRaises(AppError):
            self.client.check(REPO)
        self.fixture.data['assets'].pop()
        for size in [0, -1, True, '100']:
            self.fixture.data['assets'][0]['size'] = size
            with self.assertRaises(AppError):
                self.client.check(REPO)

    def test_streamed_download_progress_and_private_files_preserved(self):
        settings = self.root / 'settings.json'
        settings.write_text('private settings')
        replay = self.root / 'replays' / 'local.wowsreplay'
        replay.parent.mkdir()
        replay.write_bytes(b'private replay')
        events = []
        release = self.client.check(REPO)
        destination = self.client.download(release, lambda a, b: events.append((a, b)))
        self.assertEqual(destination.read_bytes(), self.fixture.payload)
        self.assertEqual(destination.parent.parent, self.root / 'updates')
        self.assertEqual(events[-1], (release.size, release.size))
        self.assertGreater(len(events), 2)
        self.assertEqual(settings.read_text(), 'private settings')
        self.assertEqual(replay.read_bytes(), b'private replay')

    def test_download_without_content_length_uses_release_size(self):
        self.fixture.length_header = False
        events = []
        release = self.client.check(REPO)
        self.client.download(release, lambda a, b: events.append((a, b)))
        self.assertTrue(all(total == release.size for _, total in events))

    def test_valid_download_is_reused(self):
        release = self.client.check(REPO)
        self.client.download(release)
        before = self.fixture.requests.count('/file')
        self.client.download(release)
        self.assertEqual(self.fixture.requests.count('/file'), before)

    def test_bad_hash_preserves_previous_file_and_removes_partial(self):
        self.fixture.data['assets'][0]['digest'] = 'sha256:' + '0' * 64
        release = self.client.check(REPO)
        destination = self.root / 'updates' / release.version / release.filename
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b'previous')
        with self.assertRaises(AppError):
            self.client.download(release)
        self.assertEqual(destination.read_bytes(), b'previous')
        self.assertFalse(list(self.root.rglob('*.part')))

    def test_cancellation_removes_partial_download(self):
        release = self.client.check(REPO)
        def progress(done, _):
            if done:
                self.client.net.cancel.set()
        with self.assertRaises(Cancelled):
            self.client.download(release, progress)
        self.assertFalse(list(self.root.rglob('*.zip')))
        self.assertFalse(list(self.root.rglob('*.part')))

    def test_non_zip_and_path_traversal_packages_are_rejected(self):
        for unsafe in [False, True]:
            payload = io.BytesIO()
            if unsafe:
                with zipfile.ZipFile(payload, 'w') as archive:
                    archive.writestr('../WoWSReplayBatch.exe', b'payload')
                self.fixture.payload = payload.getvalue()
            else:
                self.fixture.payload = b'<html>not a package</html>'
            asset = self.fixture.data['assets'][0]
            asset['size'] = len(self.fixture.payload)
            asset['digest'] = 'sha256:' + hashlib.sha256(self.fixture.payload).hexdigest()
            with self.assertRaises(AppError):
                self.client.download(self.client.check(REPO))
            self.assertFalse(list(self.root.rglob('*.zip')))
