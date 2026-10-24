"""Pass 2: Exact duplicate files across the whole tree."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from dedupe.core import (
    HashCache,
    Inventory,
    ProgressReporter,
    Removal,
    TrashManager,
    build_inventory,
    hash_file,
    is_symlink_or_junction,
)

__all__ = ["exact_keeper_key", "run_exact_pass", "is_symlink_or_junction", "hash_file"]


def exact_keeper_key(root: Path, p: Path) -> Tuple[int, str, float, int, str]:
    """Keeper key: shortest file name, alphabetical name, oldest mtime, shallowest depth, alphabetical path."""
    rel = p.relative_to(root)
    mtime = p.stat().st_mtime
    return (len(p.name), p.name, mtime, len(rel.parts), rel.as_posix())


def _keeper_key(root: Path, inventory: Inventory, p: Path) -> Tuple[int, str, float, int, str]:
    info = inventory.file_map.get(p)
    if info is not None:
        rel = p.relative_to(root)
        return (len(p.name), p.name, info.mtime, len(rel.parts), rel.as_posix())
    return exact_keeper_key(root, p)


def run_exact_pass(
    root: Path,
    trash_manager: TrashManager,
    removed_folders: Set[Path],
    dry_run: bool = False,
    permanent: bool = False,
    *,
    inventory: Optional[Inventory] = None,
    hash_cache: Optional[HashCache] = None,
    progress: Optional[ProgressReporter] = None,
) -> Tuple[List[Removal], Set[Path]]:
    """Scan root for exact duplicate files (same size and SHA-256) and remove duplicates."""
    root = root.resolve()
    removals: List[Removal] = []
    removed_files: Set[Path] = set()

    if progress is not None:
        progress.set_pass("exact")
    if inventory is None:
        inventory = build_inventory(root, progress=progress)
    if hash_cache is None:
        hash_cache = HashCache(inventory=inventory, hasher=hash_file, progress=progress)
    elif hash_cache.inventory is None:
        hash_cache.inventory = inventory
    if hash_cache is not None:
        hash_cache.hasher = hash_file
    if progress is not None:
        progress.files_found = len(inventory.files)
        progress.total_bytes = inventory.total_bytes

    size_groups: Dict[int, List[Path]] = {}
    for info in inventory.files:
        fp = info.path
        if any(fp == rf or rf in fp.parents for rf in removed_folders):
            continue
        if info.size == 0:
            continue
        size_groups.setdefault(info.size, []).append(fp)

    action_label = "delete" if permanent else "trash"
    if dry_run:
        action_label = f"dry-run {action_label}"

    for size, files in size_groups.items():
        if len(files) < 2:
            continue
        if progress is not None:
            progress.set_pass("exact")
        # Pre-filter: only files sharing a first block can be identical,
        # so a same-size file with a different first block is never hashed.
        block_groups: Dict[bytes, List[Path]] = {}
        for fp in files:
            try:
                with open(fp, "rb") as fh:
                    head = fh.read(8192)
            except OSError:
                continue
            block_groups.setdefault(head, []).append(fp)

        hash_groups: Dict[str, List[Path]] = {}
        for head_files in block_groups.values():
            if len(head_files) < 2:
                continue
            for fp in head_files:
                digest = hash_cache.get(fp)
                if digest is None:
                    continue
                hash_groups.setdefault(digest, []).append(fp)

        for matching_files in hash_groups.values():
            if len(matching_files) < 2:
                continue
            keeper = min(matching_files, key=lambda p: _keeper_key(root, inventory, p))
            for fp in matching_files:
                if fp == keeper:
                    continue
                removed_files.add(fp)
                removals.append(
                    Removal(
                        action=action_label,
                        removed_path=fp,
                        kept_path=keeper,
                        reason="exact",
                        bytes_freed=size,
                        is_dir=False,
                    )
                )
                if progress is not None:
                    progress.note_duplicate()
                trash_manager.remove_file(fp)

    if progress is not None:
        progress.end_pass("exact")
    return removals, removed_files
