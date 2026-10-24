"""Pass 3: Fuzzy-name duplicates detection and removal."""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from dedupe.core import (
    Inventory,
    ProgressReporter,
    Removal,
    TrashManager,
    build_inventory,
    is_symlink_or_junction,
)

__all__ = [
    "check_copy_marker",
    "strip_copy_markers",
    "normalize_stem",
    "differ_only_in_digits",
    "fuzzy_keeper_key",
    "UnionFind",
    "run_fuzzy_pass",
    "is_symlink_or_junction", "extract_episode_code", "extract_year_value", "number_roman_signature", "sequel_episode_guard",
]

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
    """Normalize stem: lowercase, strip copy markers, collapse runs of 3+ identical
    characters, collapse spaces/underscores/hyphens."""
    s = stem.lower()
    s = strip_copy_markers(s)
    s = re.sub(r"(.)\1{2,}", r"\1", s)
    s = re.sub(r"[\s_\-]+", " ", s).strip()
    return s


def differ_only_in_digits(s1: str, s2: str) -> bool:
    """Return True if s1 and s2 differ ONLY in digits."""
    clean1 = re.sub(r"[\s_\-]+", "", re.sub(r"\d+", "", s1))
    clean2 = re.sub(r"[\s_\-]+", "", re.sub(r"\d+", "", s2))
    digits1 = re.findall(r"\d+", s1)
    digits2 = re.findall(r"\d+", s2)
    return clean1 == clean2 and digits1 != digits2


ROMAN_VALUES = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7,
    "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12, "xiii": 13,
    "xiv": 14, "xv": 15, "xvi": 16, "xvii": 17, "xviii": 18, "xix": 19, "xx": 20,
}

_EPISODE_SE_RE = re.compile(r"s\s*(\d{1,2})\s*e\s*(\d{1,3})", re.IGNORECASE)
_EPISODE_X_RE = re.compile(r"\b(\d{1,2})x(\d{1,3})\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")


def extract_episode_code(text: str):
    """Return (season, episode) ints from S01E05 / S01 E05 / 1x05, else None."""
    low = text.lower()
    m = _EPISODE_SE_RE.search(low)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = _EPISODE_X_RE.search(low)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


def extract_year_value(text: str):
    """Return first 19xx/20xx year in text, else None."""
    m = _YEAR_RE.search(text)
    return int(m.group(1)) if m else None


def number_roman_signature(text: str):
    """Ordered numeric/roman values in text (sequel numbers, years, resolutions)."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    sig = []
    for tok in tokens:
        if _EPISODE_SE_RE.fullmatch(tok) or _EPISODE_X_RE.fullmatch(tok):
            continue
        m = re.fullmatch(r"(?:vol|part|cd|disc|disk)(\d+)", tok)
        if m:
            sig.append(int(m.group(1)))
            continue
        if tok.isdigit():
            sig.append(int(tok))
            continue
        m = re.fullmatch(r"(\d+)p", tok)
        if m:
            sig.append(int(m.group(1)))
            continue
        if tok in ROMAN_VALUES:
            sig.append(ROMAN_VALUES[tok])
            continue
    return sig


def _guard_key(s: str):
    return (extract_year_value(s), extract_episode_code(s), tuple(number_roman_signature(s)))


def _guarded_keys(k1, k2) -> bool:
    if k1[0] is not None and k2[0] is not None and k1[0] != k2[0]:
        return True
    if (k1[1] is None) != (k2[1] is None):
        return True
    if k1[1] is not None and k2[1] is not None and k1[1] != k2[1]:
        return True
    return k1[2] != k2[2]


def sequel_episode_guard(s1: str, s2: str) -> bool:
    """True when year/episode/number-roman guards forbid treating s1/s2 as duplicates."""
    y1, y2 = extract_year_value(s1), extract_year_value(s2)
    if y1 is not None and y2 is not None and y1 != y2:
        return True
    e1, e2 = extract_episode_code(s1), extract_episode_code(s2)
    if (e1 is None) != (e2 is None):
        return True
    if e1 is not None and e2 is not None and e1 != e2:
        return True
    if number_roman_signature(s1) != number_roman_signature(s2):
        return True
    return False


def fuzzy_keeper_key(p: Path) -> Tuple[int, float, int, str]:
    """Keeper key: no copy marker (0 < 1), newest mtime (-mtime), shortest name, alphabetical."""
    has_marker = 1 if check_copy_marker(p.stem) else 0
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (has_marker, -mtime, len(p.name), p.name)


def _fuzzy_key(inventory: Optional[Inventory], p: Path) -> Tuple[int, float, int, str]:
    if inventory is not None:
        info = inventory.file_map.get(p)
        if info is not None:
            has_marker = 1 if check_copy_marker(p.stem) else 0
            return (has_marker, -info.mtime, len(p.name), p.name)
    return fuzzy_keeper_key(p)


class UnionFind:
    """Disjoint-set data structure with path compression."""

    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        root = i
        while self.parent[root] != root:
            root = self.parent[root]
        curr = i
        while curr != root:
            nxt = self.parent[curr]
            self.parent[curr] = root
            curr = nxt
        return root

    def union(self, i: int, j: int) -> None:
        root_i = self.find(i)
        root_j = self.find(j)
        if root_i != root_j:
            self.parent[root_i] = root_j


def run_fuzzy_pass(
    root: Path,
    trash_manager: TrashManager,
    removed_folders: Set[Path],
    removed_files: Set[Path],
    threshold: float = 0.9,
    min_match: int = 10,
    dry_run: bool = False,
    permanent: bool = False,
    *,
    inventory: Optional[Inventory] = None,
    progress: Optional[ProgressReporter] = None,
) -> Tuple[List[Removal], Set[Path]]:
    """Scan root for fuzzy-name duplicates in the same folder with the same extension."""
    root = root.resolve()
    removals: List[Removal] = []
    fuzzy_removed: Set[Path] = set()

    if progress is not None:
        progress.set_pass("fuzzy")
    if inventory is None:
        inventory = build_inventory(root, progress=progress)
    if progress is not None:
        progress.files_found = len(inventory.files)
        progress.total_bytes = inventory.total_bytes

    folder_ext_groups: Dict[Tuple[Path, str], List[Path]] = {}
    for info in inventory.files:
        fp = info.path
        if any(fp == rf or rf in fp.parents for rf in removed_folders):
            continue
        if fp in removed_files:
            continue
        if info.size == 0:
            continue
        folder_ext_groups.setdefault((fp.parent, fp.suffix.lower()), []).append(fp)

    action_label = "delete" if permanent else "trash"
    if dry_run:
        action_label = f"dry-run {action_label}"

    for (parent_dir, ext), files in folder_ext_groups.items():
        if len(files) < 2:
            continue
        n = len(files)
        uf = UnionFind(n)
        norm_stems = [normalize_stem(fp.stem) for fp in files]
        guard_keys = [_guard_key(s) for s in norm_stems]
        for i in range(n):
            for j in range(i + 1, n):
                s1 = norm_stems[i]
                s2 = norm_stems[j]
                if differ_only_in_digits(s1, s2) or _guarded_keys(guard_keys[i], guard_keys[j]):
                    continue
                sm = difflib.SequenceMatcher(None, s1, s2)
                is_match = False
                if threshold is not None and sm.ratio() >= threshold:
                    is_match = True
                elif min_match is not None and min_match > 0:
                    longest = sm.find_longest_match(0, len(s1), 0, len(s2))
                    if longest.size >= min_match:
                        is_match = True
                if is_match:
                    uf.union(i, j)

        groups: Dict[int, List[Path]] = {}
        for idx, fp in enumerate(files):
            root_idx = uf.find(idx)
            groups.setdefault(root_idx, []).append(fp)

        duplicate_groups = [g for g in groups.values() if len(g) >= 2]
        duplicate_groups.sort(
            key=lambda g: _fuzzy_key(inventory, min(g, key=lambda p: _fuzzy_key(inventory, p)))
        )

        for group_files in duplicate_groups:
            sorted_group = sorted(group_files, key=lambda p: _fuzzy_key(inventory, p))
            keeper = sorted_group[0]
            for fp in sorted_group[1:]:
                fuzzy_removed.add(fp)
                info = inventory.file_map.get(fp)
                bytes_freed = info.size if info is not None else 0
                if info is None:
                    try:
                        bytes_freed = fp.stat().st_size
                    except OSError:
                        bytes_freed = 0
                removals.append(
                    Removal(
                        action=action_label,
                        removed_path=fp,
                        kept_path=keeper,
                        reason="fuzzy",
                        bytes_freed=bytes_freed,
                        is_dir=False,
                    )
                )
                if progress is not None:
                    progress.note_duplicate()
                trash_manager.remove_file(fp)

    if progress is not None:
        progress.end_pass("fuzzy")
    return removals, fuzzy_removed
