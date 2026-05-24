#!/usr/bin/env python3
"""
Fast recursive search for Henry Adams' "Rule of Phase Applied to History"

Per-disk thread pools, iterative walk, two-phase PDF scan, graceful Ctrl-C.

Usage:
    python3 find_adams_phase.py                    # auto-detects real mounts
    python3 find_adams_phase.py /home /mnt/disk2   # explicit roots
"""

import os, sys, re, zlib, zipfile, io, signal, threading, queue
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from datetime import datetime

# ── tunables ──────────────────────────────────────────────────────────────────
WORKERS_PER_DISK = 4
MAX_FILE_MB      = 3
SKIP_DIRS        = {
    '.git', 'node_modules', '__pycache__', '.Trash', 'lost+found',
    'System Volume Information', '$Recycle.Bin',
    'Windows', 'proc', 'sys', 'dev', 'run',
}

# ── graceful stop ─────────────────────────────────────────────────────────────
_stop = threading.Event()

def _sigint(sig, frame):
    if not _stop.is_set():
        print('\n\n  Ctrl-C received — stopping cleanly, please wait…', flush=True)
        _stop.set()

signal.signal(signal.SIGINT, _sigint)

# ── search targets ────────────────────────────────────────────────────────────
NAME_RE = re.compile(
    r'adams.?phase|rule.?of.?phase|phase.?applied|'
    r'degradation.?democratic|democratic.?dogma',
    re.IGNORECASE
)

# Ordered: cheapest / most distinctive first
CONTENT_SIGS: list[bytes] = [
    b'Rule of Phase Applied to History',
    b'RULE OF PHASE APPLIED TO HISTORY',
    b'Ethereal Phase',
    b'adams-phase',
    b'phase applied to history',
    b'Phase Applied to History',
]

READABLE_EXT = {
    '.txt', '.md', '.rst', '.html', '.htm', '.xml',
    '.pdf', '.epub', '.djvu',
    '.docx', '.odt', '.rtf', '.tex',
}

# ── text extraction ───────────────────────────────────────────────────────────

def _sigs_in(data: bytes) -> bool:
    lo = data.lower()
    return any(s.lower() in lo for s in CONTENT_SIGS)


def extract_pdf_streams(data: bytes) -> bytes:
    """Decompress only the zlib streams; skip if raw bytes already matched."""
    chunks = []
    for m in re.finditer(rb'stream\r?\n(.*?)\r?\nendstream', data, re.DOTALL):
        try:
            chunks.append(zlib.decompress(m.group(1)))
        except Exception:
            pass                     # not zlib, skip
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
    if _stop.is_set():
        return None

    # 1. filename — zero cost
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

    # 2. raw bytes pass (fast — no decompression)
    if _sigs_in(raw):
        return (path, 'CONTENT')

    # 3. for PDFs only: decompress streams and retry
    if ext == '.pdf':
        streams = extract_pdf_streams(raw)
        if streams and _sigs_in(streams):
            return (path, 'CONTENT(pdf)')

    # 4. docx: unzip xml
    if ext == '.docx':
        xml = extract_docx(raw)
        if xml and _sigs_in(xml):
            return (path, 'CONTENT(docx)')

    return None


# ── per-disk scanner ──────────────────────────────────────────────────────────

class DiskScanner:
    def __init__(self, roots, dev_id, out_q, stats, lock):
        self.roots  = roots
        self.dev_id = dev_id
        self.out_q  = out_q
        self.stats  = stats   # {'checked': 0, 'submitted': 0}
        self.lock   = lock
        self._pool  = ThreadPoolExecutor(
            max_workers=WORKERS_PER_DISK,
            thread_name_prefix=f'scan-{dev_id:#06x}'
        )

    def run(self):
        def _on_done(future):
            # Fires in the worker thread the moment the future completes —
            # concurrent with the walk, not after it.
            with self.lock:
                self.stats['checked'] += 1
            try:
                result = future.result()
                if result:
                    self.out_q.put(result)
            except Exception:
                pass

        try:
            for root in self.roots:
                if _stop.is_set():
                    break
                for path in self._walk(root):
                    if _stop.is_set():
                        break
                    try:
                        f = self._pool.submit(check_file, path)
                        f.add_done_callback(_on_done)   # ← result processed immediately
                        with self.lock:
                            self.stats['submitted'] += 1
                    except RuntimeError:
                        break
        finally:
            # wait=True blocks until every in-flight worker finishes,
            # so _on_done has fired for all futures before run() returns.
            self._pool.shutdown(wait=not _stop.is_set(),
                                cancel_futures=_stop.is_set())

    def _walk(self, root: Path):
        """Iterative DFS — no recursion stack, stays on same device."""
        stack = [root]
        while stack and not _stop.is_set():
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        if _stop.is_set():
                            return
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name in SKIP_DIRS:
                                    continue
                                if entry.name.startswith('.'):
                                    continue
                                # stay on same physical device
                                try:
                                    if os.stat(entry.path).st_dev != self.dev_id:
                                        continue
                                except OSError:
                                    continue
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                yield Path(entry.path)
                        except (PermissionError, OSError):
                            continue
            except (PermissionError, OSError):
                continue


# ── mount / root detection ────────────────────────────────────────────────────

def real_mounts() -> set[Path]:
    """Read /proc/mounts; return set of actual mount points."""
    mounts = set()
    try:
        with open('/proc/mounts') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    mounts.add(Path(parts[1]))
    except OSError:
        pass
    return mounts


def is_real_mountpoint(p: Path) -> bool:
    """True if p is in /proc/mounts or differs from its parent's st_dev."""
    try:
        return os.stat(p).st_dev != os.stat(p.parent).st_dev
    except OSError:
        return False


def group_by_device(roots: list[Path]) -> dict[int, list[Path]]:
    groups: dict[int, list[Path]] = defaultdict(list)
    seen_devs_for_path: set[tuple] = set()
    for root in roots:
        try:
            dev = os.stat(root).st_dev
            # deduplicate: don't add a path whose st_dev we already have
            # from a higher-level path (avoids re-walking same disk twice)
            key = (dev, str(root))
            if key not in seen_devs_for_path:
                seen_devs_for_path.add(key)
                groups[dev].append(root)
        except OSError as e:
            print(f'  ⚠  Cannot stat {root}: {e}')
    return groups


def default_roots() -> list[Path]:
    """Return home + actually-mounted media, deduplicated by real mount."""
    candidates = [Path.home()]
    mounts = real_mounts()

    for base in ['/media', '/mnt', '/Volumes', '/run/media']:
        bp = Path(base)
        if not bp.exists():
            continue
        try:
            for child in bp.iterdir():
                if child.is_dir() and (child in mounts or is_real_mountpoint(child)):
                    candidates.append(child)
        except PermissionError:
            pass

    if sys.platform == 'win32':
        import string
        for letter in string.ascii_uppercase:
            p = Path(f'{letter}:/')
            if p.exists():
                candidates.append(p)

    # keep only accessible paths
    return [r for r in candidates if r.exists()]


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    roots = [Path(a).expanduser().resolve() for a in sys.argv[1:]] \
            if len(sys.argv) > 1 else default_roots()

    if not roots:
        print('No search roots found.')
        sys.exit(1)

    groups = group_by_device(roots)

    print(f'\n{"="*62}')
    print(f'  Adams Phase Essay Finder  —  {datetime.now().strftime("%H:%M:%S")}')
    print(f'  Physical disks : {len(groups)}')
    for dev, dev_roots in groups.items():
        label = ', '.join(str(r) for r in dev_roots)
        print(f'    {dev:#010x}  →  {label}')
    print(f'  Workers/disk   : {WORKERS_PER_DISK}  '
          f'(total {len(groups)*WORKERS_PER_DISK})')
    print(f'{"="*62}\n')

    out_q  = queue.Queue()
    stats  = {'checked': 0, 'submitted': 0}
    lock   = threading.Lock()
    hits   = []

    scanners = [DiskScanner(dev_roots, dev_id, out_q, stats, lock)
                for dev_id, dev_roots in groups.items()]
    threads  = [threading.Thread(target=sc.run, daemon=True,
                                 name=f'walker-{i}')
                for i, sc in enumerate(scanners)]

    for t in threads:
        t.start()

    # ── progress loop (main thread) ───────────────────────────────────────────
    import time
    last_print = ''
    try:
        while any(t.is_alive() for t in threads):
            # drain hit queue
            while True:
                try:
                    hit = out_q.get_nowait()
                    hits.append(hit)
                    # clear progress line then print hit
                    print(f'\r{" "*len(last_print)}\r'
                          f'  ✓ [{hit[1]:14s}] {hit[0]}')
                except queue.Empty:
                    break

            with lock:
                c, s = stats['checked'], stats['submitted']
            last_print = f'  … {c:>9,} scanned / {s:>9,} queued / {len(hits)} hits'
            print(last_print, end='\r', flush=True)
            time.sleep(0.3)
    except KeyboardInterrupt:
        _stop.set()

    # final drain after threads finish
    for t in threads:
        t.join(timeout=5)
    while True:
        try:
            hit = out_q.get_nowait()
            hits.append(hit)
            print(f'  ✓ [{hit[1]:14s}] {hit[0]}')
        except queue.Empty:
            break

    with lock:
        c, s = stats['checked'], stats['submitted']

    print(f'\n\n{"="*62}')
    print(f'  {"Stopped" if _stop.is_set() else "Finished"}'
          f' : {datetime.now().strftime("%H:%M:%S")}')
    print(f'  Queued   : {s:,}')
    print(f'  Scanned  : {c:,}')
    print(f'  Matches  : {len(hits)}')
    print(f'{"="*62}')

    if hits:
        print('\nRESULTS:')
        for path, reason in sorted(hits, key=lambda x: str(x[0])):
            try:
                kb = path.stat().st_size / 1024
            except OSError:
                kb = 0
            print(f'  [{reason:14s}]  {path}  ({kb:.0f} KB)')
    else:
        print('\nNo matches found.')
        print('  • Run with sudo for system dirs')
        print('  • Add explicit paths: python3 find_adams_phase.py /mnt/disk2')
        print('  • File may be inside a .zip/.tar.gz (not searched yet)')


if __name__ == '__main__':
    main()
