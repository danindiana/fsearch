#!/usr/bin/env python3
"""
Fast recursive search for Henry Adams' "Rule of Phase Applied to History"

Key design: each physical disk gets its own independent thread pool so their
I/O pipelines never contend. Walk + scan are pipelined per disk (files are
scanned as they're found, not batch-collected first).

Usage:
    python3 find_adams_phase.py                    # auto-detects roots
    python3 find_adams_phase.py /home /mnt/disk2   # explicit roots
"""

import os
import sys
import re
import zlib
import zipfile
import io
import threading
import queue
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from datetime import datetime

# ── tunables ──────────────────────────────────────────────────────────────────
WORKERS_PER_DISK = 4       # scanner threads per physical disk
MAX_FILE_MB      = 3
SKIP_DIRS        = {
    '.git', 'node_modules', '__pycache__', '.Trash',
    'System Volume Information', '$Recycle.Bin',
    'Windows', 'proc', 'sys', 'dev', 'run',
}

# ── search targets ────────────────────────────────────────────────────────────
NAME_RE = re.compile(
    r'adams.?phase|rule.?of.?phase|phase.?applied|'
    r'degradation.?democratic|democratic.?dogma',
    re.IGNORECASE
)

CONTENT_SIGS = [
    b'Rule of Phase Applied to History',
    b'RULE OF PHASE APPLIED TO HISTORY',
    b'Ethereal Phase',
    b'adams-phase',
    b'Phase applied to History',
]

READABLE_EXT = {
    '.txt', '.md', '.rst', '.html', '.htm', '.xml',
    '.pdf', '.epub', '.djvu',
    '.docx', '.odt', '.rtf', '.tex',
}

# ── text extraction ───────────────────────────────────────────────────────────

def extract_pdf(data: bytes) -> bytes:
    chunks = [data]
    for m in re.finditer(rb'stream\r?\n(.*?)\r?\nendstream', data, re.DOTALL):
        try:
            chunks.append(zlib.decompress(m.group(1)))
        except Exception:
            chunks.append(m.group(1))
    return b' '.join(chunks)


def extract_docx(data: bytes) -> bytes:
    chunks = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                if name.endswith('.xml'):
                    chunks.append(z.read(name))
    except Exception:
        pass
    return b' '.join(chunks)


def check_file(path: Path) -> tuple[Path, str] | None:
    # 1. filename
    if NAME_RE.search(path.name):
        return (path, 'FILENAME')

    ext = path.suffix.lower()
    if ext not in READABLE_EXT:
        return None

    try:
        size = path.stat().st_size
        if size == 0 or size > MAX_FILE_MB * 1_048_576:
            return None
        raw = path.read_bytes()
    except OSError:
        return None

    text = extract_pdf(raw) if ext == '.pdf' else \
           extract_docx(raw) if ext == '.docx' else raw

    for sig in CONTENT_SIGS:
        if sig.lower() in text.lower():
            return (path, 'CONTENT')

    return None


# ── per-disk scanner ──────────────────────────────────────────────────────────

class DiskScanner:
    """
    Owns a thread pool for one physical device.
    Walker runs in a dedicated thread, feeding the pool as it finds files.
    Results are pushed to a shared output queue.
    """

    def __init__(self, roots: list[Path], device_id: int,
                 out_q: queue.Queue, counter: list, lock: threading.Lock):
        self.roots     = roots
        self.device_id = device_id
        self.out_q     = out_q
        self.counter   = counter   # [checked, total_submitted]
        self.lock      = lock
        self._futures  = []
        self._pool     = ThreadPoolExecutor(max_workers=WORKERS_PER_DISK,
                                            thread_name_prefix=f'disk{device_id}')
        self._done     = threading.Event()

    def run(self):
        """Called in its own thread. Walks + submits scan jobs, then waits."""
        for root in self.roots:
            self._walk(root)

        # drain futures
        for f in as_completed(self._future_iter()):
            with self.lock:
                self.counter[0] += 1
            try:
                result = f.result()
                if result:
                    self.out_q.put(result)
            except Exception:
                pass

        self._pool.shutdown(wait=False)
        self._done.set()

    def _walk(self, root: Path):
        try:
            for entry in os.scandir(root):
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name in SKIP_DIRS or entry.name.startswith('.'):
                            continue
                        # stay on same device
                        try:
                            if os.stat(entry.path).st_dev != self.device_id:
                                continue
                        except OSError:
                            continue
                        self._walk(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        with self.lock:
                            self.counter[1] += 1
                        f = self._pool.submit(check_file, Path(entry.path))
                        self._futures.append(f)
                except (PermissionError, OSError):
                    continue
        except (PermissionError, OSError):
            pass

    def _future_iter(self):
        # yield futures as they're appended (walk may still be running)
        # Use as_completed on a snapshot; works because walk and drain
        # are in the same thread here.
        return as_completed(self._futures)

    def wait(self):
        self._done.wait()


# ── device detection ──────────────────────────────────────────────────────────

def group_roots_by_device(roots: list[Path]) -> dict[int, list[Path]]:
    groups: dict[int, list[Path]] = defaultdict(list)
    for root in roots:
        try:
            dev = os.stat(root).st_dev
            groups[dev].append(root)
        except OSError as e:
            print(f"  ⚠  Cannot stat {root}: {e}")
    return groups


def default_roots() -> list[Path]:
    roots = [Path.home()]
    for candidate in ['/media', '/mnt', '/Volumes', '/run/media']:
        p = Path(candidate)
        if not p.exists():
            continue
        try:
            for child in p.iterdir():
                if child.is_dir():
                    roots.append(child)
        except PermissionError:
            pass
    if sys.platform == 'win32':
        import string
        for letter in string.ascii_uppercase:
            p = Path(f'{letter}:/')
            if p.exists():
                roots.append(p)
    return [r for r in roots if r.exists()]


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    roots = [Path(a).expanduser().resolve() for a in sys.argv[1:]] \
            if len(sys.argv) > 1 else default_roots()

    if not roots:
        print("No search roots found.")
        sys.exit(1)

    device_groups = group_roots_by_device(roots)

    print(f"\n{'='*62}")
    print(f"  Adams Phase Essay Finder  —  {datetime.now().strftime('%H:%M:%S')}")
    print(f"  Physical disks detected : {len(device_groups)}")
    for dev, dev_roots in device_groups.items():
        label = ', '.join(str(r) for r in dev_roots)
        print(f"    dev {dev:#010x}  →  {label}")
    print(f"  Threads per disk        : {WORKERS_PER_DISK}")
    print(f"  Total scanner threads   : {len(device_groups) * WORKERS_PER_DISK}")
    print(f"{'='*62}\n")

    out_q   = queue.Queue()
    counter = [0, 0]           # [checked, submitted]
    lock    = threading.Lock()
    hits    = []

    # launch one scanner thread per physical disk
    scanners = []
    threads  = []
    for dev_id, dev_roots in device_groups.items():
        sc = DiskScanner(dev_roots, dev_id, out_q, counter, lock)
        t  = threading.Thread(target=sc.run, daemon=True,
                              name=f'walker-{dev_id:#010x}')
        scanners.append(sc)
        threads.append(t)
        t.start()

    # progress reporter
    def reporter():
        import time
        while any(t.is_alive() for t in threads):
            with lock:
                c, s = counter
            # drain hits from queue
            while not out_q.empty():
                try:
                    hit = out_q.get_nowait()
                    hits.append(hit)
                    print(f"  ✓ [{hit[1]:8s}] {hit[0]}")
                except queue.Empty:
                    break
            print(f"  … {c:>8,} scanned / {s:>8,} found", end='\r', flush=True)
            time.sleep(0.5)

    rep = threading.Thread(target=reporter, daemon=True)
    rep.start()

    for t in threads:
        t.join()

    # final drain
    while not out_q.empty():
        try:
            hit = out_q.get_nowait()
            hits.append(hit)
            print(f"  ✓ [{hit[1]:8s}] {hit[0]}")
        except queue.Empty:
            break

    print(f"\n\n{'='*62}")
    print(f"  Finished : {datetime.now().strftime('%H:%M:%S')}")
    print(f"  Scanned  : {counter[0]:,} files")
    print(f"  Matches  : {len(hits)}")
    print(f"{'='*62}")

    if hits:
        print("\nRESULTS:")
        for path, reason in sorted(hits, key=lambda x: str(x[0])):
            try:
                kb = path.stat().st_size / 1024
            except OSError:
                kb = 0
            print(f"  [{reason:8s}]  {path}  ({kb:.0f} KB)")
    else:
        print("\nNo matches found. Tips:")
        print("  • Run with sudo to reach system directories")
        print("  • Add explicit paths: python3 find_adams_phase.py /mnt/disk2")
        print("  • File may be inside a .zip or .tar.gz archive (not searched)")


if __name__ == '__main__':
    main()
