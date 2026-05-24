#!/usr/bin/env python3
"""
Fast recursive search for Henry Adams' "Rule of Phase Applied to History"
Searches by filename pattern AND content (PDF/TXT/HTML/DOCX).

Usage:
    python3 find_adams_phase.py                   # searches common locations
    python3 find_adams_phase.py /path/to/search   # searches specific path
    python3 find_adams_phase.py /path1 /path2     # multiple roots
"""

import os
import sys
import re
import zlib
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# ── tunables ──────────────────────────────────────────────────────────────────
MAX_WORKERS   = 28          # parallel file readers
MAX_FILE_MB   = 3         # skip files larger than this (MB)
SKIP_DIRS     = {'.git', 'node_modules', '__pycache__', '.Trash',
                 'System Volume Information', '$Recycle.Bin',
                 'Windows', 'proc', 'sys', 'dev'}

# ── what we're looking for ────────────────────────────────────────────────────
NAME_PATTERNS = [
    r'adams.?phase',
    r'rule.?of.?phase',
    r'phase.?applied',
    r'degradation.?democratic',
    r'democratic.?dogma',
]
NAME_RE = re.compile('|'.join(NAME_PATTERNS), re.IGNORECASE)

CONTENT_PATTERNS = [
    b'Rule of Phase Applied to History',
    b'rule of phase applied to history',
    b'RULE OF PHASE APPLIED TO HISTORY',
    b'Adams.*phase.*history',
    b'Ethereal Phase',
    b'ethereal phase',
    b'adams-phase',
]

READABLE_EXT = {'.txt', '.md', '.rst', '.html', '.htm', '.xml',
                '.pdf', '.epub', '.djvu',
                '.docx', '.odt', '.rtf',
                '.tex', '.log'}

# ── helpers ───────────────────────────────────────────────────────────────────

def scan_text(data: bytes) -> bool:
    """Check raw bytes for any content signature."""
    for pat in CONTENT_PATTERNS:
        if re.search(pat, data, re.IGNORECASE):
            return True
    return False


def extract_pdf_text(data: bytes) -> bytes:
    """Very fast PDF text extraction — no dependencies, stream-level only."""
    # grab all stream objects and decompress if possible
    chunks = []
    for m in re.finditer(rb'stream\r?\n(.*?)\r?\nendstream', data, re.DOTALL):
        raw = m.group(1)
        try:
            chunks.append(zlib.decompress(raw))
        except Exception:
            chunks.append(raw)
    # also grab raw text operators
    chunks.append(data)
    return b' '.join(chunks)


def extract_docx_text(data: bytes) -> bytes:
    """Pull text from a .docx (ZIP) without python-docx."""
    import zipfile, io
    chunks = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                if name.endswith('.xml'):
                    chunks.append(z.read(name))
    except Exception:
        pass
    return b' '.join(chunks)


def check_file(path: Path) -> str | None:
    """
    Returns a human-readable match reason, or None if no match.
    First checks filename, then content for readable extensions.
    """
    name = path.name

    # 1. filename match
    if NAME_RE.search(name):
        return "FILENAME MATCH"

    # 2. content match (only for known readable types, size-limited)
    ext = path.suffix.lower()
    if ext not in READABLE_EXT:
        return None

    try:
        size = path.stat().st_size
    except OSError:
        return None

    if size > MAX_FILE_MB * 1024 * 1024 or size == 0:
        return None

    try:
        raw = path.read_bytes()
    except (PermissionError, OSError):
        return None

    # dispatch by type
    if ext == '.pdf':
        text = extract_pdf_text(raw)
    elif ext == '.docx':
        text = extract_docx_text(raw)
    else:
        text = raw

    if scan_text(text):
        return "CONTENT MATCH"

    return None


def walk_tree(root: Path, file_queue: list, lock: threading.Lock):
    """Single-threaded directory walk — fills file_queue."""
    try:
        for entry in os.scandir(root):
            if entry.name.startswith('.') and entry.name != '.':
                pass  # still process hidden files, just skip hidden dirs below
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in SKIP_DIRS:
                        continue
                    walk_tree(Path(entry.path), file_queue, lock)
                elif entry.is_file(follow_symlinks=False):
                    with lock:
                        file_queue.append(Path(entry.path))
            except (PermissionError, OSError):
                continue
    except (PermissionError, OSError):
        pass


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    # Default search roots
    if len(sys.argv) > 1:
        roots = [Path(a).expanduser() for a in sys.argv[1:]]
    else:
        roots = []
        # Home directory
        roots.append(Path.home())
        # Common mount points on Linux/macOS
        for candidate in ['/media', '/mnt', '/Volumes', '/run/media']:
            p = Path(candidate)
            if p.exists():
                try:
                    roots.extend(p.iterdir())
                except PermissionError:
                    pass
        # Windows drives
        if sys.platform == 'win32':
            import string
            for letter in string.ascii_uppercase:
                p = Path(f'{letter}:/')
                if p.exists():
                    roots.append(p)

    roots = [r for r in roots if r.exists()]
    if not roots:
        print("No search roots found.")
        sys.exit(1)

    print(f"\n{'='*60}")
    print("  Adams Phase Essay Finder")
    print(f"  Started: {datetime.now().strftime('%H:%M:%S')}")
    print(f"  Searching: {', '.join(str(r) for r in roots)}")
    print(f"  Workers: {MAX_WORKERS}")
    print(f"{'='*60}\n")

    # Collect all files first (fast walk), then process in parallel
    file_queue: list = []
    lock = threading.Lock()

    print("Walking directory tree... ", end='', flush=True)
    for root in roots:
        walk_tree(root, file_queue, lock)
    print(f"{len(file_queue):,} files found.\n")

    hits = []
    checked = 0
    print("Scanning files...")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(check_file, p): p for p in file_queue}
        for future in as_completed(futures):
            checked += 1
            path = futures[future]
            try:
                reason = future.result()
                if reason:
                    hits.append((path, reason))
                    print(f"  ✓ [{reason}] {path}")
            except Exception:
                pass
            if checked % 5000 == 0:
                print(f"  ... {checked:,}/{len(file_queue):,} checked, "
                      f"{len(hits)} hits so far", flush=True)

    print(f"\n{'='*60}")
    print(f"  Done: {datetime.now().strftime('%H:%M:%S')}")
    print(f"  Files scanned: {checked:,}")
    print(f"  Matches found: {len(hits)}")
    print(f"{'='*60}")

    if hits:
        print("\nFULL RESULTS:")
        for path, reason in sorted(hits, key=lambda x: x[1]):
            size = path.stat().st_size if path.exists() else 0
            print(f"  [{reason}] {path}  ({size/1024:.1f} KB)")
    else:
        print("\nNo matches found.")
        print("\nTips:")
        print("  - Try running with sudo for system directories")
        print("  - Specify additional paths: python3 find_adams_phase.py /path/to/search")
        print("  - The file may be in a compressed archive (.zip, .tar.gz)")


if __name__ == '__main__':
    main()
