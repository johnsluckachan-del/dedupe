"""Command line interface for dedupe."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List

from dedupe.core import HashCache, HeartbeatMonitor, ProgressReporter, Removal, TrashManager, build_inventory
from dedupe.exact import run_exact_pass
from dedupe.folder import run_folder_pass
from dedupe.fuzzy import run_fuzzy_pass
from dedupe.video import run_video_pass


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dedupe",
        description="Dedupe CLI: delete duplicate files/folders with fuzzy-name matching",
    )
    parser.add_argument(
        "path",
        help="Target folder path to scan and deduplicate",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and change nothing",
    )
    parser.add_argument(
        "--permanent",
        action="store_true",
        help="Delete duplicates permanently instead of moving to trash",
    )
    parser.add_argument(
        "--no-fuzzy",
        action="store_true",
        help="Skip fuzzy-name duplicate matching",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.9,
        help="Fuzzy matching ratio threshold (default: 0.9)",
    )
    parser.add_argument(
        "--min-match",
        type=int,
        default=10,
        help="Minimum common substring length in normalized stems for fuzzy matching (default: 10; 0 disables)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output on stderr",
    )
    return parser.parse_args(argv)


def run_dedupe(
    target_path: Path | str,
    dry_run: bool = False,
    permanent: bool = False,
    no_fuzzy: bool = False,
    threshold: float = 0.9,
    min_match: int = 10,
    quiet: bool = False,
) -> int:
    progress = ProgressReporter(quiet=quiet)
    progress.start(target_path)

    root = Path(target_path).resolve()
    if not root.exists():
        sys.stderr.write(f"Error: path '{target_path}' does not exist\n")
        return 1
    if not root.is_dir():
        sys.stderr.write(f"Error: path '{target_path}' is not a directory\n")
        return 1

    trash_manager = TrashManager(root=root, dry_run=dry_run, permanent=permanent)
    all_removals: List[Removal] = []

    heartbeat = HeartbeatMonitor(progress)
    heartbeat.start()
    try:
        # Single walk: build the inventory once for all passes.
        inventory = build_inventory(root, progress=progress, heartbeat=heartbeat)
        hash_cache = HashCache(inventory=inventory, progress=progress)

        # Pass 1: Duplicate folders
        progress.set_pass("folder")
        heartbeat.begin("working on folder")
        folder_removals, removed_folders = run_folder_pass(
            root=root,
            trash_manager=trash_manager,
            dry_run=dry_run,
            permanent=permanent,
            inventory=inventory,
            hash_cache=hash_cache,
            progress=progress,
        )
        heartbeat.end()
        all_removals.extend(folder_removals)

        # Pass 2: Exact duplicate files across the whole tree
        progress.set_pass("exact")
        heartbeat.begin("working on exact")
        exact_removals, removed_files = run_exact_pass(
            root=root,
            trash_manager=trash_manager,
            removed_folders=removed_folders,
            dry_run=dry_run,
            permanent=permanent,
            inventory=inventory,
            hash_cache=hash_cache,
            progress=progress,
        )
        heartbeat.end()
        all_removals.extend(exact_removals)

        # Pass 3: Fuzzy-name duplicates in same folder with same extension
        if not no_fuzzy:
            progress.set_pass("fuzzy")
            heartbeat.begin("working on fuzzy")
            fuzzy_removals, fuzzy_removed = run_fuzzy_pass(
                root=root,
                trash_manager=trash_manager,
                removed_folders=removed_folders,
                removed_files=removed_files,
                threshold=threshold,
                min_match=min_match,
                dry_run=dry_run,
                permanent=permanent,
                inventory=inventory,
                progress=progress,
            )
            heartbeat.end()
            all_removals.extend(fuzzy_removals)
            removed_files = removed_files | fuzzy_removed
            progress.set_pass("video-names")
            heartbeat.begin("working on video-names")
            video_removals, video_removed, _leftover = run_video_pass(root=root, trash_manager=trash_manager, removed_folders=removed_folders, removed_files=removed_files, dry_run=dry_run, permanent=permanent, inventory=inventory, progress=progress)
            heartbeat.end()
            all_removals.extend(video_removals)
    finally:
        heartbeat.stop()

    # Print one line per removal: action, removed path, kept path, reason
    for r in all_removals:
        try:
            rel_removed = r.removed_path.relative_to(root)
        except ValueError:
            rel_removed = r.removed_path
        try:
            rel_kept = r.kept_path.relative_to(root)
        except ValueError:
            rel_kept = r.kept_path

        print(f"{r.action}: {rel_removed} (kept: {rel_kept}, reason: {r.reason})")

    # Summary
    folder_count = sum(1 for r in all_removals if r.reason == "folder")
    exact_count = sum(1 for r in all_removals if r.reason == "exact")
    fuzzy_count = sum(1 for r in all_removals if r.reason == "fuzzy")
    video_count = sum(1 for r in all_removals if r.reason == "video-name")
    sample_count = sum(1 for r in all_removals if r.reason == "sample")
    leftover_count = sum(1 for r in all_removals if r.reason == "torrent-leftover")
    video_summary_extra = f", {video_count} video-name, {sample_count} sample, {leftover_count} torrent-leftover" if (video_count or sample_count or leftover_count) else ""
    total_count = len(all_removals)
    total_bytes = sum(r.bytes_freed for r in all_removals)

    action_word = "would be removed" if dry_run else "removed"
    print(
        f"Summary: {total_count} items {action_word} "
        f"({folder_count} folder, {exact_count} exact, {fuzzy_count} fuzzy{video_summary_extra}), "
        f"{total_bytes} bytes freed"
    )

    progress.done()
    return 0


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv)
    return run_dedupe(
        target_path=args.path,
        dry_run=args.dry_run,
        permanent=args.permanent,
        no_fuzzy=args.no_fuzzy,
        threshold=args.threshold,
        min_match=args.min_match,
        quiet=args.quiet,
    )
