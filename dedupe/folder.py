"""Pass 1: Duplicate folders detection and removal."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Set, Tuple

from dedupe.core import Removal, TrashManager, hash_file, is_symlink_or_junction


def folder_keeper_key(root: Path, d: Path) -> Tuple[int, int, str]:
    """Keeper key: shallowest depth, shortest name, alphabetical."""
    rel = d.relative_to(root)
    return (len(rel.parts), len(d.name), rel.as_posix())


def run_folder_pass(
    root: Path,
    trash_manager: TrashManager,
    dry_run: bool = False,
    permanent: bool = False,
) -> Tuple[List[Removal], Set[Path]]:
    """Scan root for duplicate folders and remove duplicates."""
    root = root.resolve()
    removals: List[Removal] = []
    removed_folders: Set[Path] = set()

    # Discover all subdirectories, skipping _dedupe_trash, symlinks, and junctions
    all_dirs: List[Path] = []
    for dirpath, dirnames, _ in os.walk(root, followlinks=False):
        # Exclude _dedupe_trash and symlinks/junctions from descent
        dirnames[:] = [
            d
            for d in dirnames
            if d != "_dedupe_trash"
            and not is_symlink_or_junction(Path(dirpath) / d)
        ]
        curr = Path(dirpath).resolve()
        if curr != root:
            all_dirs.append(curr)

    # Compute content signature and total byte size for each directory
    signatures: Dict[Tuple[Tuple[str, str], ...], List[Path]] = {}
    folder_sizes: Dict[Path, int] = {}

    for d in all_dirs:
        file_entries: List[Tuple[str, str]] = []
        total_size = 0
        has_non_empty = False

        for dirpath, dirnames, filenames in os.walk(d, followlinks=False):
            dirnames[:] = [
                sub
                for sub in dirnames
                if sub != "_dedupe_trash"
                and not is_symlink_or_junction(Path(dirpath) / sub)
            ]
            for f in filenames:
                raw_fp = Path(dirpath) / f
                if is_symlink_or_junction(raw_fp):
                    continue
                fp = raw_fp.resolve()
                try:
                    st = fp.stat()
                except OSError:
                    continue
                size = st.st_size
                if size > 0:
                    has_non_empty = True
                total_size += size
                rel_posix = fp.relative_to(d).as_posix()
                file_hash = hash_file(fp)
                file_entries.append((rel_posix, file_hash))

        # Folder must be non-empty (contain at least one file with size > 0)
        if not has_non_empty:
            continue

        file_entries.sort()
        sig = tuple(file_entries)
        signatures.setdefault(sig, []).append(d)
        folder_sizes[d] = total_size

    # Filter duplicate groups (signatures with more than 1 directory)
    duplicate_groups = [dirs for dirs in signatures.values() if len(dirs) > 1]
    # Sort groups by shallowest folder depth to handle top-most folders first
    duplicate_groups.sort(
        key=lambda dirs: min(len(d.relative_to(root).parts) for d in dirs)
    )

    action_label = "delete" if permanent else "trash"
    if dry_run:
        action_label = f"dry-run {action_label}"

    for group in duplicate_groups:
        # Ignore directories that are inside already removed folders
        remaining = [
            d
            for d in group
            if not any(d == rf or rf in d.parents for rf in removed_folders)
        ]
        if len(remaining) < 2:
            continue

        # Select keeper: shallowest depth, shortest name, alphabetical
        keeper = min(remaining, key=lambda d: folder_keeper_key(root, d))

        for d in remaining:
            if d == keeper:
                continue
            removed_folders.add(d)
            bytes_freed = folder_sizes.get(d, 0)
            removal = Removal(
                action=action_label,
                removed_path=d,
                kept_path=keeper,
                reason="folder",
                bytes_freed=bytes_freed,
                is_dir=True,
            )
            removals.append(removal)
            trash_manager.remove_folder(d)

    return removals, removed_folders
