import json
import shutil
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wows_replay_batch.core import AppError, Cancelled, Options, sha256, write_json
from wows_replay_batch.local_replays import LocalRunner, OUTPUT_DIRECTORY, read_local_library
from wows_replay_batch.regions import REGIONS, region_for_url
from wows_replay_batch.core import Tournament


def make_replay(path, player='Demo Player', extra=b'packet data'):
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {'clientVersionFromExe': '15,9,0,13357625', 'playerName': player,
            'mapDisplayName': 'Demo Map', 'mapName': 'spaces/demo', 'dateTime': '01.01.2026 12:00:00',
            'playerVehicle': 'PABC001-Demo-Ship', 'duration': 900}
    encoded = json.dumps(meta).encode()
    path.write_bytes(b'\x12\x32\x34\x11' + struct.pack('<II', 1, len(encoded)) + encoded + extra)
    return path


class LocalReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.folder = self.base / 'local replays'
        self.folder.mkdir()
        self.runner = LocalRunner(self.folder, Options(show_bans=True), root=self.base / 'app')
        self.runner.net.request = Mock(side_effect=AssertionError('Local scan must not access the network'))
        self.runner.client.search = Mock(side_effect=AssertionError('Local conversion queried tournament'))

    def tearDown(self):
        self.temp.cleanup()

    def test_recursive_scan_reads_metadata_without_network_or_renderer(self):
        source = make_replay(self.folder / 'one.wowsreplay')
        make_replay(self.folder / 'nested' / 'two.WOWSREPLAY', player='Second Player')
        (self.folder / 'ignore.txt').write_text('not a replay')
        with patch('wows_replay_batch.core.Tools.renderer', side_effect=AssertionError('Scanning started a renderer')):
            result = self.runner.scan()
        self.assertEqual(len(result['jobs']), 2)
        job = next(j for j in result['jobs'] if j['replay'] == 'one.wowsreplay')
        self.assertEqual((job['team'], job['map'], job['ship']), ('Demo Player', 'Demo Map', 'Demo-Ship'))
        self.assertEqual(self.runner.replay_path(job), source)
        self.assertEqual(job['replay_sha256'], sha256(source))
        self.assertEqual(self.runner.load_manifest()[1], result)
        self.runner.net.request.assert_not_called()
        self.assertFalse(list(self.folder.rglob('*.mp4')))

    def test_invalid_replay_is_skipped_but_valid_files_remain(self):
        make_replay(self.folder / 'valid.wowsreplay')
        (self.folder / 'broken.wowsreplay').write_bytes(b'<html>invalid</html>')
        result = self.runner.scan()
        self.assertEqual(len(result['jobs']), 1)
        self.assertEqual(result['invalid_files'][0]['file'], 'broken.wowsreplay')

    def test_empty_or_all_invalid_folder_fails_without_manifest(self):
        with self.assertRaises(AppError):
            self.runner.scan()
        (self.folder / 'broken.wowsreplay').write_bytes(b'invalid')
        with self.assertRaises(AppError):
            self.runner.scan()
        self.assertFalse((self.runner.output / 'manifest.json').exists())

    def test_original_files_and_existing_tournament_manifest_are_preserved(self):
        replay = make_replay(self.folder / 'source.wowsreplay')
        original_hash = sha256(replay)
        foreign = self.folder / 'manifest.json'
        foreign.write_text('existing tournament manifest')
        self.runner.scan()
        self.assertEqual(foreign.read_text(), 'existing tournament manifest')
        self.assertEqual(sha256(replay), original_hash)
        self.assertFalse((self.runner.output / 'replays').exists())

    def test_duplicate_basenames_have_distinct_ids_and_videos(self):
        make_replay(self.folder / 'a' / 'same.wowsreplay')
        make_replay(self.folder / 'b' / 'same.wowsreplay')
        jobs = self.runner.scan()['jobs']
        self.assertEqual(len({j['report_id'] for j in jobs}), 2)
        self.assertEqual(len({j['video'] for j in jobs}), 2)

    def test_rescan_retains_conversion_history_and_move_retains_identity(self):
        make_replay(self.folder / 'demo.wowsreplay')
        result = self.runner.scan()
        job = result['jobs'][0]
        job.update(status='completed', fingerprint='saved-options', video_sha256='saved-video')
        write_json(self.runner.output / 'manifest.json', result)
        again = self.runner.scan()['jobs'][0]
        self.assertEqual((again['report_id'], again['fingerprint']), (job['report_id'], 'saved-options'))
        moved = self.base / 'moved'
        shutil.copytree(self.folder, moved)
        moved_runner = LocalRunner(moved, root=self.base / 'app')
        self.assertEqual(moved_runner.scan()['jobs'][0]['report_id'], job['report_id'])

    def test_changed_source_has_new_identity_and_does_not_reuse_history(self):
        path = make_replay(self.folder / 'demo.wowsreplay')
        first = self.runner.scan()['jobs'][0]
        with path.open('ab') as file:
            file.write(b'changed source')
        second = self.runner.scan()['jobs'][0]
        self.assertNotEqual(first['report_id'], second['report_id'])
        self.assertEqual(second['status'], 'downloaded')

    def test_scan_cancellation_does_not_commit_partial_library(self):
        make_replay(self.folder / 'demo.wowsreplay')
        self.runner.cancel.set()
        with self.assertRaises(Cancelled):
            self.runner.scan()
        self.assertFalse((self.runner.output / 'manifest.json').exists())

    def test_output_folder_is_not_rescanned(self):
        make_replay(self.folder / 'demo.wowsreplay')
        make_replay(self.runner.output / 'ignored.wowsreplay')
        self.assertEqual(len(self.runner.scan()['jobs']), 1)

    def test_foreign_output_manifest_is_not_overwritten(self):
        path = self.runner.output / 'manifest.json'
        path.write_text('{"kind":"another-application","jobs":[]}')
        before = path.read_bytes()
        with self.assertRaises(AppError):
            LocalRunner(self.folder)
        self.assertEqual(path.read_bytes(), before)

    def test_manifest_rejects_outside_source_and_video_paths(self):
        make_replay(self.folder / 'demo.wowsreplay')
        original = self.runner.scan()
        for field, path in [('replay', '../outside.wowsreplay'), ('video', '../../outside.mp4')]:
            manifest = json.loads(json.dumps(original))
            manifest['jobs'][0][field] = path
            write_json(self.runner.output / 'manifest.json', manifest)
            with self.assertRaises(AppError):
                read_local_library(self.folder)

    def test_symlink_to_outside_source_is_skipped(self):
        outside = make_replay(self.base / 'outside.wowsreplay')
        try:
            (self.folder / 'link.wowsreplay').symlink_to(outside)
        except OSError:
            self.skipTest('Symlinks are unavailable in this environment')
        make_replay(self.folder / 'inside.wowsreplay')
        result = self.runner.scan()
        self.assertEqual(len(result['jobs']), 1)
        self.assertEqual(len(result['invalid_files']), 1)

    def test_conversion_uses_only_selected_source_and_forces_no_bp(self):
        make_replay(self.folder / 'one.wowsreplay')
        make_replay(self.folder / 'two.wowsreplay')
        manifest = self.runner.scan()
        selected = manifest['jobs'][0]
        before = sha256(self.folder / selected['replay'])
        observed = []
        def render(job, *_):
            observed.append(self.runner.replay_path(job))
            self.assertFalse(self.runner.options.show_bans)
            job['status'] = 'completed'
        self.runner.render_job = Mock(side_effect=render)
        result = self.runner.convert([selected['report_id']])
        self.assertEqual(observed, [self.folder / selected['replay']])
        self.assertEqual(result['summary']['converted'], 1)
        self.assertEqual(result['jobs'][1]['status'], 'downloaded')
        self.assertEqual(sha256(self.folder / selected['replay']), before)
        self.runner.client.search.assert_not_called()
        self.runner.net.request.assert_not_called()


class RegionTests(unittest.TestCase):
    def test_region_urls_and_api_headers_match_requested_servers(self):
        for name, region in [('亚服', 'apac'), ('欧服', 'eu'), ('美服', 'na')]:
            url = REGIONS[name]
            self.assertEqual(url, f'https://wows-tournaments.com/king-of-the-sea-xix/{region}/matches')
            self.assertEqual(Tournament.parse(url).headers['x-tournament-region'], region.upper())
            self.assertEqual(region_for_url(url), name)
            self.assertEqual(region_for_url(url + '/'), name)

    def test_default_region_is_asia(self):
        self.assertEqual(region_for_url(''), '亚服')
