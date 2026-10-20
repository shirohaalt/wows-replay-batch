import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zipfile

from wows_replay_batch.core import AppError, Cancelled, write_json
from wows_replay_batch.install_update import (EXE, digest, finish_install, independent_environment,
                                             install_files, load_job, mark_healthy, safe_path, stage_update)
from wows_replay_batch.runtime_layout import application_file
from wows_replay_batch.updates import Release


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "程序 with spaces"
        self.root.mkdir()
        self.originals = {
            EXE: b"previous executable", "使用说明.md": b"old manual",
            "tools/ffmpeg/ffmpeg.exe": b"old tool", "settings.json": b'{"custom": true}',
            "results/demo/manifest.json": b"manifest", "results/demo/replays/demo.wowsreplay": b"replay",
            "results/demo/videos/demo.mp4": b"video", "cache/replay-info/demo.json": b"roster",
            "cache/base-videos/demo.mp4": b"cached video", "custom-output/data.txt": b"custom location",
            "WoWSReplayBatch-output/manifest.json": b"local manifest",
            "cache/game-data/common/ab/" + "c" * 18: b"public asset",
        }
        for name, data in self.originals.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        self.files = {EXE: b"new executable", "使用说明.md": b"new manual",
                      "tools/ffmpeg/ffmpeg.exe": b"new tool", "licenses/Python.txt": b"new license"}
        self.package = Path(self.temp.name) / "update.zip"

    def release(self, files=None):
        with zipfile.ZipFile(self.package, "w") as archive:
            for name, data in (self.files if files is None else files).items():
                archive.writestr("WoWSReplayBatch-v9.1.0/" + name, data)
        return Release("9.1.0", "", "WoWSReplayBatch-v9.1.0-Windows.zip", "", self.package.stat().st_size, digest(self.package))

    def stage(self):
        return stage_update(self.root, self.package, self.release())

    def assert_originals(self):
        for name, data in self.originals.items():
            self.assertEqual((self.root / name).read_bytes(), data, name)

    def assert_private_preserved(self):
        for name, data in self.originals.items():
            if name not in self.files:
                self.assertEqual((self.root / name).read_bytes(), data, name)

    def test_stage_then_replace_preserves_all_private_data_and_backs_up_old_files(self):
        job = self.stage()
        self.assert_originals()
        result = install_files(job)
        self.assertEqual(result["state"], "awaiting_health")
        for name, data in self.files.items():
            self.assertEqual((self.root / name).read_bytes(), data)
            if name in self.originals:
                self.assertEqual((job.parent / "backup" / name).read_bytes(), self.originals[name])
        self.assert_private_preserved()

    def test_bad_package_digest_does_not_touch_installation(self):
        release = self.release()
        self.package.write_bytes(self.package.read_bytes() + b"modified")
        with self.assertRaises(AppError):
            stage_update(self.root, self.package, release)
        self.assert_originals()
        self.assertFalse((self.root / "updates").exists())

    def test_cancelled_stage_is_removed_and_originals_are_unchanged(self):
        release = self.release({**self.files, EXE: b"new" * 1024 ** 2})
        cancel = threading.Event()
        def progress(*_):
            cancel.set()
        with self.assertRaises(Cancelled):
            stage_update(self.root, self.package, release, cancel=cancel, progress=progress)
        self.assert_originals()
        self.assertEqual(list((self.root / "updates").iterdir()), [])

    def test_rejects_private_unknown_traversal_and_case_colliding_entries(self):
        for name in ("settings.json", "results/demo/manifest.json", "cache/base-videos/a.mp4",
                     "../WoWSReplayBatch.exe", "tools/ffmpeg/user.log", "licenses/CON.txt",
                     "WoWSReplayBatch.EXE", "licenses/Python.txt:stream", "licenses/Python.txt."):
            with self.subTest(name=name):
                release = self.release({**self.files, name: b"unwanted"})
                with self.assertRaises((AppError, ValueError)):
                    stage_update(self.root, self.package, release)
                self.assert_originals()

    def test_wrong_zip_root_is_rejected(self):
        with zipfile.ZipFile(self.package, "w") as archive:
            archive.writestr("another-version/" + EXE, b"new executable")
        release = Release("9.1.0", "", "", "", self.package.stat().st_size, digest(self.package))
        with self.assertRaises(AppError):
            stage_update(self.root, self.package, release)
        self.assert_originals()

    def test_tampered_stage_cannot_modify_installed_files(self):
        job = self.stage()
        (job.parent / "stage" / EXE).write_bytes(b"changed")
        with self.assertRaises(AppError):
            install_files(job)
        self.assert_originals()
        self.assertEqual(load_job(job)[1]["state"], "prepared")

    def test_middle_of_install_failure_restores_every_old_file_and_removes_new_files(self):
        job = self.stage()
        from wows_replay_batch.install_update import replace_file
        def fail_main(source, target, **kwargs):
            if source.parent.name == "stage" and source.name == EXE:
                raise PermissionError("file in use")
            return replace_file(source, target, **kwargs)
        with patch("wows_replay_batch.install_update.replace_file", side_effect=fail_main):
            with self.assertRaises(PermissionError):
                install_files(job)
        self.assert_originals()
        self.assertFalse((self.root / "licenses/Python.txt").exists())
        self.assertEqual(load_job(job)[1]["state"], "rolled_back")

    def test_restart_failure_restores_old_executable_and_private_data(self):
        job = self.stage()
        with self.assertRaises(OSError):
            finish_install(job, restart=Mock(side_effect=OSError("cannot launch")))
        self.assert_originals()
        self.assertEqual(load_job(job)[1]["state"], "rolled_back")

    def test_new_program_exits_before_healthy_so_old_files_are_restored(self):
        job = self.stage()
        with self.assertRaises(AppError):
            finish_install(job, restart=Mock(return_value=Mock(pid=123, poll=Mock(return_value=2))))
        self.assert_originals()
        self.assertEqual(load_job(job)[1]["state"], "rolled_back")

    def test_new_program_must_acknowledge_startup_before_completion(self):
        job = self.stage()
        def restart(command, root):
            self.assertEqual(command, [str(self.root / EXE), "--update-health", str(job)])
            self.assertEqual(Path(root), self.root)
            mark_healthy(job, root)
            return Mock(pid=123, poll=Mock(return_value=None))
        finish_install(job, restart=restart)
        self.assertEqual(load_job(job)[1]["state"], "completed")
        self.assert_private_preserved()
        self.assertEqual((self.root / EXE).read_bytes(), self.files[EXE])
        with self.assertRaises(AppError):
            install_files(job)

    def test_startup_acknowledgment_requires_correct_root_and_install_state(self):
        job = self.stage()
        with self.assertRaises(AppError):
            mark_healthy(job, self.root)
        install_files(job)
        with self.assertRaises(AppError):
            mark_healthy(job, self.root.parent)
        self.assertFalse((job.parent / "healthy.json").exists())

    def test_invalid_job_location_and_private_target_are_rejected(self):
        job = self.stage()
        data = json.loads(job.read_text("utf-8"))
        outside = self.root / "job.json"
        write_json(outside, data)
        with self.assertRaises(AppError):
            load_job(outside)
        data["files"].append({"name": "settings.json", "sha256": "0" * 64})
        write_json(job, data)
        with self.assertRaises(AppError):
            install_files(job)
        self.assert_originals()

    def test_windows_filename_rules_apply_even_on_other_platforms(self):
        for name in ("../a", "/a", "a\\b", "a/./b", "a//b", "a/NUL.txt", "a/name.", "a/file:stream", "a/*"):
            with self.subTest(name=name), self.assertRaises(AppError):
                safe_path(self.root, name)

    def test_symlink_cannot_redirect_update_to_user_data(self):
        folder = self.root / "licenses"
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        try:
            folder.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation requires permission")
        with self.assertRaises(AppError):
            self.stage()
        self.assertEqual(list(outside.iterdir()), [])

    def test_runtime_layout_only_allows_program_owned_paths(self):
        for name in (EXE, "docs/RELEASING.md", "tools/ffmpeg/ffmpeg.exe", "cache/game-data/15.9.0_123/.ready.json"):
            self.assertTrue(application_file(name), name)
        for name in ("settings.json", "cache/private", "results/manifest.json", "WoWSReplayBatch-output/manifest.json", "updates/file"):
            self.assertFalse(application_file(name), name)

    def test_restart_gets_a_new_pyinstaller_runtime(self):
        with patch.dict("os.environ", {"_PYI_APPLICATION_HOME_DIR": "old runtime"}):
            self.assertEqual(independent_environment()["PYINSTALLER_RESET_ENVIRONMENT"], "1")

    def test_journal_write_retries_transient_windows_file_occupancy(self):
        from wows_replay_batch.install_update import write_json as journal_json
        with patch("wows_replay_batch.install_update.atomic_json", side_effect=[PermissionError("sharing violation"), None]) as writer:
            journal_json(self.root / "status.json", {"state": "prepared"})
        self.assertEqual(writer.call_count, 2)

    def test_status_read_retries_transient_windows_file_occupancy(self):
        from wows_replay_batch.install_update import read_json
        with patch.object(Path, "read_text", side_effect=[PermissionError("sharing violation"), '{"state": "prepared"}']):
            self.assertEqual(read_json(self.root / "status.json"), {"state": "prepared"})
