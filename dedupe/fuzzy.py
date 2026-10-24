"""Pass 3: Fuzzy-name duplicates detection and removal."""

from __future__ import annotations

import difflib
import os
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

from dedupe.core import Removal, TrashManager, is_symlink_or_junction

PREFIX_COPY_RE = re.compile(
    r"^(?:copy(?:\s*\(\d+\)|\s+\d+)?\s+of\s+)+", re.IGNORECASE
)

SUFFIX_COPY_RE = re.compile(
    r"(?:\s*-\s*copy(?:\s*\(\d+\)|[ _-]?\d+)?|[ _-]copy(?:\s*\(\d+\)|[ _-]?\d+)?|\s*\(\d+\))$",
    re.IGNORECASE,
)


def check_copy_marker(stem: str) -> bool:
    """Return True if stem contains a copy marker."""
    s = stem.strip()
    return bool(PREFIX_COPY_RE.search(s) or SUFFIX_COPY_RE.search(s))


def strip_copy_markers(stem: str) -> str:
    """Strip prefix and suffix copy markers from stem."""
    s = stem.strip()
    s = PREFIX_COPY_RE.sub("", s).strip()
    while True:
        m = SUFFIX_COPY_RE.search(s)
        if not m:
            break
        s = s[: m.start()].strip()
    return s


def normalize_stem(stem: str) -> str:
    """Normalize stem: lowercase, strip copy markers, collapse spaces/underscores/hyphens."""
    s = stem.lower()
    s = strip_copy_markers(s)
    s = re.sub(r"[\s_\-]+", " ", s).strip()
    return s


def differ_only_in_digits(s1: str, s2: str) -> bool:
    """Return True if s1 and s2 differ ONLY in digits."""
    clean1 = re.sub(r"[\s_\-]+", "", re.sub(r"\d+", "", s1))
    clean2 = re.sub(r"[\s_\-]+", "", re.sub(r"\d+", "", s2))
    digits1 = re.findall(r"\d+", s1)
    digits2 = re.findall(r"\d+", s2)
    return clean1 == clean2 and digits1 != digits2


def fuzzy_keeper_key(p: Path) -> Tuple[int, float, int, str]:
    """Keeper key: no copy marker (0 < 1), newest mtime (-mtime), shortest name, alphabetical."""
    has_marker = 1 if check_copy_marker(p.stem) else 0
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (has_marker, -mtime, len(p.name), p.name)


def run_fuzzy_pass(
    root: Path,
    trash_manager: TrashManager,
    removed_folders: Set[Path],
    removed_files: Set[Path],
    threshold: float = 0.9,
    dry_run: bool = False,
    permanent: bool = False,
) -> Tuple[List[Removal], Set[Path]]:
    """Scan root for fuzzy-name duplicates in the same folder with the same extension."""
    root = root.resolve()
    removals: List[Removal] = []
    fuzzy_removed: Set[Path] = set()

    # Group files by (parent_folder, extension_lowercase)
    folder_ext_groups: Dict[Tuple[Path, str], List[Path]] = {}

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
            if fp in removed_files:
                continue

            folder_ext_groups.setdefault((curr_dir, fp.suffix.lower()), []).append(fp)

    action_label = "delete" if permanent else "trash"
    if dry_run:
        action_label = f"dry-run {action_label}"

    for (parent_dir, ext), files in folder_ext_groups.items():
        if len(files) < 2:
            continue

        # Sort candidates best keeper candidate first
        candidates = sorted(files, key=fuzzy_keeper_key)

        while candidates:
            keeper = candidates.pop(0)
            keeper_norm = normalize_stem(keeper.stem)
            remaining: List[Path] = []

            for fp in candidates:
                fp_norm = normalize_stem(fp.stem)
                if differ_only_in_digits(keeper_norm, fp_norm):
                    remaining.append(fp)
                    continue

                ratio = difflib.SequenceMatcher(None, keeper_norm, fp_norm).ratio()
                if ratio >= threshold:
                    fuzzy_removed.add(fp)
                    try:
                        bytes_freed = fp.stat().st_size
                    except OSError:
                        bytes_freed = 0

                    removal = Removal(
                        action=action_label,
                        removed_path=fp,
                        kept_path=keeper,
                        reason="fuzzy",
                        bytes_freed=bytes_freed,
                        is_dir=False,
                    )
                    removals.append(removal)
                    trash_manager.remove_file(fp)
                else:
                    remaining.append(fp)

            candidates = remaining

    return removals, fuzzy_removed
