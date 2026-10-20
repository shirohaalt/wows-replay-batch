import json
import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wows_replay_batch.core import AppError, Options, Runner, load_library, sha256, write_json
from wows_replay_batch.video import fingerprint


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runner = Runner(Options(output=str(self.root), show_bans=True), root=self.root)
        self.library = self.runner.output
        self.jobs = []
        for report in (101, 102):
            replay = self.library / f"replays/{report}.wowsreplay"
            replay.parent.mkdir(parents=True, exist_ok=True)
            header = json.dumps({"clientVersionFromExe": "15,9,0,13357625"}).encode()
            replay.write_bytes(b"\x12\x32\x34\x11" + struct.pack("<II", 1, len(header)) + header + bytes([report]))
            self.jobs.append({"report_id": report, "match_id": 10, "game_number": 1, "team": "DEMO" if report == 101 else "OTHER",
                              "opponents": ["DEMO", "OTHER"], "map": "Test", "stage": "Final", "round": "R1",
                              "replay": f"replays/{report}.wowsreplay", "video": f"videos/{report}.mp4",
                              "download_url": f"https://example.invalid/{report}", "status": "downloaded", "replay_sha256": sha256(replay)})
        self.manifest = {"team": "DEMO", "tournament_url": self.runner.tournament.url, "missing": [], "jobs": self.jobs}
        self.save()

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        write_json(self.library / "manifest.json", self.manifest)

    def forbid_network(self, runner):
        runner.client.search = Mock(side_effect=AssertionError("Conversion queried the tournament"))
        runner.client.discover = Mock(side_effect=AssertionError("Conversion rediscovered REP"))
        runner.net.download = Mock(side_effect=AssertionError("Conversion downloaded REP"))
        runner.net.json = Mock(side_effect=AssertionError("Conversion queried an API"))

    def test_local_conversion_uses_selection_and_preserves_unselected_jobs(self):
        runner = Runner.from_manifest(self.library, self.runner.options, root=self.root)
        self.forbid_network(runner)
        before = self.jobs[1].copy()
        runner.render_job = Mock(side_effect=lambda job, *args: job.update(status="completed"))
        result = runner.convert([101])
        self.assertEqual(result["summary"]["converted"], 1)
        self.assertEqual(runner.render_job.call_args.args[0]["report_id"], 101)
        self.assertEqual({k: v for k, v in result["jobs"][1].items() if k != "target_team"}, before)
        runner.net.download.assert_not_called()
        runner.client.search.assert_not_called()

    def test_matching_video_skips_offline_without_loading_tools(self):
        job = self.jobs[0]
        video = self.library / job["video"]
        video.parent.mkdir(parents=True)
        def box(kind, payload):
            return struct.pack(">I", 8 + len(payload)) + kind + payload
        video.write_bytes(box(b"ftyp", b"isom0000") + box(b"mdat", b"x" * 1200) + box(b"moov", b"index"))
        job.update(status="completed", fingerprint=fingerprint(self.runner.options, job)[0], video_sha256=sha256(video))
        self.save()
        runner = Runner.from_manifest(self.library, self.runner.options, root=self.root)
        self.forbid_network(runner)
        with patch("wows_replay_batch.core.Tools.renderer", side_effect=AssertionError("Loaded tools unnecessarily")):
            result = runner.convert([101])
        self.assertEqual(result["summary"]["skipped"], 1)
        self.assertEqual(result["summary"]["downloaded"], 0)

    def test_missing_rep_fails_locally_without_fetching_replacement(self):
        (self.library / self.jobs[0]["replay"]).unlink()
        self.forbid_network(self.runner)
        result = self.runner.convert([101])
        self.assertEqual(result["summary"]["failed"], 1)
        self.assertIn("本地回放不存在", result["jobs"][0]["error"])
        self.runner.net.download.assert_not_called()

    def test_modified_rep_cannot_reuse_old_video(self):
        with (self.library / self.jobs[0]["replay"]).open("ab") as f:
            f.write(b"damage")
        self.forbid_network(self.runner)
        result = self.runner.convert([101])
        self.assertEqual(result["summary"]["failed"], 1)
        self.assertIn("校验失败", result["jobs"][0]["error"])

    def test_download_does_not_convert_and_retains_other_perspective(self):
        job = self.jobs[0]
        job.update(status="completed", fingerprint="existing-render", video_sha256="existing-video")
        self.save()
        self.runner.client.discover = Mock(return_value=([job.copy()], []))
        self.runner.client.draft_data = {}
        self.runner.net.download = Mock(side_effect=lambda url, path, **kw: kw["progress"](path.stat().st_size, path.stat().st_size))
        events = []
        self.runner.emit = lambda kind, value: events.append((kind, value))
        with patch("wows_replay_batch.core.Tools.renderer", side_effect=AssertionError("Download started renderer")):
            result = self.runner.download([{"id": 10}])
        self.assertEqual(result["summary"]["downloaded"], 1)
        self.assertEqual(len(result["jobs"]), 2)
        self.assertEqual(result["jobs"][0]["fingerprint"], "existing-render")
        self.assertEqual(result["jobs"][0]["status"], "completed")
        progress = [value for kind, value in events if kind == "download_progress"]
        self.assertEqual(progress[-1]["value"], 1)
        self.assertEqual(progress[-1]["done"], 1)
        self.assertTrue(any(value["received"] > 0 for value in progress))

    def test_download_failed_job_still_completes_batch_progress(self):
        self.runner.client.discover = Mock(return_value=([self.jobs[0].copy()], []))
        self.runner.client.draft_data = {}
        self.runner.net.download = Mock(side_effect=AppError("download failed"))
        events = []
        self.runner.emit = lambda kind, value: events.append((kind, value))
        result = self.runner.download([{"id": 10}])
        self.assertEqual(result["summary"]["failed"], 1)
        final = [v for k, v in events if k == "download_progress"][-1]
        self.assertEqual(final["value"], final["total"])
        self.assertEqual(final["state"], "failed")

    def test_manifest_rejects_paths_outside_its_library(self):
        self.jobs[0]["video"] = "../../outside.mp4"
        self.save()
        with self.assertRaisesRegex(AppError, "超出回放目录"):
            load_library(self.library)

    def test_empty_selection_does_not_convert_every_file(self):
        self.runner.render_job = Mock()
        with self.assertRaises(AppError):
            self.runner.convert([])
        self.runner.render_job.assert_not_called()

    def test_local_library_may_be_moved_or_selected_by_replays_folder(self):
        moved = self.root / "移动后的回放"
        shutil.copytree(self.library, moved)
        runner = Runner.from_manifest(moved / "replays", Options(team="Other", output="ignored"), root=self.root)
        self.assertEqual(runner.output, moved.resolve())
        self.assertEqual(runner.options.team, "DEMO")
        self.assertEqual(runner.tournament.url, self.runner.tournament.url)


if __name__ == "__main__":
    unittest.main()
