"""Pass 1: Duplicate folders detection and removal."""

from __future__ import annotations

from collections import defaultdict
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

# Re-exported for callers that check paths before resolving them.
__all__ = ["folder_keeper_key", "run_folder_pass", "is_symlink_or_junction", "hash_file"]


def folder_keeper_key(root: Path, d: Path) -> Tuple[int, int, str]:
    """Keeper key: shallowest depth, shortest name, alphabetical."""
    rel = d.relative_to(root)
    return (len(rel.parts), len(d.name), rel.as_posix())


def run_folder_pass(
    root: Path,
    trash_manager: TrashManager,
    dry_run: bool = False,
    permanent: bool = False,
    *,
    inventory: Optional[Inventory] = None,
    hash_cache: Optional[HashCache] = None,
    progress: Optional[ProgressReporter] = None,
) -> Tuple[List[Removal], Set[Path]]:
    """Scan root for duplicate folders and remove duplicates."""
    root = root.resolve()
    removals: List[Removal] = []
    removed_folders: Set[Path] = set()

    if progress is not None:
        progress.set_pass("folder")
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

    # Build per-folder file lists from the single inventory.
    folder_files: Dict[Path, list] = defaultdict(list)
    dir_set = set(inventory.dirs)
    for info in inventory.files:
        parent = info.path.parent
        while parent != root and parent in dir_set:
            rel_posix = info.path.relative_to(parent).as_posix()
            folder_files[parent].append((rel_posix, info))
            parent = parent.parent

    # Cheap signature: sorted relative paths + sizes (no hashing yet).
    cheap_signatures: Dict[Tuple[Tuple[str, int], ...], List[Path]] = {}
    folder_sizes: Dict[Path, int] = {}
    folder_entries: Dict[Path, list] = {}

    for d in inventory.dirs:
        entries = folder_files.get(d, [])
        if not any(info.size > 0 for _, info in entries):
            continue
        folder_entries[d] = entries
        folder_sizes[d] = sum(info.size for _, info in entries)
        cheap_sig = tuple(sorted((rel, info.size) for rel, info in entries))
        cheap_signatures.setdefault(cheap_sig, []).append(d)

    # Only folders sharing a cheap signature get their files hashed.
    # Confirm incrementally: split clusters by each file's digest in turn,
    # so a folder that mismatches on an early file is not hashed further.
    duplicate_groups: List[List[Path]] = []
    for cheap_sig, dirs in cheap_signatures.items():
        if len(dirs) < 2:
            continue
        if progress is not None:
            progress.set_pass("folder")
        rels = [rel for rel, _size in cheap_sig]
        by_rel: Dict[Path, Dict[str, object]] = {
            d: {rel: info for rel, info in folder_entries[d]} for d in dirs
        }
        clusters: List[List[Path]] = [list(dirs)]
        for rel in rels:
            next_clusters: List[List[Path]] = []
            for cluster in clusters:
                if len(cluster) < 2:
                    continue
                by_digest: Dict[str, List[Path]] = {}
                for d in cluster:
                    info = by_rel[d][rel]
                    digest = hash_cache.get(info.path)
                    key = digest if digest is not None else f"size:{info.size}"
                    by_digest.setdefault(key, []).append(d)
                next_clusters.extend(g for g in by_digest.values() if len(g) > 1)
            clusters = next_clusters
            if not clusters:
                break
        duplicate_groups.extend(clusters)
    duplicate_groups.sort(
        key=lambda dirs: min(len(d.relative_to(root).parts) for d in dirs)
    )

    action_label = "delete" if permanent else "trash"
    if dry_run:
        action_label = f"dry-run {action_label}"

    for group in duplicate_groups:
        remaining = [
            d
            for d in group
            if not any(d == rf or rf in d.parents for rf in removed_folders)
        ]
        if len(remaining) < 2:
            continue
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
            if progress is not None:
                progress.note_duplicate()
            trash_manager.remove_folder(d)

    if progress is not None:
        progress.end_pass("folder")
    return removals, removed_folders
