"""Tests for flat image and video frame detection and caching (task-20)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dedupe.cache import ProbeCache
from dedupe.similar import (
    FLAT_THRESHOLD,
    dhash_image,
    is_thumbnail_flat,
    video_frame_hashes,
    _grab_frame_hash,
)

try:
    from PIL import Image
    HAVE_PILLOW = True
except Exception:
    HAVE_PILLOW = False


def completed(stdout=b"", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=b"")


class TestFlatThumbnail(unittest.TestCase):
    def test_threshold_constant(self):
        self.assertEqual(FLAT_THRESHOLD, 8)

    def test_is_thumbnail_flat_true_when_diff_within_8(self):
        self.assertTrue(is_thumbnail_flat([100] * 72))
        self.assertTrue(is_thumbnail_flat([100] * 36 + [108] * 36))
        self.assertTrue(is_thumbnail_flat([0] * 71 + [8]))

    def test_is_thumbnail_flat_false_when_diff_exceeds_8(self):
        self.assertFalse(is_thumbnail_flat([100] * 36 + [109] * 36))
        self.assertFalse(is_thumbnail_flat([0] * 71 + [9]))
        self.assertFalse(is_thumbnail_flat(list(range(72))))

    def test_is_thumbnail_flat_empty(self):
        self.assertFalse(is_thumbnail_flat([]))


class TestFlatImage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_solid_colour_image_gives_none(self):
        if not HAVE_PILLOW:
            return
        p = self.root / "solid_red.png"
        img = Image.new("RGB", (100, 100), (200, 30, 30))
        img.save(p)
        self.assertIsNone(dhash_image(p))

    def test_nearly_flat_image_gives_none(self):
        if not HAVE_PILLOW:
            return
        p = self.root / "nearly_flat.png"
        img = Image.new("L", (100, 100), 128)
        px = img.load()
        px[0, 0] = 126
        px[99, 99] = 130
        img.save(p)
        self.assertIsNone(dhash_image(p))

    def test_high_contrast_image_gives_hash(self):
        if not HAVE_PILLOW:
            return
        p = self.root / "contrast.png"
        img = Image.new("L", (100, 100), 0)
        px = img.load()
        for x in range(50, 100):
            for y in range(100):
                px[x, y] = 255
        img.save(p)
        res = dhash_image(p)
        self.assertIsNotNone(res)
        bits, pixels = res
        self.assertEqual(pixels, 10000)
        self.assertIsInstance(bits, int)


class TestFlatVideoFrames(unittest.TestCase):
    def test_grab_frame_flat_gives_none(self):
        with patch("dedupe.similar.subprocess.run", return_value=completed(bytes([128] * 72))):
            self.assertIsNone(_grab_frame_hash(Path("clip.mp4"), 1.0))

    def test_grab_frame_nearly_flat_gives_none(self):
        data = bytes([120] * 36 + [128] * 36)  # diff = 8 <= 8
        with patch("dedupe.similar.subprocess.run", return_value=completed(data)):
            self.assertIsNone(_grab_frame_hash(Path("clip.mp4"), 1.0))

    def test_grab_frame_contrast_gives_hash(self):
        data = bytes([100] * 36 + [120] * 36)  # diff = 20 > 8
        with patch("dedupe.similar.subprocess.run", return_value=completed(data)):
            h = _grab_frame_hash(Path("clip.mp4"), 1.0)
            self.assertIsNotNone(h)
            self.assertIsInstance(h, int)

    def test_video_with_four_flat_frames_gives_none(self):
        calls = {"n": 0}

        def fake_grab(path, t):
            calls["n"] += 1
            if calls["n"] <= 4:
                return None  # 4 flat frames
            return 12345

        with patch("dedupe.similar._grab_frame_hash", side_effect=fake_grab):
            self.assertIsNone(video_frame_hashes(Path("clip.mp4"), 10.0))

    def test_video_with_two_flat_frames_returns_hashes(self):
        calls = {"n": 0}

        def fake_grab(path, t):
            calls["n"] += 1
            if calls["n"] <= 2:
                return None  # 2 flat frames
            return 12345

        with patch("dedupe.similar._grab_frame_hash", side_effect=fake_grab):
            hashes = video_frame_hashes(Path("clip.mp4"), 10.0)
            self.assertIsNotNone(hashes)
            self.assertEqual(len(hashes), 7)
            self.assertEqual(hashes[0], None)
            self.assertEqual(hashes[1], None)
            self.assertEqual(hashes[2], 12345)


class TestProbeCacheFlatHandling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_cache_miss_on_entry_without_image_flat(self):
        cache = ProbeCache(cache_dir=self.cache_dir)
        p = Path("test.png").resolve()
        cache._data[str(p)] = {
            "size": 100,
            "mtime": 1000.0,
            "image": [0, 1000],
            "last_used": 1000.0,
        }
        hit, res = cache.get_image(p, 100, 1000.0)
        self.assertFalse(hit)
        self.assertIsNone(res)

    def test_cache_miss_on_all_zero_image_hash(self):
        cache = ProbeCache(cache_dir=self.cache_dir)
        p = Path("test.png").resolve()
        cache._data[str(p)] = {
            "size": 100,
            "mtime": 1000.0,
            "image": [0, 1000],
            "image_flat": False,
            "last_used": 1000.0,
        }
        hit, res = cache.get_image(p, 100, 1000.0)
        self.assertFalse(hit)

    def test_cache_hit_on_image_flat(self):
        cache = ProbeCache(cache_dir=self.cache_dir)
        p = Path("test.png").resolve()
        cache.set_image(p, 100, 1000.0, None, is_flat=True)
        hit, res = cache.get_image(p, 100, 1000.0)
        self.assertTrue(hit)
        self.assertIsNone(res)

    def test_cache_miss_on_entry_without_video_flat(self):
        cache = ProbeCache(cache_dir=self.cache_dir)
        p = Path("test.mp4").resolve()
        cache._data[str(p)] = {
            "size": 100,
            "mtime": 1000.0,
            "video_frames": [0, 0, 0, 0, 0, 0, 0],
            "last_used": 1000.0,
        }
        hit, res = cache.get_video_frames(p, 100, 1000.0)
        self.assertFalse(hit)
        self.assertIsNone(res)

    def test_cache_miss_on_all_zero_video_frames(self):
        cache = ProbeCache(cache_dir=self.cache_dir)
        p = Path("test.mp4").resolve()
        cache._data[str(p)] = {
            "size": 100,
            "mtime": 1000.0,
            "video_frames": [0, 0, 0, 0, 0, 0, 0],
            "video_flat": False,
            "last_used": 1000.0,
        }
        hit, res = cache.get_video_frames(p, 100, 1000.0)
        self.assertFalse(hit)

    def test_cache_hit_on_video_flat(self):
        cache = ProbeCache(cache_dir=self.cache_dir)
        p = Path("test.mp4").resolve()
        cache.set_video_frames(p, 100, 1000.0, None, is_flat=True)
        hit, res = cache.get_video_frames(p, 100, 1000.0)
        self.assertTrue(hit)
        self.assertIsNone(res)


if __name__ == "__main__":
    unittest.main()
