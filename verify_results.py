"""Validate downloaded replays and decode every rendered MP4; requires ffmpeg."""
import argparse
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wows_replay_batch.core import mp4_valid, replay_meta, sha256, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        raise SystemExit("Validation requires ffprobe and ffmpeg on PATH.")

    def check(job):
        replay, video = directory / job["replay"], directory / job["video"]
        assert job["status"] in {"completed", "skipped"}, job
        assert sha256(replay) == job["replay_sha256"], replay.name
        assert sha256(video) == job["video_sha256"], video.name
        assert mp4_valid(video), video.name
        meta = replay_meta(replay)
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=codec_name,width,height,r_frame_rate,nb_frames,duration", "-show_entries",
                                "format=duration,size", "-of", "json", str(video)], capture_output=True, text=True, check=True)
        info = json.loads(probe.stdout)
        assert info["streams"][0]["codec_name"] == "h264", video.name
        assert float(info["format"]["duration"]) > 0, video.name
        decode = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-threads", "1", "-i", str(video), "-f", "null", "-"],
                                 capture_output=True, text=True, check=True)
        assert not decode.stderr.strip(), decode.stderr
        return {"report_id": job["report_id"], "match_id": job["match_id"], "team": job["team"],
                "replay_version": meta["clientVersionFromExe"], "video": job["video"],
                "probe": info, "full_decode": "passed", "sha256": job["video_sha256"]}

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(check, manifest["jobs"]))
    report = {"checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(), "team": manifest["team"],
              "tournament_url": manifest["tournament_url"], "replays_verified": len(results), "videos_decoded": len(results),
              "missing": manifest["missing"], "videos": results}
    write_json(directory / "validation.json", report)
    print(f"Verified {len(results)} replay hashes and fully decoded {len(results)} videos.")


if __name__ == "__main__":
    main()
