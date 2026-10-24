"""Pass 4: Torrent video duplicates by similar release names."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from dedupe.core import (
    Inventory,
    ProgressReporter,
    Removal,
    TrashManager,
    build_inventory,
)
from dedupe.fuzzy import UnionFind, extract_episode_code, extract_year_value, number_roman_signature

__all__ = [
    "VIDEO_EXTENSIONS",
    "ParsedVideoName",
    "parse_video_name",
    "is_video_file",
    "is_sample_video",
    "run_video_pass",
]

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".mpg", ".mpeg",
    ".ts", ".m2ts", ".mts", ".webm", ".flv", ".3gp", ".divx", ".vob",
}

JUNK_EXTENSIONS = {
    ".nfo", ".txt", ".jpg", ".jpeg", ".png", ".srt", ".sub", ".idx",
    ".ass", ".url", ".sfv", ".md5", ".exe", ".lnk",
}

_SINGLE_TAGS = {
    "bluray", "bdrip", "brrip", "webdl", "webrip", "web", "hdrip",
    "dvdrip", "dvdscr", "hdtv", "hdcam", "cam", "ts", "remux", "dvd",
    "x264", "x265", "h264", "h265", "hevc", "avc", "xvid", "divx",
    "10bit", "8bit", "hdr", "hdr10", "dv", "aac", "ac3", "dts",
    "atmos", "truehd", "dd5", "ddp5", "dd", "ddp",
    "extended", "unrated", "remastered", "theatrical", "proper",
    "repack", "internal", "limited", "hc", "imax",
    "hindi", "tamil", "telugu", "english", "dual", "audio", "multi",
    "esub", "esubs", "subs", "sub", "dubbed", "dub",
    "yify", "rarbg", "yts", "sample",
    "2160p", "1440p", "1080p", "720p", "576p", "480p", "360p",
    "4k", "uhd", "sd", "hd",
}

_TAG_SEQUENCES = [
    ("web", "dl"), ("web", "rip"), ("blu", "ray"),
    ("h", "264"), ("h", "265"),
    ("ddp", "5", "1"), ("dd", "5", "1"),
    ("ddp5", "1"), ("dd5", "1"), ("5", "1"), ("7", "1"),
    ("dual", "audio"), ("directors", "cut"), ("director", "s", "cut"),
    ("director", "cut"),
]

_RESOLUTION_TOKENS = {
    "2160p", "1440p", "1080p", "720p", "576p", "480p", "360p", "4k", "uhd", "sd",
}

_GENERIC_TOKENS = {
    "yts", "mx", "rarbg", "yify", "galaxyrg", "movie", "video", "film",
    "file", "download", "sample", "www", "com", "org", "net", "ag",
    "lt", "tv", "io", "me", "site", "mx", "am", "pm",
}

_KNOWN_GROUPS = {
    "yts", "rarbg", "yify", "galaxyrg", "flux", "fgt", "evo", "ntg",
    "tgx", "psa", "amzn", "nf",
}

_RES_PATTERNS = [
    (2160, re.compile(r"2160p|\b4k\b|\buhd\b")),
    (1440, re.compile(r"1440p")),
    (1080, re.compile(r"1080p")),
    (720, re.compile(r"720p")),
    (576, re.compile(r"576p")),
    (480, re.compile(r"480p")),
    (360, re.compile(r"360p")),
    (480, re.compile(r"\bsd\b")),
]


def _extract_resolution(low: str) -> int:
    best = None
    for value, pat in _RES_PATTERNS:
        m = pat.search(low)
        if m:
            cand = (m.start(), -value, value)
            if best is None or cand < best:
                best = cand
    return best[2] if best else 0


@dataclass
class ParsedVideoName:
    title: str
    year: Optional[int]
    episode: Optional[Tuple[int, int]]
    resolution: int
    title_tokens: Tuple[str, ...] = ()


def _parse_raw(name: str) -> ParsedVideoName:
    low = name.lower()
    year = extract_year_value(low)
    episode = extract_episode_code(low)
    resolution = _extract_resolution(low)
    text = low
    text = re.sub(r"^\s*www\.[^\s]+\s*-\s*", "", text)
    text = re.sub(r"^\s*\[[^\]]*\]\s*-?\s*", "", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    # Trailing -GROUP (uppercase in the original, or a known group).
    m_orig = re.search(r"-\s*([A-Za-z0-9]+)\s*$", name.strip())
    if m_orig:
        group = m_orig.group(1)
        if group.isupper() or group.lower() in _KNOWN_GROUPS:
            text = re.sub(r"-\s*[a-z0-9]+\s*$", "", text.strip())
    tokens = re.findall(r"[a-z0-9]+", text)
    title_tokens: List[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if re.fullmatch(r"(?:19|20)\d{2}", tok):
            break
        if extract_episode_code(tok) is not None:
            break
        if re.fullmatch(r"s\d{1,2}", tok) and i + 1 < len(tokens) and re.fullmatch(r"e\d{1,3}", tokens[i + 1]):
            break
        if tok in _RESOLUTION_TOKENS:
            break
        seq_hit = False
        for seq in _TAG_SEQUENCES:
            if tuple(tokens[i:i + len(seq)]) == seq:
                seq_hit = True
                break
        if seq_hit:
            break
        if tok in _SINGLE_TAGS:
            break
        title_tokens.append(tok)
        i += 1
    return ParsedVideoName(
        title=" ".join(title_tokens),
        year=year,
        episode=episode,
        resolution=resolution,
        title_tokens=tuple(title_tokens),
    )


def _is_generic(parsed: ParsedVideoName) -> bool:
    if not parsed.title_tokens:
        return True
    return all(tok in _GENERIC_TOKENS for tok in parsed.title_tokens)


def parse_video_name(stem: str, parent_name: Optional[str] = None) -> ParsedVideoName:
    """Parse a file stem; fall back to the parent folder name for generic stems."""
    parsed = _parse_raw(stem)
    if _is_generic(parsed) and parent_name:
        parent = _parse_raw(parent_name)
        if parent.title_tokens and not _is_generic(parent):
            return ParsedVideoName(
                title=parent.title,
                year=parsed.year if parsed.year is not None else parent.year,
                episode=parsed.episode if parsed.episode is not None else parent.episode,
                resolution=parsed.resolution if parsed.resolution else parent.resolution,
                title_tokens=parent.title_tokens,
            )
    return parsed


def is_video_file(path: Path) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def _stem_tokens(path: Path) -> List[str]:
    return re.findall(r"[a-z0-9]+", Path(path).stem.lower())


def is_sample_video(path: Path) -> bool:
    p = Path(path)
    if "sample" in _stem_tokens(p):
        return True
    if p.parent.name.lower() in ("sample", "samples"):
        return True
    return False


def _is_under(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _titles_guarded(a: ParsedVideoName, b: ParsedVideoName, sig_a=None, sig_b=None) -> bool:
    if a.year is not None and b.year is not None and a.year != b.year:
        return True
    if (a.episode is None) != (b.episode is None):
        return True
    if a.episode is not None and b.episode is not None and a.episode != b.episode:
        return True
    _sa = sig_a if sig_a is not None else tuple(number_roman_signature(a.title))
    _sb = sig_b if sig_b is not None else tuple(number_roman_signature(b.title))
    if _sa != _sb:
        return True
    return False


def run_video_pass(
    root: Path,
    trash_manager: TrashManager,
    removed_folders: Set[Path],
    removed_files: Set[Path],
    dry_run: bool = False,
    permanent: bool = False,
    *,
    inventory: Optional[Inventory] = None,
    progress: Optional[ProgressReporter] = None,
) -> Tuple[List[Removal], Set[Path], Set[Path]]:
    """Torrent video pass: sample, video-name duplicates, leftover folders."""
    root = root.resolve()
    removals: List[Removal] = []
    video_removed: Set[Path] = set()
    leftover_removed: Set[Path] = set()

    if progress is not None:
        progress.set_pass("video-names")
    if inventory is None:
        inventory = build_inventory(root, progress=progress)
    if progress is not None:
        progress.files_found = len(inventory.files)
        progress.total_bytes = inventory.total_bytes

    action_label = "delete" if permanent else "trash"
    if dry_run:
        action_label = f"dry-run {action_label}"

    def _gone(fp: Path) -> bool:
        if fp in removed_files or fp in video_removed:
            return True
        return any(fp == rf or rf in fp.parents for rf in removed_folders)

    videos: List[Path] = []
    for info in inventory.files:
        fp = info.path
        if _gone(fp):
            continue
        if info.size == 0:
            continue
        if not is_video_file(fp):
            continue
        videos.append(fp)
    videos.sort(key=lambda p: p.as_posix())
    parsed: Dict[Path, ParsedVideoName] = {
        fp: parse_video_name(fp.stem, fp.parent.name) for fp in videos
    }
    size_of: Dict[Path, int] = {
        info.path: info.size for info in inventory.files
    }

    def _depth(p: Path) -> int:
        try:
            return len(p.relative_to(root).parts)
        except ValueError:
            return len(p.parts)

    # --- Samples -------------------------------------------------------
    sample_keeper: Dict[Path, Path] = {}
    non_samples = [fp for fp in videos if not is_sample_video(fp)]
    for fp in videos:
        if not is_sample_video(fp):
            continue
        refs = [
            q for q in non_samples
            if q.parent == fp.parent or q.parent == fp.parent.parent
        ]
        refs = [q for q in refs if size_of.get(q, 0) > 10 * size_of.get(fp, 0)]
        if not refs:
            continue
        ref = max(refs, key=lambda q: (size_of.get(q, 0), q.as_posix()))
        sample_keeper[fp] = ref
    for fp in sorted(sample_keeper, key=lambda p: p.as_posix()):
        ref = sample_keeper[fp]
        video_removed.add(fp)
        removals.append(Removal(
            action=action_label, removed_path=fp, kept_path=ref,
            reason="sample", bytes_freed=size_of.get(fp, 0), is_dir=False,
        ))
        if progress is not None:
            progress.note_duplicate()
        trash_manager.remove_file(fp)

    # --- Video-name duplicates ----------------------------------------
    dup_keeper: Dict[Path, Path] = {}
    cands = [
        fp for fp in videos
        if fp not in video_removed and not is_sample_video(fp) and parsed[fp].title
    ]
    n = len(cands)
    _sigs = [tuple(number_roman_signature(parsed[fp].title)) for fp in cands]
    uf = UnionFind(n)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = parsed[cands[i]], parsed[cands[j]]
            if _titles_guarded(a, b, _sigs[i], _sigs[j]):
                continue
            if a.title == b.title:
                uf.union(i, j)
                continue
            la, lb = len(a.title), len(b.title)
            if la and lb and (2.0 * min(la, lb) / (la + lb)) < 0.85:
                continue
            if difflib.SequenceMatcher(None, a.title, b.title).ratio() >= 0.85:
                uf.union(i, j)
    groups: Dict[int, List[Path]] = {}
    for idx, fp in enumerate(cands):
        groups.setdefault(uf.find(idx), []).append(fp)

    def _keeper_key(p: Path):
        return (-parsed[p].resolution, -size_of.get(p, 0), _depth(p), p.as_posix())

    dup_groups = [g for g in groups.values() if len(g) >= 2]
    dup_groups.sort(key=lambda g: min(_keeper_key(p) for p in g))
    for group in dup_groups:
        keeper = min(group, key=_keeper_key)
        for fp in sorted(group, key=lambda p: p.as_posix()):
            if fp == keeper:
                continue
            dup_keeper[fp] = keeper
            video_removed.add(fp)
            removals.append(Removal(
                action=action_label, removed_path=fp, kept_path=keeper,
                reason="video-name", bytes_freed=size_of.get(fp, 0), is_dir=False,
            ))
            if progress is not None:
                progress.note_duplicate()
            trash_manager.remove_file(fp)

    # --- Leftover torrent folders --------------------------------------
    keeper_for: Dict[Path, Path] = {}
    keeper_for.update(sample_keeper)
    keeper_for.update(dup_keeper)
    candidates: List[Path] = []
    seen: Set[Path] = set()

    def _add_candidate(folder: Path) -> None:
        if folder == root or folder in seen:
            return
        seen.add(folder)
        candidates.append(folder)

    for fp in sorted(video_removed, key=lambda p: p.as_posix()):
        _add_candidate(fp.parent)
        if fp.parent.name.lower() in ("sample", "samples", "subs"):
            _add_candidate(fp.parent.parent)
    candidates.sort(key=lambda p: (_depth(p), p.as_posix()))

    all_removed_files: Set[Path] = set(removed_files) | set(video_removed)
    for folder in candidates:
        if folder == root:
            continue
        if any(folder == rf or rf in folder.parents for rf in removed_folders):
            continue
        if any(folder == lf or lf in folder.parents for lf in leftover_removed):
            continue
        remaining = []
        for info in inventory.files:
            fp = info.path
            if not _is_under(fp, folder):
                continue
            if fp in all_removed_files:
                continue
            if any(fp == rf or rf in fp.parents for rf in removed_folders):
                continue
            if any(fp == lf or lf in fp.parents for lf in leftover_removed):
                continue
            remaining.append(info)
        is_junk = True
        for info in remaining:
            fp = info.path
            if fp.suffix.lower() in JUNK_EXTENSIONS:
                continue
            if is_video_file(fp) and is_sample_video(fp):
                continue
            is_junk = False
            break
        if not is_junk:
            continue
        kept = root
        for fp in sorted(video_removed, key=lambda p: p.as_posix()):
            if fp.parent == folder or (
                fp.parent.name.lower() in ("sample", "samples", "subs")
                and fp.parent.parent == folder
            ):
                kept = keeper_for.get(fp, root)
                break
        bytes_freed = sum(info.size for info in remaining)
        leftover_removed.add(folder)
        removals.append(Removal(
            action=action_label, removed_path=folder, kept_path=kept,
            reason="torrent-leftover", bytes_freed=bytes_freed, is_dir=True,
        ))
        trash_manager.remove_folder(folder)

    if progress is not None:
        progress.end_pass("video-names")
    return removals, video_removed, leftover_removed
