"""Pass 4: near-duplicate images, documents and videos (the "similar" pass).

Everything here runs on this machine only: a perceptual difference hash
(dHash) for images through Pillow, shingled MinHash/LSH over extracted
text for documents, and local ffmpeg/ffprobe frame grabs hashed the same
way (dHash) for videos. No network calls, no services, no API keys -- the
pass only ever opens local files and spawns ffmpeg/ffprobe.

Near-duplicates are removed like any other duplicate by default; the caller
can ask for a listing instead (``review=True``), in which case nothing from
this pass moves.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import importlib
import os
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from dedupe.core import (
    Inventory,
    ProgressReporter,
    Removal,
    TrashManager,
    build_inventory,
)
from dedupe.cache import ProbeCache
from dedupe.fuzzy import UnionFind
from dedupe.video import VIDEO_EXTENSIONS

__all__ = [
    "IMAGE_EXTENSIONS",
    "DOCUMENT_EXTENSIONS",
    "PLAIN_TEXT_EXTENSIONS",
    "DEFAULT_IMAGE_DISTANCE",
    "DEFAULT_TEXT_SIMILARITY",
    "DEFAULT_VIDEO_DISTANCE",
    "default_workers",
    "SimilarMatch",
    "dhash_image",
    "hamming_distance",
    "band_keys",
    "document_words",
    "document_shingles",
    "shingle_set",
    "jaccard",
    "ffprobe_command",
    "probe_video",
    "video_frame_hashes",
    "frames_matching",
    "run_similar_pass",
]

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff",
}
PLAIN_TEXT_EXTENSIONS = {".txt", ".md", ".csv"}
DOCUMENT_EXTENSIONS = PLAIN_TEXT_EXTENSIONS | {".docx", ".pdf"}

DEFAULT_IMAGE_DISTANCE = 6
DEFAULT_TEXT_SIMILARITY = 0.9
DEFAULT_VIDEO_DISTANCE = 10


def default_workers() -> int:
    """Default parallel workers: min(8, cpu_count or 4), at least 2."""
    cpus = os.cpu_count() or 4
    return max(2, min(8, cpus))

# Video: 7 evenly spaced frames roughly spanning 10%-90% of the duration,
# each reduced by ffmpeg to the same 9x8 grayscale grid used for dHash.
VIDEO_FRAME_COUNT = 7
VIDEO_FRAME_MATCH_NEEDED = 5
_FFMPEG_TIMEOUT = 60

# dHash: 9x8 grayscale, each pixel compared with its right neighbour.
DHASH_WIDTH = 9
DHASH_HEIGHT = 8
DHASH_BITS = 64
# 8 bands of 8 bits: any two hashes within distance 7 must agree on a whole
# band (pigeonhole), so bucketing by band is a complete candidate filter for
# the supported distances and nothing has to be compared with everything.
HASH_BANDS = 8
HASH_BAND_BITS = DHASH_BITS // HASH_BANDS

# Documents: at most the first 2 MB of text, 5-word shingles, and anything
# shorter than 50 words is too small to judge.
MAX_TEXT_BYTES = 2 * 1024 * 1024
MIN_DOCUMENT_WORDS = 50
SHINGLE_WORDS = 5
_MAX_DOCX_XML_BYTES = 64 * 1024 * 1024

# MinHash/LSH: 32 permutations in 8 bands of 4 rows. At Jaccard 0.9 a pair is
# a candidate with probability 1-(1-0.9**4)**8 > 0.999, and every candidate is
# confirmed with the real Jaccard afterwards, so banding only ever saves work.
_MINHASH_PERMUTATIONS = 32
_LSH_ROWS = 4
_LSH_BANDS = _MINHASH_PERMUTATIONS // _LSH_ROWS
# Small groups are compared directly: banding cannot pay for itself there, and
# an exact sweep removes the (tiny) chance of a missed pair.
_PAIRWISE_LIMIT = 50
_MERSENNE_PRIME = (1 << 61) - 1


@dataclass(frozen=True)
class SimilarMatch:
    """One near-duplicate and the file kept in its place."""

    removed_path: Path
    kept_path: Path
    kind: str  # "image", "document" or "video"
    score_text: str  # "distance 3", "similarity 0.95", "frames 7/7"


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------


def _resampling_filter():
    from PIL import Image

    resampling = getattr(Image, "Resampling", None)
    if resampling is not None:
        return resampling.LANCZOS
    return Image.LANCZOS


def _dhash_from_gray(data: Sequence[int]) -> Optional[int]:
    """64-bit dHash of a 9x8 grayscale grid, or None when the grid is short."""
    if len(data) < DHASH_WIDTH * DHASH_HEIGHT:
        return None
    bits = 0
    for y in range(DHASH_HEIGHT):
        row = y * DHASH_WIDTH
        for x in range(DHASH_WIDTH - 1):
            bits = (bits << 1) | (1 if data[row + x] < data[row + x + 1] else 0)
    return bits


def dhash_image(path: Path) -> Optional[Tuple[int, int]]:
    """Return ``(64-bit dHash, pixel count)`` for an image, or None.

    Unreadable, truncated or corrupt files (anything Pillow refuses) give
    None so the caller can skip them silently.
    """
    try:
        from PIL import Image

        with Image.open(path) as im:
            pixels = int(im.width) * int(im.height)
            small = im.convert("L").resize(
                (DHASH_WIDTH, DHASH_HEIGHT), _resampling_filter()
            )
            data = list(small.getdata())
    except Exception:
        return None
    bits = _dhash_from_gray(data)
    if bits is None:
        return None
    return bits, pixels


def hamming_distance(a: int, b: int) -> int:
    """Number of differing bits between two hashes."""
    return int(a ^ b).bit_count()


def band_keys(value: int) -> List[Tuple[int, int]]:
    """Split a 64-bit hash into ``(band index, band value)`` pairs."""
    mask = (1 << HASH_BAND_BITS) - 1
    return [
        (band, (value >> (band * HASH_BAND_BITS)) & mask)
        for band in range(HASH_BANDS)
    ]


# --------------------------------------------------------------------------
# Videos
# --------------------------------------------------------------------------


def _have_ffmpeg_tools() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def ffprobe_command(path: Path) -> List[str]:
    """The ffprobe argv used to read one video's duration and frame size.

    No ``-nostdin`` here: that option belongs to ffmpeg only, and ffprobe
    rejects unknown options outright (exit 1, nothing on stdout), which would
    make every video unprobeable. ``stdin=subprocess.DEVNULL`` on the call
    gives the same guarantee of never touching the terminal.
    """
    return [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration:format=duration",
        "-of", "json", str(path),
    ]


def _positive_float(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def probe_video(path: Path) -> Optional[Tuple[float, int, int]]:
    """Return ``(duration seconds, width, height)`` via ffprobe, or None.

    None means "skip this file silently": ffprobe missing, a corrupt or
    non-video file, or a stream with no usable duration or frame size.
    """
    try:
        proc = subprocess.run(
            ffprobe_command(path),
            capture_output=True, timeout=_FFMPEG_TIMEOUT, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
        streams = data.get("streams") or []
        if not streams:
            return None
        stream = streams[0]
        width = int(stream["width"])
        height = int(stream["height"])
    except Exception:
        return None
    # Container duration first; some containers (Matroska is the common one)
    # only carry it on the stream, so fall back to that before giving up.
    duration = _positive_float((data.get("format") or {}).get("duration"))
    if duration is None:
        duration = _positive_float(stream.get("duration"))
    if duration is None or width <= 0 or height <= 0:
        return None
    return duration, width, height


def _frame_fractions(count: int = VIDEO_FRAME_COUNT) -> List[float]:
    if count <= 1:
        return [0.5]
    return [0.1 + i * (0.8 / (count - 1)) for i in range(count)]


def _grab_frame_hash(path: Path, t: float) -> Optional[int]:
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-ss", f"{t:.3f}", "-i", str(path),
                "-frames:v", "1", "-vf", f"scale={DHASH_WIDTH}:{DHASH_HEIGHT},format=gray",
                # -an/-sn: nothing but the picture may reach the raw pipe.
                "-an", "-sn", "-f", "rawvideo", "-",
            ],
            capture_output=True, timeout=_FFMPEG_TIMEOUT, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    # scale=9:8,format=gray over rawvideo is exactly 72 packed bytes.
    return _dhash_from_gray(proc.stdout)


def video_frame_hashes(path: Path, duration: float) -> Optional[List[Optional[int]]]:
    """dHash per evenly spaced frame (None for a frame that would not decode).

    A single unreadable frame only costs that frame: it can never count as a
    match, so a video that still yields ``VIDEO_FRAME_MATCH_NEEDED`` frames can
    be judged. Fewer than that means the file is not judgeable at all
    (unreadable/corrupt/truncated) and the whole video is skipped silently,
    signalled by None.
    """
    hashes: List[Optional[int]] = []
    for frac in _frame_fractions():
        t = max(0.0, min(duration - 0.01, duration * frac))
        hashes.append(_grab_frame_hash(path, t))
    if sum(1 for h in hashes if h is not None) < VIDEO_FRAME_MATCH_NEEDED:
        return None
    return hashes


def frames_matching(
    a: Sequence[Optional[int]], b: Sequence[Optional[int]], distance: int
) -> int:
    """How many frame pairs are within ``distance`` bits of each other."""
    return sum(
        1
        for ha, hb in zip(a, b)
        if ha is not None and hb is not None and hamming_distance(ha, hb) <= distance
    )


def _duration_close(a: float, b: float) -> bool:
    tol = max(2.0, 0.02 * max(a, b))
    return abs(a - b) <= tol


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="ignore")


def _read_plain_text(path: Path) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            return _decode(fh.read(MAX_TEXT_BYTES))
    except OSError:
        return None


def _read_docx_text(path: Path) -> Optional[str]:
    """Pull the body text out of word/document.xml with the stdlib only.

    The pieces are concatenated with no separator: WordprocessingML carries no
    implied whitespace between runs, and a writer may split a single sentence
    (even a single word) across runs and paragraphs at arbitrary points. Joining
    with a space would invent word breaks there, and because two edits of the
    same document split at different offsets the invented breaks would differ,
    which wrecks the shingle overlap the comparison relies on.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            try:
                info = zf.getinfo("word/document.xml")
            except KeyError:
                return None
            if info.file_size > _MAX_DOCX_XML_BYTES:
                return None
            with zf.open(info) as fh:
                data = fh.read(_MAX_DOCX_XML_BYTES)
        root = ET.fromstring(data)
    except Exception:
        return None
    parts: List[str] = []
    length = 0
    for text in root.itertext():
        if not text:
            continue
        parts.append(text)
        length += len(text)
        if length >= MAX_TEXT_BYTES:
            break
    return "".join(parts)


def _read_pdf_text(path: Path) -> Optional[str]:
    try:
        import pypdf
    except Exception:
        return None
    try:
        reader = pypdf.PdfReader(str(path))
        parts: List[str] = []
        length = 0
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                continue
            parts.append(text)
            length += len(text)
            if length >= MAX_TEXT_BYTES:
                break
    except Exception:
        return None
    return " ".join(parts)


def _extract_text(path: Path, suffix: str) -> Optional[str]:
    if suffix in PLAIN_TEXT_EXTENSIONS:
        return _read_plain_text(path)
    if suffix == ".docx":
        return _read_docx_text(path)
    if suffix == ".pdf":
        return _read_pdf_text(path)
    return None


def document_words(text: str) -> List[str]:
    """Lowercase, collapse whitespace, split into words."""
    return text.lower().split()


def shingle_set(words: Sequence[str], size: int = SHINGLE_WORDS) -> FrozenSet[int]:
    """Hashed set of overlapping ``size``-word shingles."""
    shingles = set()
    for i in range(len(words) - size + 1):
        joined = " ".join(words[i : i + size])
        digest = hashlib.blake2b(joined.encode("utf-8"), digest_size=8).digest()
        shingles.add(int.from_bytes(digest, "big"))
    return frozenset(shingles)


def document_shingles(path: Path, suffix: Optional[str] = None) -> Optional[FrozenSet[int]]:
    """Shingles for one document, or None when it cannot be judged.

    None means "skip silently": unreadable or corrupt file, a format whose
    reader is not installed, or fewer than 50 words of text.
    """
    sfx = (suffix if suffix is not None else Path(path).suffix).lower()
    text = _extract_text(Path(path), sfx)
    if text is None:
        return None
    words = document_words(text)
    if len(words) < MIN_DOCUMENT_WORDS:
        return None
    shingles = shingle_set(words)
    return shingles or None


def jaccard(a: FrozenSet[int], b: FrozenSet[int]) -> float:
    """Jaccard similarity of two shingle sets."""
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    if not intersection:
        return 0.0
    return intersection / (len(a) + len(b) - intersection)


def _minhash_coefficients(count: int) -> Tuple[List[int], List[int]]:
    multipliers: List[int] = []
    offsets: List[int] = []
    for i in range(count):
        digest = hashlib.blake2b(str(i).encode("ascii"), digest_size=16).digest()
        multipliers.append(int.from_bytes(digest[:8], "big") % (_MERSENNE_PRIME - 1) + 1)
        offsets.append(int.from_bytes(digest[8:], "big") % _MERSENNE_PRIME)
    return multipliers, offsets


_MINHASH_A, _MINHASH_B = _minhash_coefficients(_MINHASH_PERMUTATIONS)


def _signature(shingles: FrozenSet[int]) -> Tuple[int, ...]:
    values = list(shingles)
    prime = _MERSENNE_PRIME
    return tuple(
        min((a * value + b) % prime for value in values)
        for a, b in zip(_MINHASH_A, _MINHASH_B)
    )


def _lsh_candidate_pairs(signatures: Sequence[Tuple[int, ...]]) -> Set[Tuple[int, int]]:
    buckets: Dict[Tuple[int, Tuple[int, ...]], List[int]] = {}
    for idx, sig in enumerate(signatures):
        for band in range(_LSH_BANDS):
            key = (band, sig[band * _LSH_ROWS : (band + 1) * _LSH_ROWS])
            buckets.setdefault(key, []).append(idx)
    pairs: Set[Tuple[int, int]] = set()
    for members in buckets.values():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pairs.add((members[i], members[j]))
    return pairs


# --------------------------------------------------------------------------
# The pass
# --------------------------------------------------------------------------


def _is_under_any(path: Path, folders: Set[Path]) -> bool:
    return any(path == folder or folder in path.parents for folder in folders)


def _groups_from(uf: UnionFind, items: Sequence[Path]) -> List[List[Path]]:
    groups: Dict[int, List[Path]] = {}
    for idx, item in enumerate(items):
        groups.setdefault(uf.find(idx), []).append(item)
    return [sorted(g, key=lambda p: p.as_posix()) for g in groups.values() if len(g) >= 2]


def run_similar_pass(
    root: Path,
    trash_manager: TrashManager,
    removed_folders: Set[Path],
    removed_files: Set[Path],
    dry_run: bool = False,
    permanent: bool = False,
    review: bool = False,
    image_distance: int = DEFAULT_IMAGE_DISTANCE,
    text_similarity: float = DEFAULT_TEXT_SIMILARITY,
    video_distance: int = DEFAULT_VIDEO_DISTANCE,
    *,
    inventory: Optional[Inventory] = None,
    progress: Optional[ProgressReporter] = None,
    quiet: bool = False,
    workers: Optional[int] = None,
    probe_cache: Optional[ProbeCache] = None,
) -> Tuple[List[Removal], Set[Path], List[SimilarMatch]]:
    """Find near-duplicate images and documents among the surviving files.

    Returns ``(removals, removed paths, matches)``. With ``review=True`` the
    removal list is empty and nothing is moved, but the matches are still
    reported so the caller can list them.
    """
    root = root.resolve()
    removals: List[Removal] = []
    similar_removed: Set[Path] = set()
    matches: List[SimilarMatch] = []

    if progress is not None:
        progress.set_pass("similar")
    if inventory is None:
        inventory = build_inventory(root, progress=progress)

    size_of: Dict[Path, int] = {}
    mtime_of: Dict[Path, float] = {}
    images: List[Path] = []
    documents: Dict[str, List[Path]] = {}
    videos: List[Path] = []
    for info in inventory.files:
        fp = info.path
        size_of[fp] = info.size
        mtime_of[fp] = info.mtime
        if info.size == 0:
            continue
        if fp in removed_files or _is_under_any(fp, removed_folders):
            continue
        suffix = fp.suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            images.append(fp)
        elif suffix in DOCUMENT_EXTENSIONS:
            documents.setdefault(suffix, []).append(fp)
        elif suffix in VIDEO_EXTENSIONS:
            videos.append(fp)
    images.sort(key=lambda p: p.as_posix())
    videos.sort(key=lambda p: p.as_posix())
    for paths in documents.values():
        paths.sort(key=lambda p: p.as_posix())

    if progress is not None:
        progress.set_similar_totals(
            videos=len(videos),
            images=len(images),
            documents=sum(len(p) for p in documents.values()),
        )

    def _depth(p: Path) -> int:
        try:
            return len(p.relative_to(root).parts)
        except ValueError:
            return len(p.parts)

    def _rel_posix(p: Path) -> str:
        try:
            return p.relative_to(root).as_posix()
        except ValueError:
            return p.as_posix()

    def _exact_tail(p: Path):
        """The pass-2 keeper rule, read from the inventory (never a fresh stat)."""
        return (len(p.name), p.name, mtime_of.get(p, 0.0), _depth(p), _rel_posix(p))

    def _warn(message: str) -> None:
        if quiet:
            return
        try:
            sys.stderr.write(message + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    # --- Images --------------------------------------------------------
    if workers is None:
        workers = default_workers()
    elif workers < 1:
        workers = 1
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers) if workers > 1 else None

    # --- Images --------------------------------------------------------
    image_hash: Dict[Path, int] = {}
    image_pixels: Dict[Path, int] = {}
    if len(images) >= 2:
        try:
            importlib.import_module("PIL.Image")
            have_pillow = True
        except Exception:
            have_pillow = False
        if not have_pillow:
            _warn("image similarity skipped: Pillow not installed")
            if progress is not None:
                progress.note_similar_done("images", len(images))
        else:
            images_to_hash: List[Path] = []
            for fp in images:
                if probe_cache is not None:
                    hit, res = probe_cache.get_image(
                        fp, size_of.get(fp, 0), mtime_of.get(fp, 0.0)
                    )
                    if hit:
                        if res is not None:
                            image_hash[fp], image_pixels[fp] = res
                        if progress is not None:
                            progress.note_similar_done("images")
                        continue
                images_to_hash.append(fp)

            if images_to_hash:
                if executor is not None:
                    fut_map = {
                        executor.submit(dhash_image, fp): fp
                        for fp in images_to_hash
                    }
                    for fut in concurrent.futures.as_completed(fut_map):
                        fp = fut_map[fut]
                        res = fut.result()
                        if res is not None:
                            image_hash[fp], image_pixels[fp] = res
                        if probe_cache is not None:
                            probe_cache.set_image(
                                fp, size_of.get(fp, 0), mtime_of.get(fp, 0.0), res
                            )
                        if progress is not None:
                            progress.note_similar_done("images")
                else:
                    for fp in images_to_hash:
                        res = dhash_image(fp)
                        if res is not None:
                            image_hash[fp], image_pixels[fp] = res
                        if probe_cache is not None:
                            probe_cache.set_image(
                                fp, size_of.get(fp, 0), mtime_of.get(fp, 0.0), res
                            )
                        if progress is not None:
                            progress.note_similar_done("images")
    else:
        if progress is not None and images:
            progress.note_similar_done("images", len(images))

    hashed_images = [fp for fp in images if fp in image_hash]
    image_groups: List[List[Path]] = []
    if len(hashed_images) >= 2:
        buckets: Dict[Tuple[int, int], List[int]] = {}
        for idx, fp in enumerate(hashed_images):
            for key in band_keys(image_hash[fp]):
                buckets.setdefault(key, []).append(idx)
        uf = UnionFind(len(hashed_images))
        for members in buckets.values():
            if len(members) < 2:
                continue
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    a, b = members[i], members[j]
                    if uf.find(a) == uf.find(b):
                        continue
                    if hamming_distance(
                        image_hash[hashed_images[a]], image_hash[hashed_images[b]]
                    ) <= image_distance:
                        uf.union(a, b)
        image_groups = _groups_from(uf, hashed_images)

    def _image_keeper_key(p: Path):
        return (-image_pixels.get(p, 0), -size_of.get(p, 0)) + _exact_tail(p)

    image_groups.sort(key=lambda g: min(_image_keeper_key(p) for p in g))
    for group in image_groups:
        keeper = min(group, key=_image_keeper_key)
        for fp in group:
            if fp == keeper:
                continue
            distance = hamming_distance(image_hash[fp], image_hash[keeper])
            matches.append(
                SimilarMatch(
                    removed_path=fp,
                    kept_path=keeper,
                    kind="image",
                    score_text=f"distance {distance}",
                )
            )

    # --- Documents -----------------------------------------------------
    if len(documents.get(".pdf", [])) >= 2:
        try:
            importlib.import_module("pypdf")
            have_pypdf = True
        except Exception:
            have_pypdf = False
        if not have_pypdf:
            _warn("pdf similarity skipped: pypdf not installed")

    document_groups: List[List[Path]] = []
    shingles_of: Dict[Path, FrozenSet[int]] = {}
    # Same extension only: plan.txt and plan.docx are an intentional export,
    # never a duplicate of each other.
    for suffix in sorted(documents):
        paths = documents[suffix]
        if len(paths) < 2:
            if progress is not None:
                progress.note_similar_done("documents", len(paths))
            continue
        if suffix == ".pdf" and not have_pypdf:
            if progress is not None:
                progress.note_similar_done("documents", len(paths))
            continue
        docs_to_read: List[Path] = []
        for fp in paths:
            if probe_cache is not None:
                hit, res = probe_cache.get_document_shingles(
                    fp, size_of.get(fp, 0), mtime_of.get(fp, 0.0)
                )
                if hit:
                    if res is not None:
                        shingles_of[fp] = res
                    if progress is not None:
                        progress.note_similar_done("documents")
                    continue
            docs_to_read.append(fp)

        if docs_to_read:
            if executor is not None:
                fut_map = {
                    executor.submit(document_shingles, fp, suffix): fp
                    for fp in docs_to_read
                }
                for fut in concurrent.futures.as_completed(fut_map):
                    fp = fut_map[fut]
                    res = fut.result()
                    if res is not None:
                        shingles_of[fp] = res
                    if probe_cache is not None:
                        probe_cache.set_document_shingles(
                            fp, size_of.get(fp, 0), mtime_of.get(fp, 0.0), res
                        )
                    if progress is not None:
                        progress.note_similar_done("documents")
            else:
                for fp in docs_to_read:
                    res = document_shingles(fp, suffix)
                    if res is not None:
                        shingles_of[fp] = res
                    if probe_cache is not None:
                        probe_cache.set_document_shingles(
                            fp, size_of.get(fp, 0), mtime_of.get(fp, 0.0), res
                        )
                    if progress is not None:
                        progress.note_similar_done("documents")

        readable = [fp for fp in paths if fp in shingles_of]
        if len(readable) < 2:
            continue

        if len(readable) <= _PAIRWISE_LIMIT:
            candidate_pairs = {
                (i, j)
                for i in range(len(readable))
                for j in range(i + 1, len(readable))
            }
        else:
            candidate_pairs = _lsh_candidate_pairs(
                [_signature(shingles_of[fp]) for fp in readable]
            )

        uf = UnionFind(len(readable))
        for i, j in sorted(candidate_pairs):
            if uf.find(i) == uf.find(j):
                continue
            first, second = shingles_of[readable[i]], shingles_of[readable[j]]
            # Jaccard >= t forces the sizes to be within t of each other.
            smaller, larger = sorted((len(first), len(second)))
            if larger and (smaller / larger) < text_similarity:
                continue
            if jaccard(first, second) >= text_similarity:
                uf.union(i, j)
        document_groups.extend(_groups_from(uf, readable))

    def _document_keeper_key(p: Path):
        return (-mtime_of.get(p, 0.0), -size_of.get(p, 0), _rel_posix(p))

    document_groups.sort(key=lambda g: min(_document_keeper_key(p) for p in g))
    for group in document_groups:
        keeper = min(group, key=_document_keeper_key)
        for fp in group:
            if fp == keeper:
                continue
            score = jaccard(shingles_of[fp], shingles_of[keeper])
            matches.append(
                SimilarMatch(
                    removed_path=fp,
                    kept_path=keeper,
                    kind="document",
                    score_text=f"similarity {score:.2f}",
                )
            )

    # --- Videos ----------------------------------------------------------
    video_hashes: Dict[Path, List[Optional[int]]] = {}
    video_pixels: Dict[Path, int] = {}
    video_groups: List[List[Path]] = []
    match_count: Dict[Tuple[Path, Path], int] = {}
    if len(videos) >= 2:
        if not _have_ffmpeg_tools():
            _warn("video similarity skipped: ffmpeg not found")
            if progress is not None:
                progress.note_similar_done("videos", len(videos))
        else:
            probe: Dict[Path, Tuple[float, int, int]] = {}
            videos_to_probe: List[Path] = []
            for fp in videos:
                if probe_cache is not None:
                    hit, res = probe_cache.get_video_probe(
                        fp, size_of.get(fp, 0), mtime_of.get(fp, 0.0)
                    )
                    if hit:
                        if res is not None:
                            probe[fp] = res
                        if progress is not None:
                            progress.note_similar_done("videos")
                        continue
                videos_to_probe.append(fp)

            if videos_to_probe:
                if executor is not None:
                    fut_map = {
                        executor.submit(probe_video, fp): fp
                        for fp in videos_to_probe
                    }
                    for fut in concurrent.futures.as_completed(fut_map):
                        fp = fut_map[fut]
                        res = fut.result()
                        if res is not None:
                            probe[fp] = res
                        if probe_cache is not None:
                            probe_cache.set_video_probe(
                                fp, size_of.get(fp, 0), mtime_of.get(fp, 0.0), res
                            )
                        if progress is not None:
                            progress.note_similar_done("videos")
                else:
                    for fp in videos_to_probe:
                        res = probe_video(fp)
                        if res is not None:
                            probe[fp] = res
                        if probe_cache is not None:
                            probe_cache.set_video_probe(
                                fp, size_of.get(fp, 0), mtime_of.get(fp, 0.0), res
                            )
                        if progress is not None:
                            progress.note_similar_done("videos")

            probed = [fp for fp in videos if fp in probe]

            duration_pairs: List[Tuple[Path, Path]] = []
            for i in range(len(probed)):
                for j in range(i + 1, len(probed)):
                    a, b = probed[i], probed[j]
                    if _duration_close(probe[a][0], probe[b][0]):
                        duration_pairs.append((a, b))

            needed: Set[Path] = set()
            for a, b in duration_pairs:
                needed.add(a)
                needed.add(b)

            needed_to_hash: List[Path] = []
            for fp in sorted(needed, key=lambda p: p.as_posix()):
                if probe_cache is not None:
                    hit, cached_hashes = probe_cache.get_video_frames(
                        fp, size_of.get(fp, 0), mtime_of.get(fp, 0.0)
                    )
                    if hit:
                        if cached_hashes is not None:
                            video_hashes[fp] = cached_hashes
                            video_pixels[fp] = probe[fp][1] * probe[fp][2]
                        continue
                needed_to_hash.append(fp)

            if needed_to_hash:
                if executor is not None:
                    fut_map = {
                        executor.submit(video_frame_hashes, fp, probe[fp][0]): fp
                        for fp in needed_to_hash
                    }
                    for fut in concurrent.futures.as_completed(fut_map):
                        fp = fut_map[fut]
                        hashes = fut.result()
                        if hashes is not None:
                            video_hashes[fp] = hashes
                            video_pixels[fp] = probe[fp][1] * probe[fp][2]
                        if probe_cache is not None:
                            probe_cache.set_video_frames(
                                fp, size_of.get(fp, 0), mtime_of.get(fp, 0.0), hashes
                            )
                        if progress is not None:
                            progress.maybe_status()
                else:
                    for fp in needed_to_hash:
                        hashes = video_frame_hashes(fp, probe[fp][0])
                        if hashes is not None:
                            video_hashes[fp] = hashes
                            video_pixels[fp] = probe[fp][1] * probe[fp][2]
                        if probe_cache is not None:
                            probe_cache.set_video_frames(
                                fp, size_of.get(fp, 0), mtime_of.get(fp, 0.0), hashes
                            )
                        if progress is not None:
                            progress.maybe_status()

            index_of = {fp: i for i, fp in enumerate(probed)}
            uf = UnionFind(len(probed))
            for a, b in duration_pairs:
                if a not in video_hashes or b not in video_hashes:
                    continue
                n = frames_matching(video_hashes[a], video_hashes[b], video_distance)
                if n >= VIDEO_FRAME_MATCH_NEEDED:
                    match_count[(a, b)] = n
                    uf.union(index_of[a], index_of[b])
            video_groups = _groups_from(uf, probed)
            video_groups = [[p for p in g if p in video_hashes] for g in video_groups]
            video_groups = [g for g in video_groups if len(g) >= 2]
    else:
        if progress is not None and videos:
            progress.note_similar_done("videos", len(videos))

    def _video_keeper_key(p: Path):
        return (-video_pixels.get(p, 0), -size_of.get(p, 0), _depth(p), _rel_posix(p))

    video_groups.sort(key=lambda g: min(_video_keeper_key(p) for p in g))
    for group in video_groups:
        keeper = min(group, key=_video_keeper_key)
        for fp in group:
            if fp == keeper:
                continue
            n = match_count.get((fp, keeper))
            if n is None:
                n = match_count.get((keeper, fp))
            if n is None:
                n = frames_matching(
                    video_hashes[fp], video_hashes[keeper], video_distance
                )
            matches.append(
                SimilarMatch(
                    removed_path=fp,
                    kept_path=keeper,
                    kind="video",
                    score_text=f"frames {n}/{VIDEO_FRAME_COUNT}",
                )
            )

    if progress is not None and matches:
        progress.note_similar(len(matches))

    if not review:
        action_label = "delete" if permanent else "trash"
        if dry_run:
            action_label = f"dry-run {action_label}"
        for match in matches:
            fp = match.removed_path
            similar_removed.add(fp)
            removals.append(
                Removal(
                    action=action_label,
                    removed_path=fp,
                    kept_path=match.kept_path,
                    reason=f"similar-{match.kind}",
                    bytes_freed=size_of.get(fp, 0),
                    is_dir=False,
                )
            )
            if progress is not None:
                progress.note_duplicate()
            try:
                trash_manager.remove_file(fp)
            except OSError:
                pass

    if executor is not None:
        executor.shutdown(wait=True)

    if progress is not None:
        progress.end_pass("similar")
    return removals, similar_removed, matches
