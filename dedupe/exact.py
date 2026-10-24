"""Pass 2: Exact duplicate files across the whole tree."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Set, Tuple

from dedupe.core import Removal, TrashManager, hash_file, is_symlink_or_junction


def exact_keeper_key(root: Path, p: Path) -> Tuple[int, int, float, str]:
    """Keeper key: shallowest depth, shortest file name, oldest mtime, alphabetical."""
    rel = p.relative_to(root)
    mtime = p.stat().st_mtime
    return (len(rel.parts), len(p.name), mtime, rel.as_posix())


def run_exact_pass(
    root: Path,
    trash_manager: TrashManager,
    removed_folders: Set[Path],
    dry_run: bool = False,
    permanent: bool = False,
) -> Tuple[List[Removal], Set[Path]]:
    """Scan root for exact duplicate files (same size and SHA-256) and remove duplicates."""
    root = root.resolve()
    removals: List[Removal] = []
    removed_files: Set[Path] = set()

    # Discover files, grouping first by file size
    size_groups: Dict[int, List[Path]] = {}

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            d
            for d in dirnames
            if d != "_dedupe_trash"
            and not is_symlink_or_junction(Path(dirpath) / d)
        ]
        curr_dir = Path(dirpath).resolve()
        if any(curr_dir == rf or rf in curr_dir.parents for rf in removed_folders):
            continue

        for f in filenames:
            raw_fp = curr_dir / f
            if is_symlink_or_junction(raw_fp):
                continue
            fp = raw_fp.resolve()
            if any(fp == rf or rf in fp.parents for rf in removed_folders):
                continue

            try:
                st = fp.stat()
            except OSError:
                continue

            # Skip zero-byte files
            if st.st_size == 0:
                continue

            size_groups.setdefault(st.st_size, []).append(fp)

    action_label = "delete" if permanent else "trash"
    if dry_run:
        action_label = f"dry-run {action_label}"

    # For each size group with multiple files, compare SHA-256 hashes
    for size, files in size_groups.items():
        if len(files) < 2:
            continue

        hash_groups: Dict[str, List[Path]] = {}
        for fp in files:
            file_hash = hash_file(fp)
            hash_groups.setdefault(file_hash, []).append(fp)

        for h, matching_files in hash_groups.items():
            if len(matching_files) < 2:
                continue

            keeper = min(matching_files, key=lambda p: exact_keeper_key(root, p))

            for fp in matching_files:
                if fp == keeper:
                    continue
                removed_files.add(fp)
                removal = Removal(
                    action=action_label,
                    removed_path=fp,
                    kept_path=keeper,
                    reason="exact",
                    bytes_freed=size,
                    is_dir=False,
                )
                removals.append(removal)
                trash_manager.remove_file(fp)

    return removals, removed_files
