# dedupe

A command-line tool to scan a folder recursively and safely remove duplicate folders, exact duplicate files, and fuzzy-name duplicate files. Built with Python 3.12 standard library only (no third-party dependencies).

## Key Features

- **Duplicate Folders**: Identifies non-empty folders whose entire recursive structure and SHA-256 file contents match. Only top-most matching folders are removed whole.
- **Exact Duplicate Files**: Scans the whole tree for identical files by size and chunked SHA-256 hashes (skipping zero-byte files).
- **Fuzzy-Name Duplicate Files**: Identifies copy-named variants (e.g. `report (1).docx`, `Copy of budget.xlsx`, `file - Copy.docx`) and files sharing $N+$ common characters within the same folder and same extension using stem normalization, `difflib.SequenceMatcher` ratio, and longest common contiguous substring matching (`--min-match`).
- **Numbered Sequence Protection**: Excludes files that differ only in numbers/digits (e.g. `IMG_0001.jpg` vs `IMG_0002.jpg`, `chapter-1.md` vs `chapter-2.md`).
- **Safety First**:
  - Defaults to moving duplicates into a timestamped trash folder: `PATH\_dedupe_trash\<YYYYMMDD-HHMMSS>\`, preserving relative directory structures.
  - The trash folder is only created when an item is actually moved.
  - Subsequent runs automatically skip `_dedupe_trash`.
  - Symlinks and junctions are never followed.
  - `--dry-run` allows previewing removals without modifying any files.

---

## Quick Start

### Using `dedupe.cmd` (Windows)

From any directory, run `dedupe.cmd` pointing to the target folder:

```cmd
dedupe.cmd D:\path\to\target
```

### Using Python Module

Run directly with Python 3.12:

```bash
python -m dedupe PATH [options]
```

---

## CLI Options

```text
usage: dedupe [-h] [--dry-run] [--permanent] [--no-fuzzy] [--quiet]
              [--threshold THRESHOLD] [--min-match MIN_MATCH]
              path

positional arguments:
  path                  Target folder path to scan and deduplicate

options:
  -h, --help            Show this help message and exit
  --dry-run             Print the plan and change nothing
  --permanent           Delete duplicates permanently instead of moving to trash
  --no-fuzzy            Skip fuzzy-name duplicate matching
  --threshold THRESHOLD
                        Fuzzy matching ratio threshold (default: 0.9)
  --min-match MIN_MATCH
                        Minimum common substring length in normalized stems
                        for fuzzy matching (default: 10; 0 disables)
  --quiet               Suppress progress output on stderr
```

---

## Progress Output

Progress is printed to stderr (stdout keeps only the removal lines and summary). The first line is `Scanning <path> ...`, printed and flushed immediately. While walking and hashing, a status line is printed at most every 2 seconds; one status line is always printed at the end of each pass (`walk`, `folder`, `exact`, `fuzzy`). Every status line shows both running counters plus the current pass name, for example `[exact] 12,345 files scanned, 37 duplicates found`. `files scanned` counts every file found by the walk (including zero-byte files; `_dedupe_trash` and symlinks/junctions excluded) and `duplicates found` counts removals planned or done so far (one per duplicate folder, one per duplicate file; never goes down). The last stderr line shows the elapsed time with the final totals, for example `Done in 1.23s: 12,345 files scanned, 37 duplicates found`. Use `--quiet` to suppress all progress output on stderr (errors are still printed to stderr).

The tree is walked once and files are hashed lazily through a shared cache: each file is hashed at most once per run, and a file whose size is unique among non-empty files is never hashed. Note that the first run on a big network share still reads every file that has a same-size twin.

The walk reads size and modification time straight from the directory listing (no per-file stat/resolve round trips), so a slow network share is fast to scan. If a single directory listing (or another pass) takes a while, a `(still listing <folder>, Ns)` or `(still working on <pass>, Ns)` line is printed every 2-3 seconds until it returns, so the scan never looks stuck; a folder that cannot be listed (permission error, dropped share) is skipped and the walk continues. These heartbeat lines are suppressed by `--quiet` along with the rest of the progress output.

## Order of Passes

1. **Pass 1 - Duplicate Folders**:
   - Compares non-empty directories across the tree. A folder is non-empty if it contains at least one file with size > 0.
   - Matching is based on identical relative file paths and SHA-256 hashes.
   - Removes duplicate folders whole. Only top-most folders are handled; matching sub-folders are not reported separately.
   - Keeper selection: shallowest directory depth, then shortest folder name, then alphabetical order.

2. **Pass 2 - Exact Duplicate Files**:
   - Compares surviving regular files across the entire tree.
   - Files are grouped by size; only files sharing a first block are then hashed in chunks using SHA-256 (through the shared cache). Zero-byte files are skipped.
   - Keeper selection: shortest file name, then alphabetical file name, then oldest modification time (`mtime`), then shallowest directory depth, then alphabetical path.

3. **Pass 3 - Fuzzy-Name Duplicate Files**:
   - Skipped if `--no-fuzzy` is specified. Zero-byte files are skipped.
   - Compares files only within the **same folder** and having the **same extension** (case-insensitive).
   - Normalizes stems: converts to lowercase, strips copy markers (`copy of ` prefix, ` (n)` suffix, ` - copy` / ` - copy (n)` suffix, `_copy`/` copy`/`-copy` with optional digits), collapses runs of 3+ identical characters to one (so padded names like `filexxxxxx` do not match on the padding alone), and collapses whitespace/underscores/hyphens to a single space.
   - Matches when EITHER `difflib.SequenceMatcher` ratio of normalized stems >= threshold (default `0.9`) OR the longest common contiguous substring of normalized stems (via `difflib.SequenceMatcher.find_longest_match`) is at least `N` characters (`--min-match N`, default `10`; `0` disables this rule), **except** when stems differ only in digits (which are never duplicates).
   - Matches are transitive: files are grouped using union-find.
   - Keeper selection: one file is kept per group (a file with no copy marker in its name, then newest modification time (`mtime`), then shortest name, then alphabetical order). The printed reason for removals is `fuzzy`.

---

## Torrent Video Duplicates (video-names pass)

After the fuzzy pass (and skipped by `--no-fuzzy`), video files (`.mkv`, `.mp4`, `.avi`, `.m4v`, `.mov`, `.wmv`, `.mpg`, `.mpeg`, `.ts`, `.m2ts`, `.mts`, `.webm`, `.flv`, `.3gp`, `.divx`, `.vob`) are compared across the whole tree and across extensions by their parsed release names. Site prefixes (`www.xxx.yyy - `, `[site]`), bracketed groups, release/source/codec/audio/edition/language tags, and trailing `-GROUP` suffixes are stripped; the year, episode code, and resolution are extracted. The keeper is the highest resolution, then the biggest file, then the shallowest path. Removals use reason `video-name`. This pass never prompts and never uses the network.

Examples of what matches (duplicates):

- `The.Movie.2020.720p.WEB-DL.x264.mkv` and `The Movie (2020) [1080p].mp4` — same movie; the 1080p copy is kept.
- `Show.S01E05.720p.HDTV.mkv` and `Show 1x05 1080p WEB-DL.avi` — same episode; the 1080p copy is kept.
- `[YTS.MX] Film Name 2019 720p BluRay.mkv` in one torrent folder and `www.site.com - Film.Name.2019.2160p.WEB-DL.mp4` in another — same film; the 2160p copy is kept.

Examples of what never matches:

- Different years: `Film.2019.1080p.mkv` vs `Film.2020.1080p.mkv`.
- Different episodes: `Show.S01E01.720p.mkv` vs `Show.S01E02.720p.mkv`, or an episode file vs a file with no episode code.
- Sequels: `Rocky.IV.720p.mkv` vs `Rocky.V.720p.mkv`, `Movie 2` vs `Movie 3`, `Saga Vol 1` vs `Saga Vol 2`, `Film Part 1` vs `Film Part 2`, `Disc CD1` vs `CD2` (these guards also apply to the fuzzy pass).
- Different movies whose parsed titles are not similar enough (ratio below 0.85).

Samples and leftovers: a video with the token `sample` (or inside a `Sample` folder) that is under 1/10 of the size of a non-sample video in the same torrent folder (or its parent) is removed with reason `sample`. After a video is removed, if its torrent folder (its parent, not the scan root) holds only junk (`.nfo .txt .jpg .jpeg .png .srt .sub .idx .ass .url .sfv .md5 .exe .lnk` files, sample videos, empty folders, or a `Subs`/`Sample` subfolder), the whole folder is removed with reason `torrent-leftover`. A folder holding any other file (for example a document or another non-sample video) is kept. `video-name` and `sample` removals count as duplicates found; `torrent-leftover` folders do not.

## Safety Notes

- **Always Dry-Run First**: Run with `--dry-run` to preview the planned removals before applying changes:
  ```bash
  python -m dedupe D:\MyFolder --dry-run
  ```
- **`--min-match` Aggressiveness Warning**: The `--min-match` rule is aggressive because files sharing 10+ contiguous characters (e.g. `report_draft.xlsx` and `report_final.xlsx`) will be treated as fuzzy duplicates. Always run with `--dry-run` first. To be safer, raise `N` (e.g. `--min-match 25`) or use `--min-match 0` to disable the character-match rule.
- **Recoverable by Default**: Removed files and folders are moved to `PATH\_dedupe_trash\<YYYYMMDD-HHMMSS>\` relative to `PATH`. To restore any item, simply move it back to its original relative location.
- **Permanent Deletion Warning**: When `--permanent` is supplied, files are deleted permanently and directories removed with `rmtree`. No trash folder is created. Use with caution.
