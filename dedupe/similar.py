"""Pass 4: near-duplicate images and documents (the "similar" pass).

Everything here runs on this machine only: a perceptual difference hash
(dHash) for images through Pillow, and shingled MinHash/LSH over extracted
text for documents. No network calls, no services, no API keys -- the pass
only ever opens local files.

Near-duplicates are removed like any other duplicate by default; the caller
can ask for a listing instead (``review=True``), in which case nothing from
this pass moves.
"""

from __future__ import annotations

import hashlib
import importlib
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
from dedupe.fuzzy import UnionFind

__all__ = [
    "IMAGE_EXTENSIONS",
    "DOCUMENT_EXTENSIONS",
    "PLAIN_TEXT_EXTENSIONS",
    "DEFAULT_IMAGE_DISTANCE",
    "DEFAULT_TEXT_SIMILARITY",
    "SimilarMatch",
    "dhash_image",
    "hamming_distance",
    "band_keys",
    "document_words",
    "document_shingles",
    "shingle_set",
    "jaccard",
    "run_similar_pass",
]

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff",
}
PLAIN_TEXT_EXTENSIONS = {".txt", ".md", ".csv"}
DOCUMENT_EXTENSIONS = PLAIN_TEXT_EXTENSIONS | {".docx", ".pdf"}

DEFAULT_IMAGE_DISTANCE = 6
DEFAULT_TEXT_SIMILARITY = 0.9

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
    kind: str  # "image" or "document"
    score_text: str  # "distance 3", "similarity 0.95"


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------


def _resampling_filter():
    from PIL import Image

    resampling = getattr(Image, "Resampling", None)
    if resampling is not None:
        return resampling.LANCZOS
    return Image.LANCZOS


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
    if len(data) < DHASH_WIDTH * DHASH_HEIGHT:
        return None
    bits = 0
    for y in range(DHASH_HEIGHT):
        row = y * DHASH_WIDTH
        for x in range(DHASH_WIDTH - 1):
            bits = (bits << 1) | (1 if data[row + x] < data[row + x + 1] else 0)
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
    *,
    inventory: Optional[Inventory] = None,
    progress: Optional[ProgressReporter] = None,
    quiet: bool = False,
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
    images.sort(key=lambda p: p.as_posix())
    for paths in documents.values():
        paths.sort(key=lambda p: p.as_posix())

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
        else:
            for fp in images:
                result = dhash_image(fp)
                if progress is not None:
                    progress.maybe_status()
                if result is None:
                    continue
                image_hash[fp], image_pixels[fp] = result

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
            continue
        readable: List[Path] = []
        for fp in paths:
            shingles = document_shingles(fp, suffix)
            if progress is not None:
                progress.maybe_status()
            if shingles is None:
                continue
            shingles_of[fp] = shingles
            readable.append(fp)
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

    if progress is not None:
        progress.end_pass("similar")
    return removals, similar_removed, matches
