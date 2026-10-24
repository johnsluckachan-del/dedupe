"""Command line interface for dedupe."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List

from dedupe.core import Removal, TrashManager
from dedupe.exact import run_exact_pass
from dedupe.folder import run_folder_pass
from dedupe.fuzzy import run_fuzzy_pass


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
    return parser.parse_args(argv)


def run_dedupe(
    target_path: Path | str,
    dry_run: bool = False,
    permanent: bool = False,
    no_fuzzy: bool = False,
    threshold: float = 0.9,
) -> int:
    root = Path(target_path).resolve()
    if not root.exists():
        sys.stderr.write(f"Error: path '{target_path}' does not exist\n")
        return 1
    if not root.is_dir():
        sys.stderr.write(f"Error: path '{target_path}' is not a directory\n")
        return 1

    trash_manager = TrashManager(root=root, dry_run=dry_run, permanent=permanent)
    all_removals: List[Removal] = []

    # Pass 1: Duplicate folders
    folder_removals, removed_folders = run_folder_pass(
        root=root,
        trash_manager=trash_manager,
        dry_run=dry_run,
        permanent=permanent,
    )
    all_removals.extend(folder_removals)

    # Pass 2: Exact duplicate files across the whole tree
    exact_removals, removed_files = run_exact_pass(
        root=root,
        trash_manager=trash_manager,
        removed_folders=removed_folders,
        dry_run=dry_run,
        permanent=permanent,
    )
    all_removals.extend(exact_removals)

    # Pass 3: Fuzzy-name duplicates in same folder with same extension
    if not no_fuzzy:
        fuzzy_removals, _ = run_fuzzy_pass(
            root=root,
            trash_manager=trash_manager,
            removed_folders=removed_folders,
            removed_files=removed_files,
            threshold=threshold,
            dry_run=dry_run,
            permanent=permanent,
        )
        all_removals.extend(fuzzy_removals)

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
    total_count = len(all_removals)
    total_bytes = sum(r.bytes_freed for r in all_removals)

    action_word = "would be removed" if dry_run else "removed"
    print(
        f"Summary: {total_count} items {action_word} "
        f"({folder_count} folder, {exact_count} exact, {fuzzy_count} fuzzy), "
        f"{total_bytes} bytes freed"
    )

    return 0


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv)
    return run_dedupe(
        target_path=args.path,
        dry_run=args.dry_run,
        permanent=args.permanent,
        no_fuzzy=args.no_fuzzy,
        threshold=args.threshold,
    )
