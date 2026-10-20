import tempfile
import unittest
import zipfile
from pathlib import Path

from release_package import ROOT, runtime_files, verify_no_local_paths, write_archive


class ReleasePrivacyTests(unittest.TestCase):
    def test_runtime_whitelist_excludes_existing_user_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = ["WoWSReplayBatch.exe", "使用说明.md", "licenses/Python.txt", "tools/ffmpeg/ffmpeg.exe",
                     "cache/game-data/common/ab/" + "c" * 18, "cache/game-data/15.9.0_123/.ready.json",
                     "settings.json", "results/team/manifest.json", "results/team/replays/a.wowsreplay",
                     "screenshots/download.png", "run.log", "cache/replay-info/private.json", "cache/base-videos/a.mp4",
                     "cache/game-data/settings.json", "cache/game-data/15.9.0_123/history.json", "tools/ffmpeg/user.log",
                     "WoWSReplayBatch-output/manifest.json", "WoWSReplayBatch-output/videos/private.mp4"]
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            selected = {path.relative_to(root).as_posix() for path in runtime_files(root)}
            self.assertEqual(selected, set(names[:6]))
            archive = root / "public.zip"
            write_archive(archive, runtime_files(root), root)
            with zipfile.ZipFile(archive) as result:
                self.assertEqual(set(result.namelist()), selected)

    def test_local_paths_are_rejected_in_plain_and_json_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "说明.md"
            for value in [str(ROOT), str(ROOT).replace("\\", "/"), str(ROOT).replace("\\", "\\\\")]:
                for encoding in ["utf-8", "utf-16-le"]:
                    path.write_bytes(value.encode(encoding))
                    with self.assertRaises(ValueError):
                        verify_no_local_paths([path])

    def test_failed_privacy_check_preserves_previous_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "说明.md"
            private.write_text(str(ROOT), encoding="utf-8")
            archive = root / "public.zip"
            archive.write_bytes(b"previous")
            with self.assertRaises(ValueError):
                write_archive(archive, [private], root)
            self.assertEqual(archive.read_bytes(), b"previous")
