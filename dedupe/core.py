"""Core data structures and utilities for dedupe."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
from datetime import datetime
from pathlib import Path

# Windows reparse-point attribute bit (junctions and symlinks both set it).
# Read from the DirEntry's cached stat, so detecting it costs no extra
# network round trip on a directory listing that already returned it.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass
class Removal:
    action: str
    removed_path: Path
    kept_path: Path
    reason: str  # "folder", "exact", "fuzzy"
    bytes_freed: int
    is_dir: bool = False


def is_symlink_or_junction(path: Path | str) -> bool:
    """Check if path is a symlink or Windows junction."""
    p = Path(path)
    if p.is_symlink():
        return True
    if hasattr(p, "is_junction") and p.is_junction():
        return True
    if hasattr(os.path, "isjunction") and os.path.isjunction(p):
        return True
    return False


def _entry_is_symlink_or_junction(entry: "os.DirEntry") -> bool:
    """Like ``is_symlink_or_junction``, but from a scandir ``DirEntry``.

    Every check here is served from the directory-listing metadata the
    scandir call already fetched (or, on Windows, from ``DirEntry.stat()``,
    which is itself cached from that same listing) -- never a fresh
    lstat/stat/resolve round trip per entry.
    """
    try:
        if entry.is_symlink():
            return True
    except OSError:
        return True
    is_junction = getattr(entry, "is_junction", None)
    if is_junction is not None:
        try:
            if is_junction():
                return True
        except OSError:
            return True
        return False
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def hash_file(path: Path, chunk_size: int = 65536) -> str:
    """Compute SHA-256 hash of a file in chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


_ORIGINAL_HASH_FILE = hash_file


def _resolve_hasher(explicit: Optional[Callable[[Path], str]] = None) -> Callable[[Path], str]:
    """Return the explicit hasher, or this module's ``hash_file``."""
    if explicit is not None:
        return explicit
    return globals().get("hash_file", _ORIGINAL_HASH_FILE)


class ProgressReporter:
    """Throttled progress lines on stderr (never on stdout)."""

    def __init__(self, quiet: bool = False, stream=None, interval: float = 2.0):
        self.quiet = quiet
        self._stream = stream
        self.interval = interval
        self.start_time = time.monotonic()
        self._last_status = self.start_time
        self.files_found = 0
        self.total_bytes = 0
        self.files_hashed = 0
        self.duplicates_found = 0
        self.similar_found = 0
        self.current_pass = "walk"
        self._overwrote = False
        self._write_lock = threading.Lock()

    @property
    def stream(self):
        return self._stream if self._stream is not None else sys.stderr

    def _is_tty(self) -> bool:
        try:
            return bool(self.stream.isatty())
        except Exception:
            return False

    def _write_unlocked(self, text: str) -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
        except Exception:
            pass

    def _write(self, text: str) -> None:
        # Heartbeat lines come from a background thread while the main
        # thread may be writing status/done lines; keep each line whole.
        with self._write_lock:
            self._write_unlocked(text)

    def start(self, path) -> None:
        self.start_time = time.monotonic()
        self._last_status = self.start_time
        if self.quiet:
            return
        self._write(f"Scanning {path} ...\n")

    def _format_status(self) -> str:
        return (
            f"[{self.current_pass}] {self.files_found:,} files scanned, "
            f"{self.duplicates_found:,} duplicates found"
        )

    def set_pass(self, name: str) -> None:
        with self._write_lock:
            self.current_pass = name
        self.maybe_status()

    def end_pass(self, name: str | None = None) -> None:
        with self._write_lock:
            if name is not None:
                self.current_pass = name
        self.maybe_status(force=True)

    def note_file(self, size: int = 0) -> None:
        with self._write_lock:
            self.files_found += 1
            self.total_bytes += size
        self.maybe_status()

    def note_hash(self) -> None:
        with self._write_lock:
            self.files_hashed += 1
        self.maybe_status()

    def note_duplicate(self, count: int = 1) -> None:
        with self._write_lock:
            self.duplicates_found += count
        self.maybe_status()

    def add_duplicates(self, count: int = 1) -> None:
        self.note_duplicate(count)

    def note_similar(self, count: int = 1) -> None:
        """Near-duplicates found by the similar pass, removed or only listed."""
        with self._write_lock:
            self.similar_found += count
        self.maybe_status()

    def maybe_status(self, force: bool = False) -> None:
        if self.quiet:
            return
        with self._write_lock:
            now = time.monotonic()
            if not force and (now - self._last_status) < self.interval:
                return
            self._last_status = now
            self._write_unlocked(self._format_status() + "\n")

    def done(self) -> None:
        if self.quiet:
            return
        elapsed = time.monotonic() - self.start_time
        self._overwrote = False
        with self._write_lock:
            self._write_unlocked(
                f"Done in {elapsed:.2f}s: {self.files_found:,} files scanned, "
                f"{self.duplicates_found:,} duplicates found, "
                f"{self.similar_found:,} similar found\n"
            )

    def heartbeat(self, label: str, elapsed: float) -> None:
        """A '(still ...)' line printed by HeartbeatMonitor's background thread."""
        if self.quiet:
            return
        self._write(f"(still {label}, {elapsed:.0f}s)\n")


class HeartbeatMonitor:
    """Prints '(still ...)' lines on a background thread while a single
    slow call (a directory listing on a network share, a slow pass) would
    otherwise leave the progress output silent for a long time.

    Usage: wrap the slow section with ``begin(label)``/``end()``; a daemon
    thread polls roughly every half second and emits a line through the
    reporter at most once per ``interval`` seconds for the active label.
    """

    def __init__(self, progress: ProgressReporter, interval: float = 2.5, poll: float = 0.5):
        self.progress = progress
        self.interval = interval
        self.poll = poll
        self._lock = threading.Lock()
        self._label: Optional[str] = None
        self._started_at: Optional[float] = None
        self._next_due: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.progress.quiet or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def begin(self, label: str) -> None:
        with self._lock:
            self._label = label
            self._started_at = time.monotonic()
            self._next_due = self._started_at + self.interval

    def end(self) -> None:
        with self._lock:
            self._label = None
            self._started_at = None
            self._next_due = None

    def _loop(self) -> None:
        while not self._stop.wait(self.poll):
            with self._lock:
                label, started, due = self._label, self._started_at, self._next_due
                now = time.monotonic()
                if label is None or started is None or due is None or now < due:
                    continue
                self._next_due = now + self.interval
            self.progress.heartbeat(label, now - started)


@dataclass
class FileInfo:
    path: Path
    size: int
    mtime: float


@dataclass
class Inventory:
    root: Path
    files: List[FileInfo] = field(default_factory=list)
    dirs: List[Path] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.file_map: Dict[Path, FileInfo] = {f.path: f for f in self.files}
        self.nonempty_size_counts: Counter = Counter(
            f.size for f in self.files if f.size > 0
        )
        self.total_bytes: int = sum(f.size for f in self.files)


def build_inventory(
    root: Path,
    progress: Optional[ProgressReporter] = None,
    heartbeat: Optional["HeartbeatMonitor"] = None,
) -> Inventory:
    """Walk the tree once, collecting (path, size, mtime) for every file.

    Uses the directory-listing metadata from ``os.scandir`` (the DirEntry's
    cached attributes/stat) instead of a per-file lstat/stat/resolve, so a
    slow network share pays for one round trip per directory, not several
    per file. The root is made absolute once; every child path is built by
    joining onto it, never by resolving.
    """
    root = Path(os.path.abspath(os.fspath(root)))
    files: List[FileInfo] = []
    dirs: List[Path] = []
    if progress is not None:
        progress.current_pass = "walk"

    stack: List[Path] = [root]
    while stack:
        current = stack.pop()
        rel = "." if current == root else current.relative_to(root).as_posix()
        if heartbeat is not None:
            heartbeat.begin(f"listing {rel}")
        try:
            entries = list(os.scandir(current))
        except OSError:
            # Unreadable folder (permissions, a dropped network share, ...):
            # skip it and keep walking the rest of the tree.
            continue
        finally:
            if heartbeat is not None:
                heartbeat.end()

        for entry in entries:
            if entry.name == "_dedupe_trash":
                continue
            if _entry_is_symlink_or_junction(entry):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                child = Path(entry.path)
                dirs.append(child)
                stack.append(child)
            else:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                fp = Path(entry.path)
                files.append(FileInfo(path=fp, size=st.st_size, mtime=st.st_mtime))
                if progress is not None:
                    progress.note_file(st.st_size)

    inv = Inventory(root=root, files=files, dirs=dirs)
    if progress is not None:
        # Inventory totals are authoritative even if no status line was emitted.
        progress.files_found = len(files)
        progress.total_bytes = inv.total_bytes
        progress.end_pass("walk")
    return inv


_GLOBAL_HASH_CACHE: Dict[tuple, str] = {}


class HashCache:
    """Lazy shared hash cache: each file hashed at most once per run."""

    def __init__(
        self,
        inventory: Optional[Inventory] = None,
        hasher: Optional[Callable[[Path], str]] = None,
        progress: Optional[ProgressReporter] = None,
    ):
        self.inventory = inventory
        self.hasher = hasher
        self.progress = progress
        self._cache: Dict[Path, str] = {}

    @property
    def hashed_count(self) -> int:
        return len(self._cache)

    def __contains__(self, path: Path) -> bool:
        return Path(path) in self._cache

    def get(self, path: Path) -> Optional[str]:
        p = Path(path)
        if p in self._cache:
            return self._cache[p]
        _info = self.inventory.file_map.get(p) if self.inventory is not None else None
        _key = None
        if _info is not None:
            _key = (p, _info.size, _info.mtime)
            if _key in _GLOBAL_HASH_CACHE:
                digest = _GLOBAL_HASH_CACHE[_key]
                self._cache[p] = digest
                return digest
        if self.inventory is not None:
            info = self.inventory.file_map.get(p)
            if info is not None and info.size == 0:
                digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
                self._cache[p] = digest
                return digest
            if info is not None and info.size > 0:
                if self.inventory.nonempty_size_counts.get(info.size, 0) <= 1:
                    return None
        hasher = self.hasher if self.hasher is not None else globals().get("hash_file", _ORIGINAL_HASH_FILE)
        digest = hasher(p)
        self._cache[p] = digest
        if _key is not None:
            _GLOBAL_HASH_CACHE[_key] = digest
        if self.progress is not None:
            self.progress.note_hash()
        return digest


class TrashManager:
    """Manages moving duplicate items to _dedupe_trash or deleting permanently."""

    def __init__(self, root: Path, dry_run: bool = False, permanent: bool = False):
        self.root = root.resolve()
        self.dry_run = dry_run
        self.permanent = permanent
        self.trash_base = self.root / "_dedupe_trash"
        self._trash_session_dir: Path | None = None

    def get_trash_session_dir(self) -> Path:
        """Get or lazily create the session trash folder."""
        if self._trash_session_dir is None:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self._trash_session_dir = self.trash_base / timestamp
            self._trash_session_dir.mkdir(parents=True, exist_ok=True)
        return self._trash_session_dir

    def remove_file(self, file_path: Path) -> None:
        """Delete file or move it to trash relative to root."""
        if self.dry_run:
            return
        if self.permanent:
            file_path.unlink()
        else:
            rel = file_path.resolve().relative_to(self.root)
            dest = self.get_trash_session_dir() / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(file_path), str(dest))

    def remove_folder(self, folder_path: Path) -> None:
        """Delete directory or move it to trash relative to root."""
        if self.dry_run:
            return
        if self.permanent:
            shutil.rmtree(folder_path)
        else:
            rel = folder_path.resolve().relative_to(self.root)
            dest = self.get_trash_session_dir() / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(folder_path), str(dest))
