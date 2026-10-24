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
usage: dedupe [-h] [--dry-run] [--permanent] [--no-fuzzy]
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
```

---

## Order of Passes

1. **Pass 1 - Duplicate Folders**:
   - Compares non-empty directories across the tree. A folder is non-empty if it contains at least one file with size > 0.
   - Matching is based on identical relative file paths and SHA-256 hashes.
   - Removes duplicate folders whole. Only top-most folders are handled; matching sub-folders are not reported separately.
   - Keeper selection: shallowest directory depth, then shortest folder name, then alphabetical order.

2. **Pass 2 - Exact Duplicate Files**:
   - Compares surviving regular files across the entire tree.
   - Files are grouped by size, then hashed in chunks using SHA-256. Zero-byte files are skipped.
   - Keeper selection: shallowest directory depth, then shortest file name, then oldest modification time (`mtime`), then alphabetical order.

3. **Pass 3 - Fuzzy-Name Duplicate Files**:
   - Skipped if `--no-fuzzy` is specified. Zero-byte files are skipped.
   - Compares files only within the **same folder** and having the **same extension** (case-insensitive).
   - Normalizes stems: converts to lowercase, strips copy markers (`copy of ` prefix, ` (n)` suffix, ` - copy` / ` - copy (n)` suffix, `_copy`/` copy`/`-copy` with optional digits), and collapses whitespace/underscores/hyphens to a single space.
   - Matches when EITHER `difflib.SequenceMatcher` ratio of normalized stems >= threshold (default `0.9`) OR the longest common contiguous substring of normalized stems (via `difflib.SequenceMatcher.find_longest_match`) is at least `N` characters (`--min-match N`, default `10`; `0` disables this rule), **except** when stems differ only in digits (which are never duplicates).
   - Matches are transitive: files are grouped using union-find.
   - Keeper selection: one file is kept per group (a file with no copy marker in its name, then newest modification time (`mtime`), then shortest name, then alphabetical order). The printed reason for removals is `fuzzy`.

---

## Safety Notes

- **Always Dry-Run First**: Run with `--dry-run` to preview the planned removals before applying changes:
  ```bash
  python -m dedupe D:\MyFolder --dry-run
  ```
- **`--min-match` Aggressiveness Warning**: The `--min-match` rule is aggressive because files sharing 10+ contiguous characters (e.g. `report_draft.xlsx` and `report_final.xlsx`) will be treated as fuzzy duplicates. Always run with `--dry-run` first. To be safer, raise `N` (e.g. `--min-match 25`) or use `--min-match 0` to disable the character-match rule.
- **Recoverable by Default**: Removed files and folders are moved to `PATH\_dedupe_trash\<YYYYMMDD-HHMMSS>\` relative to `PATH`. To restore any item, simply move it back to its original relative location.
- **Permanent Deletion Warning**: When `--permanent` is supplied, files are deleted permanently and directories removed with `rmtree`. No trash folder is created. Use with caution.
