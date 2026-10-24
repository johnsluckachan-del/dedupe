"""Tests for the video half of Pass 4 (near-duplicate videos).

ffmpeg/ffprobe are not available everywhere, so these tests pin the command
shapes and the decision logic with the two subprocess helpers patched out.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dedupe.cli import parse_args
from dedupe.core import TrashManager
from dedupe.similar import (
    DEFAULT_VIDEO_DISTANCE,
    VIDEO_FRAME_COUNT,
    VIDEO_FRAME_MATCH_NEEDED,
    ffprobe_command,
    frames_matching,
    probe_video,
    run_similar_pass,
    video_frame_hashes,
)

NEAR = 0b1  # 1 bit away from 0
FAR = (1 << 64) - 1  # 64 bits away from 0


def completed(stdout=b"", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=b"")


class TestFfprobeCommand(unittest.TestCase):
    def test_never_passes_nostdin(self):
        # -nostdin is an ffmpeg-only option: ffprobe rejects unknown options
        # and exits 1, which would make every video unprobeable.
        self.assertNotIn("-nostdin", ffprobe_command(Path("clip.mp4")))

    def test_asks_for_duration_and_frame_size_as_json(self):
        argv = ffprobe_command(Path("clip.mp4"))
        self.assertEqual(argv[0], "ffprobe")
        self.assertEqual(argv[-1], "clip.mp4")
        self.assertIn("v:0", argv)
        self.assertIn("json", argv)
        entries = argv[argv.index("-show_entries") + 1]
        for field in ("width", "height", "duration"):
            self.assertIn(field, entries)


class TestProbeVideo(unittest.TestCase):
    def _probe(self, payload, returncode=0):
        with patch("dedupe.similar.subprocess.run",
                   return_value=completed(json.dumps(payload).encode(), returncode)):
            return probe_video(Path("clip.mp4"))

    def test_reads_format_duration(self):
        payload = {"streams": [{"width": 640, "height": 360}], "format": {"duration": "12.0"}}
        self.assertEqual(self._probe(payload), (12.0, 640, 360))

    def test_falls_back_to_stream_duration(self):
        payload = {"streams": [{"width": 640, "height": 360, "duration": "11.5"}], "format": {}}
        self.assertEqual(self._probe(payload), (11.5, 640, 360))

    def test_no_usable_duration_is_skipped(self):
        payload = {"streams": [{"width": 640, "height": 360}], "format": {"duration": "N/A"}}
        self.assertIsNone(self._probe(payload))

    def test_failed_probe_is_skipped(self):
        payload = {"streams": [{"width": 640, "height": 360}], "format": {"duration": "12.0"}}
        self.assertIsNone(self._probe(payload, returncode=1))

    def test_garbage_output_is_skipped(self):
        with patch("dedupe.similar.subprocess.run", return_value=completed(b"not json")):
            self.assertIsNone(probe_video(Path("broken.mp4")))

    def test_missing_ffprobe_is_skipped(self):
        with patch("dedupe.similar.subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(probe_video(Path("clip.mp4")))


class TestFrameHashes(unittest.TestCase):
    def test_frame_command_shape(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["stdin"] = kwargs.get("stdin")
            return completed(bytes(range(72)))

        with patch("dedupe.similar.subprocess.run", side_effect=fake_run):
            hashes = video_frame_hashes(Path("clip.mp4"), 12.0)

        argv = seen["argv"]
        self.assertEqual(argv[0], "ffmpeg")
        self.assertIn("-nostdin", argv)
        self.assertEqual(argv[argv.index("-frames:v") + 1], "1")
        self.assertIn("scale=9:8,format=gray", argv[argv.index("-vf") + 1])
        self.assertEqual(argv[argv.index("-f") + 1], "rawvideo")
        self.assertEqual(argv[-1], "-")
        self.assertEqual(seen["stdin"], subprocess.DEVNULL)
        self.assertEqual(len(hashes), VIDEO_FRAME_COUNT)

    def test_seek_times_span_the_middle_of_the_clip(self):
        times = []

        def fake_run(argv, **kwargs):
            times.append(float(argv[argv.index("-ss") + 1]))
            return completed(bytes(range(72)))

        with patch("dedupe.similar.subprocess.run", side_effect=fake_run):
            video_frame_hashes(Path("clip.mp4"), 100.0)

        self.assertEqual(len(times), VIDEO_FRAME_COUNT)
        self.assertAlmostEqual(times[0], 10.0, places=2)
        self.assertAlmostEqual(times[-1], 90.0, places=2)
        self.assertEqual(times, sorted(times))

    def test_short_frame_is_not_hashed(self):
        with patch("dedupe.similar.subprocess.run", return_value=completed(b"\0" * 10)):
            self.assertIsNone(video_frame_hashes(Path("clip.mp4"), 12.0))

    def test_a_few_unreadable_frames_still_judgeable(self):
        calls = {"n": 0}

        def flaky(path, t):
            calls["n"] += 1
            return None if calls["n"] <= VIDEO_FRAME_COUNT - VIDEO_FRAME_MATCH_NEEDED else 0

        with patch("dedupe.similar._grab_frame_hash", side_effect=flaky):
            hashes = video_frame_hashes(Path("clip.mp4"), 12.0)
        self.assertEqual(len(hashes), VIDEO_FRAME_COUNT)
        self.assertEqual(sum(1 for h in hashes if h is None),
                         VIDEO_FRAME_COUNT - VIDEO_FRAME_MATCH_NEEDED)

    def test_too_few_readable_frames_skips_the_video(self):
        with patch("dedupe.similar._grab_frame_hash", return_value=None):
            self.assertIsNone(video_frame_hashes(Path("clip.mp4"), 12.0))


class TestFramesMatching(unittest.TestCase):
    def test_counts_pairs_within_the_distance(self):
        a = [0] * VIDEO_FRAME_COUNT
        b = [NEAR] * 5 + [FAR, FAR]
        self.assertEqual(frames_matching(a, b, DEFAULT_VIDEO_DISTANCE), 5)

    def test_missing_frames_never_match(self):
        a = [0, None, 0]
        b = [0, None, None]
        self.assertEqual(frames_matching(a, b, DEFAULT_VIDEO_DISTANCE), 1)


class TestVideoSimilarPass(unittest.TestCase):
    """The pass itself, with ffprobe/ffmpeg replaced by canned answers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        # The bigger file is the lower resolution one: pixels must win.
        self.big_res = self._write("camera/holiday.mp4", 1000)
        self.small_res = self._write("whatsapp/VID-001.mkv", 4000)
        self.probes = {
            self.big_res: (12.0, 640, 360),
            self.small_res: (12.1, 320, 180),
        }
        self.hashes = {
            self.big_res: [0] * VIDEO_FRAME_COUNT,
            self.small_res: [NEAR] * 5 + [FAR, FAR],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rel, size):
        # Built by joining onto the already-resolved root, exactly the way
        # build_inventory builds the paths these are matched against.
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        return p

    def _run(self, **kwargs):
        with patch("dedupe.similar._have_ffmpeg_tools", return_value=True), \
                patch("dedupe.similar.probe_video", side_effect=self.probes.get), \
                patch("dedupe.similar.video_frame_hashes",
                      side_effect=lambda p, d: self.hashes.get(p)) as frames:
            result = run_similar_pass(
                self.root, TrashManager(self.root),
                removed_folders=set(), removed_files=set(), **kwargs,
            )
        self.frame_calls = [c.args[0] for c in frames.call_args_list]
        return result

    def test_review_lists_the_reencode_without_removing(self):
        removals, removed, matches = self._run(review=True)
        self.assertEqual(removals, [])
        self.assertEqual(removed, set())
        self.assertEqual(len(matches), 1)
        match = matches[0]
        self.assertEqual(match.removed_path, self.small_res)
        self.assertEqual(match.kept_path, self.big_res)
        self.assertEqual(match.kind, "video")
        self.assertEqual(match.score_text, f"frames 5/{VIDEO_FRAME_COUNT}")
        self.assertTrue(self.small_res.exists())

    def test_default_trashes_the_lower_resolution_copy(self):
        removals, removed, matches = self._run()
        self.assertEqual([r.reason for r in removals], ["similar-video"])
        self.assertEqual(removals[0].removed_path, self.small_res)
        self.assertEqual(removals[0].kept_path, self.big_res)
        self.assertEqual(removals[0].bytes_freed, 4000)
        self.assertEqual(removed, {self.small_res})
        self.assertFalse(self.small_res.exists())
        self.assertTrue(self.big_res.exists())

    def test_four_matching_frames_are_not_enough(self):
        self.hashes[self.small_res] = [NEAR] * 4 + [FAR, FAR, FAR]
        removals, removed, matches = self._run(review=True)
        self.assertEqual(matches, [])
        self.assertEqual(removals, [])

    def test_video_distance_flag_tightens_the_match(self):
        self.hashes[self.small_res] = [0b111] * 5 + [FAR, FAR]  # distance 3
        self.assertEqual(len(self._run(review=True)[2]), 1)
        self.assertEqual(self._run(review=True, video_distance=2)[2], [])

    def test_different_durations_are_never_hashed(self):
        self.probes[self.small_res] = (30.0, 320, 180)
        removals, removed, matches = self._run(review=True)
        self.assertEqual(matches, [])
        self.assertEqual(self.frame_calls, [])

    def test_unprobeable_video_is_skipped_silently(self):
        del self.probes[self.small_res]
        removals, removed, matches = self._run(review=True)
        self.assertEqual(matches, [])
        self.assertEqual(self.frame_calls, [])

    def test_missing_ffmpeg_warns_once_and_carries_on(self):
        with patch("dedupe.similar._have_ffmpeg_tools", return_value=False), \
                patch("dedupe.similar.sys.stderr") as err:
            removals, removed, matches = run_similar_pass(
                self.root, TrashManager(self.root),
                removed_folders=set(), removed_files=set(), review=True,
            )
        written = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertIn("video similarity skipped: ffmpeg not found", written)
        self.assertEqual(matches, [])
        self.assertTrue(self.small_res.exists())

    def test_quiet_suppresses_the_warning(self):
        with patch("dedupe.similar._have_ffmpeg_tools", return_value=False), \
                patch("dedupe.similar.sys.stderr") as err:
            run_similar_pass(
                self.root, TrashManager(self.root),
                removed_folders=set(), removed_files=set(), review=True, quiet=True,
            )
        self.assertEqual(err.write.call_args_list, [])


class TestVideoDistanceFlag(unittest.TestCase):
    def test_default_and_override(self):
        self.assertEqual(parse_args(["D:\\x"]).video_distance, DEFAULT_VIDEO_DISTANCE)
        self.assertEqual(parse_args(["D:\\x", "--video-distance", "4"]).video_distance, 4)


if __name__ == "__main__":
    unittest.main()
