"""Core data structures and utilities for dedupe."""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


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


def hash_file(path: Path, chunk_size: int = 65536) -> str:
    """Compute SHA-256 hash of a file in chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


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
