import hashlib
import gzip
import json
import struct
import tempfile
import threading
import unittest
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

from wows_replay_batch.core import (
    AppError, Cancelled, Network, Tournament, TournamentClient,
    Options, Runner, mp4_valid, replay_meta, safe_name, team_matches, unpack_zstd, write_json,
)


def match(mid, tag="DEMO"):
    return {"id": mid, "scheduledTime": "2026-10-11", "matchOpponents": [
        {"id": 1, "signup": {"teamTag": tag, "teamName": f"[{tag}] 示例测试队"}},
        {"id": 2, "signup": {"teamTag": "OTHER", "teamName": "[OTHER] Test"}}]}


class PaginationTests(unittest.TestCase):
    def client(self, pages):
        network = Mock()
        network.session.cookies.get.return_value = "anonymous-token"
        network.json.side_effect = [{}] + pages
        return TournamentClient(Tournament.parse("https://wows-tournaments.com/king-of-the-sea-xix/apac/matches"), network), network

    def test_site_can_report_wrong_total_pages(self):
        client, network = self.client([{"items": [match(1)], "totalCount": 1, "totalPages": 2}])
        self.assertEqual([m["id"] for m in client.search("demo")], [1])
        self.assertEqual(network.json.call_count, 2)
        headers = network.json.call_args.kwargs["headers"]
        self.assertEqual(headers["X-XSRF-TOKEN"], "anonymous-token")
        self.assertEqual(headers["x-tournament-region"], "APAC")

    def test_pagination_de_duplicates_overlapping_pages(self):
        client, _ = self.client([{"items": [match(1), match(2)], "totalCount": 3},
                                 {"items": [match(2), match(3)], "totalCount": 3}])
        self.assertEqual([m["id"] for m in client.search("DEMO")], [1, 2, 3])

    def test_repeated_page_does_not_silently_lose_matches(self):
        client, _ = self.client([{"items": [match(1)], "totalCount": 2},
                                 {"items": [match(1)], "totalCount": 2}])
        with self.assertRaisesRegex(AppError, "重复或空页"):
            client.search("DEMO")

    def test_server_substring_search_is_filtered_to_exact_team(self):
        client, _ = self.client([{"items": [match(1), match(2, "PDEMO")], "totalCount": 2}])
        self.assertEqual([m["id"] for m in client.search("DEMO")], [1])


class DiscoveryTests(unittest.TestCase):
    def test_report_order_does_not_determine_perspective(self):
        net = Mock()
        m = {**match(10), "matchGames": [{"id": 20, "number": 1}]}
        detail = {"report1": {"id": 100, "opponentId": 2, "replayFileId": 1000},
                  "report2": {"id": 101, "opponentId": 1, "replayFileId": 1001}}
        net.json.side_effect = [m, detail]
        client = TournamentClient(Tournament("king-of-the-sea-xix", "apac"), net)
        jobs, missing = client.discover([m], "DEMO")
        self.assertEqual([j["report_id"] for j in jobs], [101])
        self.assertEqual(missing, [])

    def test_missing_target_report_does_not_substitute_opponent(self):
        net = Mock()
        m = {**match(10), "matchGames": [{"id": 20, "number": 1}]}
        net.json.side_effect = [m, {"report1": {"id": 100, "opponentId": 2, "replayFileId": 1000}, "report2": None}]
        client = TournamentClient(Tournament("king-of-the-sea-xix", "apac"), net)
        jobs, missing = client.discover([m], "DEMO")
        self.assertEqual(jobs, [])
        self.assertEqual(missing[0]["team"], "DEMO")

    def test_both_perspectives(self):
        net = Mock()
        m = {**match(10), "matchGames": [{"id": 20}]}
        net.json.side_effect = [m, {"report1": {"id": 100, "opponentId": 1, "replayFileId": 1000},
                                   "report2": {"id": 101, "opponentId": 2, "replayFileId": 1001}}]
        jobs, missing = TournamentClient(Tournament("kots", "apac"), net).discover([m], "DEMO", both=True)
        self.assertEqual({j["team"] for j in jobs}, {"DEMO", "OTHER"})
        self.assertEqual(missing, [])


class FileTests(unittest.TestCase):
    def test_replay_rejects_html_error_page(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.wowsreplay"
            p.write_bytes(b"<html>Access denied</html>")
            with self.assertRaises(AppError):
                replay_meta(p)

    def test_replay_reads_only_header_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "回放.wowsreplay"
            data = json.dumps({"clientVersionFromExe": "15,9,0,13357625", "playerName": "测试"}).encode()
            p.write_bytes(b"\x12\x32\x34\x11" + struct.pack("<II", 3, len(data)) + data + b"encrypted packets")
            self.assertEqual(replay_meta(p)["playerName"], "测试")

    def test_path_names_cannot_escape_output_directory(self):
        self.assertNotIn("/", safe_name("../../DEMO"))
        self.assertNotIn("\\", safe_name("..\\..\\DEMO"))
        self.assertEqual(safe_name("CON"), "_CON")

    def test_partial_mp4_not_accepted_without_index(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "partial.mp4"
            p.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"x" * 1200)
            self.assertFalse(mp4_valid(p))

    def test_atomic_json_preserves_unicode(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "manifest.json"
            write_json(p, {"状态": "已完成"})
            self.assertEqual(json.loads(p.read_text("utf-8")), {"状态": "已完成"})
            self.assertFalse(p.with_name("manifest.json.tmp").exists())


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = b"valid content" * 1024
        cls.calls = 0
        cls.flaky_calls = 0
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.calls += 1
                self.send_response(200)
                payload = gzip.compress(cls.payload) if self.path == "/gzip" else cls.payload
                if self.path != "/unknown":
                    self.send_header("Content-Length", str(len(payload)))
                if self.path == "/gzip":
                    self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                if self.path == "/flaky":
                    cls.flaky_calls += 1
                    if cls.flaky_calls == 1:
                        self.wfile.write(cls.payload[:100])
                        self.close_connection = True
                        return
                self.wfile.write(payload)
            def log_message(self, *args):
                pass
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/object"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_hash_verification_replaces_corrupt_cache_then_skips(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "object"
            p.write_bytes(b"corrupt cached object")
            net = Network()
            digest = hashlib.sha256(self.payload).hexdigest()[:20]
            before = self.calls
            net.download(self.url, p, expected_hash=digest)
            net.download(self.url, p, expected_hash=digest)
            self.assertEqual(p.read_bytes(), self.payload)
            self.assertEqual(self.calls - before, 1)
            self.assertFalse(p.with_name("object.part").exists())

    def test_cancellation_does_not_start_request(self):
        cancel = threading.Event()
        cancel.set()
        with tempfile.TemporaryDirectory() as d, self.assertRaises(Cancelled):
            Network(cancel).download(self.url, Path(d) / "object")

    def test_interrupted_transfer_retries_and_commits_only_complete_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "object"
            Network().download(self.url.replace("/object", "/flaky"), p, expected_hash=hashlib.sha256(self.payload).hexdigest())
            self.assertEqual(p.read_bytes(), self.payload)
            self.assertEqual(self.flaky_calls, 2)
            self.assertFalse(p.with_name("object.part").exists())

    def test_changed_remote_replay_can_replace_corrupt_local_cache(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "replay"
            p.write_bytes(b"locally damaged replay")
            Network().download(self.url, p, cache_hash=hashlib.sha256(b"previous valid replay").hexdigest())
            self.assertEqual(p.read_bytes(), self.payload)

    def test_known_size_progress_includes_start_finish_and_cached_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "object"
            progress = Mock()
            digest = hashlib.sha256(self.payload).hexdigest()
            net = Network()
            net.download(self.url, path, expected_hash=digest, progress=progress)
            self.assertEqual(progress.call_args_list[0].args, (0, len(self.payload)))
            self.assertEqual(progress.call_args.args, (len(self.payload), len(self.payload)))
            before = self.calls
            net.download(self.url, path, expected_hash=digest, progress=progress)
            self.assertEqual(self.calls, before)
            self.assertEqual(progress.call_args.args, (len(self.payload), len(self.payload)))

    def test_unknown_or_gzip_size_does_not_report_false_percentage(self):
        with tempfile.TemporaryDirectory() as d:
            for endpoint in ("unknown", "gzip"):
                progress = Mock()
                Network().download(self.url.replace("object", endpoint), Path(d) / endpoint, progress=progress)
                self.assertEqual(progress.call_args_list[0].args, (0, 0))
                self.assertEqual(progress.call_args.args, (len(self.payload), len(self.payload)))


class ProcessTests(unittest.TestCase):
    def test_stopping_conversion_terminates_its_child_process(self):
        with tempfile.TemporaryDirectory() as d:
            runner = Runner(Options(output=d), root=d)
            timer = threading.Timer(0.4, runner.cancel.set)
            timer.start()
            try:
                with self.assertRaises(Cancelled):
                    runner.run_process([sys.executable, "-c", "import time;time.sleep(60)"], Path(d) / "child.log")
                self.assertIsNone(runner.process)
            finally:
                timer.cancel()

    def test_compressed_upstream_data_matches_raw_cas_hash(self):
        try:
            from compression import zstd
            compress = zstd.compress
        except ImportError:
            import zstandard
            compress = zstandard.ZstdCompressor().compress
        payload = b"renderer game parameters" * 1000
        with tempfile.TemporaryDirectory() as d:
            source, destination = Path(d) / "packed", Path(d) / "raw"
            source.write_bytes(compress(payload))
            unpack_zstd(source, destination, hashlib.sha256(payload).hexdigest()[:20])
            self.assertEqual(destination.read_bytes(), payload)


class InputTests(unittest.TestCase):
    def test_only_expected_tournament_origin(self):
        for url in ("http://wows-tournaments.com/kots/apac/matches", "https://wows-tournaments.com.evil.test/kots/apac/matches", "https://example.com/kots/apac/matches"):
            with self.assertRaises(AppError):
                Tournament.parse(url)

    def test_chinese_name_tag_and_case_are_supported(self):
        signup = {"teamTag": "DEMO", "teamName": "[DEMO] 示例测试队"}
        for name in (" demo ", "[DEMO]", "示例测试队", "[DEMO] 示例测试队"):
            self.assertTrue(team_matches(signup, name))
        self.assertFalse(team_matches(signup, "ARI"))


if __name__ == "__main__":
    unittest.main()
