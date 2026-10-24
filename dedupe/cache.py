"""Probe cache between runs for ffprobe/ffmpeg, image dHash and document shingles."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

MAX_CACHE_ENTRIES = 200_000
CACHE_VERSION = 2


def default_cache_dir() -> Path:
    """Return default probe cache directory: %LOCALAPPDATA%\\dedupe\\cache or ~/.cache/dedupe/."""
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return Path(local_app) / "dedupe" / "cache"
        return Path.home() / "AppData" / "Local" / "dedupe" / "cache"
    return Path.home() / ".cache" / "dedupe"


class ProbeCache:
    """Persistent local cache for video, image and document probes."""

    def __init__(
        self,
        cache_dir: Optional[Path | str] = None,
        no_cache: bool = False,
    ) -> None:
        self.no_cache = no_cache
        self._lock = threading.Lock()
        self._dirty = False
        self._data: Dict[str, dict] = {}
        self.cache_dir: Optional[Path] = None
        self.cache_file: Optional[Path] = None

        if self.no_cache:
            return

        if cache_dir is not None:
            self.cache_dir = Path(cache_dir).resolve()
        else:
            self.cache_dir = default_cache_dir().resolve()

        self.cache_file = self.cache_dir / "probe_cache.json"
        self._load()

    def _load(self) -> None:
        if self.no_cache or self.cache_file is None:
            return
        if not self.cache_file.exists():
            return
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and raw.get("_version") == CACHE_VERSION:
                self._data = raw
            else:
                self._data = {"_version": CACHE_VERSION}
        except Exception:
            # A missing, unreadable or corrupt cache is ignored and rebuilt;
            # never crash and never print a traceback.
            self._data = {"_version": CACHE_VERSION}

    def _get_entry(self, path: Path, size: int, mtime: float) -> Optional[dict]:
        key = str(Path(path).resolve())
        with self._lock:
            entry = self._data.get(key)
            if entry is None or not isinstance(entry, dict):
                return None
            if entry.get("size") != size:
                return None
            entry_mtime = entry.get("mtime")
            if entry_mtime is None or abs(float(entry_mtime) - float(mtime)) > 1e-4:
                return None
            return entry

    def _ensure_entry(self, path: Path, size: int, mtime: float) -> dict:
        key = str(Path(path).resolve())
        entry = self._data.get(key)
        if (
            entry is None
            or not isinstance(entry, dict)
            or entry.get("size") != size
            or entry.get("mtime") is None
            or abs(float(entry.get("mtime")) - float(mtime)) > 1e-4
        ):
            entry = {
                "size": size,
                "mtime": mtime,
                "last_used": time.time(),
            }
            self._data[key] = entry
        else:
            entry["last_used"] = time.time()
        return entry

    # --- Videos --------------------------------------------------------

    def get_video_probe(
        self, path: Path, size: int, mtime: float
    ) -> Tuple[bool, Optional[Tuple[float, int, int]]]:
        """Return (hit, result). If hit, result is (duration, width, height) or None."""
        if self.no_cache:
            return False, None
        try:
            entry = self._get_entry(path, size, mtime)
            if entry is None:
                return False, None
            if entry.get("video_unprobeable"):
                return True, None
            if "video_probe" in entry:
                vp = entry["video_probe"]
                if vp is None:
                    return True, None
                return True, (float(vp[0]), int(vp[1]), int(vp[2]))
            return False, None
        except Exception:
            return False, None

    def set_video_probe(
        self,
        path: Path,
        size: int,
        mtime: float,
        result: Optional[Tuple[float, int, int]],
    ) -> None:
        if self.no_cache:
            return
        try:
            with self._lock:
                entry = self._ensure_entry(path, size, mtime)
                if result is None:
                    entry["video_unprobeable"] = True
                    entry["video_probe"] = None
                else:
                    entry["video_probe"] = [result[0], result[1], result[2]]
                self._dirty = True
        except Exception:
            pass

    def get_video_frames(
        self, path: Path, size: int, mtime: float
    ) -> Tuple[bool, Optional[List[Optional[int]]]]:
        """Return (hit, result). If hit, result is list of frame hashes (or None)."""
        if self.no_cache:
            return False, None
        try:
            entry = self._get_entry(path, size, mtime)
            if entry is None:
                return False, None
            if entry.get("video_frames_unreadable") or entry.get("video_flat"):
                return True, None
            if "video_flat" not in entry and "video_frames" in entry:
                return False, None
            if "video_frames" in entry:
                raw = entry["video_frames"]
                if raw is None:
                    return True, None
                hashes = [int(h) if h is not None else None for h in raw]
                non_none = [h for h in hashes if h is not None]
                if non_none and all(h == 0 for h in non_none):
                    return False, None
                return True, hashes
            return False, None
        except Exception:
            return False, None

    def set_video_frames(
        self,
        path: Path,
        size: int,
        mtime: float,
        result: Optional[List[Optional[int]]],
        is_flat: bool = False,
    ) -> None:
        if self.no_cache:
            return
        try:
            with self._lock:
                entry = self._ensure_entry(path, size, mtime)
                entry["video_flat"] = is_flat
                if is_flat:
                    entry["video_frames"] = None
                elif result is None:
                    entry["video_frames_unreadable"] = True
                    entry["video_frames"] = None
                else:
                    entry["video_frames"] = [int(h) if h is not None else None for h in result]
                self._dirty = True
        except Exception:
            pass

    # --- Images --------------------------------------------------------

    def get_image(
        self, path: Path, size: int, mtime: float
    ) -> Tuple[bool, Optional[Tuple[int, int]]]:
        if self.no_cache:
            return False, None
        try:
            entry = self._get_entry(path, size, mtime)
            if entry is None:
                return False, None
            if entry.get("image_unreadable") or entry.get("image_flat"):
                return True, None
            if "image_flat" not in entry and "image" in entry:
                return False, None
            if "image" in entry:
                val = entry["image"]
                if val is None:
                    return True, None
                h = int(val[0])
                if h == 0 or h == (1 << 64) - 1:
                    return False, None
                return True, (h, int(val[1]))
            return False, None
        except Exception:
            return False, None

    def set_image(
        self,
        path: Path,
        size: int,
        mtime: float,
        result: Optional[Tuple[int, int]],
        is_flat: bool = False,
    ) -> None:
        if self.no_cache:
            return
        try:
            with self._lock:
                entry = self._ensure_entry(path, size, mtime)
                entry["image_flat"] = is_flat
                if is_flat:
                    entry["image"] = None
                elif result is None:
                    entry["image_unreadable"] = True
                    entry["image"] = None
                else:
                    entry["image"] = [int(result[0]), int(result[1])]
                self._dirty = True
        except Exception:
            pass

    # --- Documents -----------------------------------------------------

    def get_document_shingles(
        self, path: Path, size: int, mtime: float
    ) -> Tuple[bool, Optional[FrozenSet[int]]]:
        if self.no_cache:
            return False, None
        try:
            entry = self._get_entry(path, size, mtime)
            if entry is None:
                return False, None
            if entry.get("doc_unreadable"):
                return True, None
            if "doc_shingles" in entry:
                val = entry["doc_shingles"]
                if val is None:
                    return True, None
                return True, frozenset(int(x) for x in val)
            return False, None
        except Exception:
            return False, None

    def set_document_shingles(
        self,
        path: Path,
        size: int,
        mtime: float,
        result: Optional[FrozenSet[int]],
    ) -> None:
        if self.no_cache:
            return
        try:
            with self._lock:
                entry = self._ensure_entry(path, size, mtime)
                if result is None:
                    entry["doc_unreadable"] = True
                    entry["doc_shingles"] = None
                else:
                    if len(result) <= 20_000:
                        entry["doc_shingles"] = [int(x) for x in result]
                self._dirty = True
        except Exception:
            pass

    # --- Persistence ---------------------------------------------------

    def save(self) -> None:
        if self.no_cache or not self._dirty or self.cache_dir is None or self.cache_file is None:
            return
        tmp_path = None
        try:
            with self._lock:
                entry_keys = [
                    k for k in self._data.keys()
                    if k != "_version" and isinstance(self._data[k], dict)
                ]
                if len(entry_keys) > MAX_CACHE_ENTRIES:
                    sorted_keys = sorted(
                        entry_keys,
                        key=lambda k: self._data[k].get("last_used", 0.0),
                    )
                    to_remove = len(entry_keys) - MAX_CACHE_ENTRIES
                    for k in sorted_keys[:to_remove]:
                        del self._data[k]

                self._data["_version"] = CACHE_VERSION
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                fd, tmp_path = tempfile.mkstemp(
                    prefix="probe_cache_",
                    suffix=".tmp",
                    dir=str(self.cache_dir),
                )
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self._data, f)
                os.replace(tmp_path, str(self.cache_file))
                tmp_path = None
                self._dirty = False
        except Exception:
            pass
        finally:
            if tmp_path is not None:
                try:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                except Exception:
                    pass
