#!/usr/bin/env python3
"""
fsearch — General-purpose fast recursive file search.

Per-disk thread pools, BFS walk, mmap content scan with early exit,
bounded backpressure, event-driven progress, graceful Ctrl-C.

Interactive:  python3 fsearch.py
CLI:          python3 fsearch.py -t "rule of phase" "ethereal phase"
              python3 fsearch.py -t "budget 2024" -e pdf docx -p /home /mnt/nas
              python3 fsearch.py -t secret -c          # case-sensitive
"""

import os, sys, re, zlib, zipfile, io, mmap, signal, threading, queue
import time, argparse
from dataclasses import dataclass
from pathlib import Path
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# ── constants ─────────────────────────────────────────────────────────────────

WORKERS_PER_DISK  = 6
BACKPRESSURE_MULT = 4      # max in-flight futures = workers × this
MAX_FILE_MB       = 50
SHUTDOWN_TIMEOUT  = 3.0    # seconds to wait for workers on Ctrl-C

SKIP_DIRS = {
    '.git', 'node_modules', '__pycache__', '.Trash', 'lost+found',
    'System Volume Information', '$Recycle.Bin',
    'Windows', 'proc', 'sys', 'dev', 'run',
}

READABLE_EXT = {
    '.txt', '.md', '.rst', '.html', '.htm', '.xml',
    '.pdf', '.epub', '.djvu',
    '.docx', '.odt', '.rtf', '.tex', '.csv', '.log',
}

# ── graceful stop ─────────────────────────────────────────────────────────────

_stop = threading.Event()

def _sigint(sig, frame):
    # Only set the flag here — printing from a signal handler is unsafe
    # (can interleave with main thread I/O).  The main loop prints the message.
    _stop.set()

signal.signal(signal.SIGINT, _sigint)

# ── search config ─────────────────────────────────────────────────────────────

@dataclass
class SearchConfig:
    terms:          list[str]       # raw terms as entered
    name_re:        re.Pattern      # compiled text regex for filenames
    content_re:     re.Pattern      # compiled bytes regex for content (mmap-safe)
    extensions:     set[str]        # empty → use READABLE_EXT default
    case_sensitive: bool
    roots:          list[Path]

    @property
    def effective_extensions(self) -> set[str]:
        return self.extensions if self.extensions else READABLE_EXT

    def describe(self) -> str:
        terms_str = ',  '.join(f'"{t}"' for t in self.terms)
        exts = ', '.join(sorted(self.extensions)) if self.extensions \
               else 'all readable types'
        case  = 'sensitive' if self.case_sensitive else 'insensitive'
        paths = ', '.join(str(r) for r in self.roots)
        return (f'  Terms      : {terms_str}\n'
                f'  File types : {exts}\n'
                f'  Case       : {case}\n'
                f'  Paths      : {paths}')


def build_config(terms, extensions, case_sensitive, roots) -> SearchConfig:
    flags = 0 if case_sensitive else re.IGNORECASE
    # Text regex for filenames
    name_re = re.compile('|'.join(re.escape(t) for t in terms), flags)
    # Bytes regex for mmap content scanning — single pattern, early exit on match
    content_re = re.compile(
        b'|'.join(re.escape(t.encode()) for t in terms), flags
    )
    exts = {('.' + e.lstrip('.').lower()) for e in extensions} if extensions else set()
    return SearchConfig(terms, name_re, content_re, exts, case_sensitive, roots)


# ── text extraction ───────────────────────────────────────────────────────────

def extract_pdf_streams(data: bytes) -> bytes:
    """Decompress zlib streams from a PDF.  zlib.decompress() releases the GIL."""
    chunks = []
    for m in re.finditer(rb'stream\r?\n(.*?)\r?\nendstream', data, re.DOTALL):
        try:
            chunks.append(zlib.decompress(m.group(1)))
        except Exception:
            pass
    return b' '.join(chunks)


def extract_docx_xml(data: bytes) -> bytes:
    chunks = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                if name.endswith('.xml'):
                    chunks.append(z.read(name))
    except Exception:
        pass
    return b' '.join(chunks)


# ── file checker ──────────────────────────────────────────────────────────────

def check_file(path: Path, cfg: SearchConfig) -> tuple[Path, str] | None:
    if _stop.is_set():
        return None

    # Gate 1: filename — zero I/O, regex on string already in memory
    if cfg.name_re.search(path.name):
        return (path, 'filename')

    # Gate 2: extension filter
    ext = path.suffix.lower()
    if ext not in cfg.effective_extensions:
        return None

    # Gate 3: size — one stat syscall
    try:
        size = path.stat().st_size
        if size == 0 or size > MAX_FILE_MB * 1_048_576:
            return None
    except OSError:
        return None

    # Gates 4-6: open once, mmap for zero-copy scan with early exit
    try:
        with open(path, 'rb') as f:
            try:
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    # Gate 4: raw content scan — re.search on mmap exits at first match
                    if cfg.content_re.search(mm):
                        return (path, 'content')

                    # Gates 5/6 need full decompression — read from mmap once
                    if ext == '.pdf':
                        raw = bytes(mm)
                        if cfg.content_re.search(extract_pdf_streams(raw)):
                            return (path, 'content(pdf)')

                    elif ext in ('.docx', '.odt'):
                        raw = bytes(mm)
                        if cfg.content_re.search(extract_docx_xml(raw)):
                            return (path, 'content(docx)')

            except (mmap.error, ValueError):
                # mmap fails on empty files and some special files — fallback
                f.seek(0)
                raw = f.read()
                if cfg.content_re.search(raw):
                    return (path, 'content')

    except OSError:
        return None

    return None


# ── per-disk scanner ──────────────────────────────────────────────────────────

class DiskScanner:
    def __init__(self, roots, dev_id, cfg, out_q, stats, stats_lock,
                 known_mounts):
        self.roots        = roots
        self.dev_id       = dev_id
        self.cfg          = cfg
        self.out_q        = out_q
        self.stats        = stats
        self.stats_lock   = stats_lock
        self.known_mounts = known_mounts
        # Semaphore caps in-flight futures → prevents unbounded queue growth
        self._backpressure = threading.Semaphore(
            WORKERS_PER_DISK * BACKPRESSURE_MULT
        )
        self._pool = ThreadPoolExecutor(
            max_workers=WORKERS_PER_DISK,
            thread_name_prefix=f'scan-{dev_id:#06x}',
        )

    def run(self):
        def _on_done(future):
            self._backpressure.release()          # unblock the walker
            with self.stats_lock:
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
                    self._backpressure.acquire()  # blocks when queue is full
                    if _stop.is_set():
                        self._backpressure.release()
                        break
                    try:
                        f = self._pool.submit(check_file, path, self.cfg)
                        f.add_done_callback(_on_done)
                        with self.stats_lock:
                            self.stats['submitted'] += 1
                    except RuntimeError:
                        self._backpressure.release()
                        break
        finally:
            self._pool.shutdown(
                wait=not _stop.is_set(),
                cancel_futures=_stop.is_set(),
            )

    def _walk(self, root: Path):
        """BFS walk — shallower (user-visible) directories scanned first.
        Uses DirEntry.stat() only for known mount points to minimise syscalls.
        """
        q = deque([root])
        while q and not _stop.is_set():
            current = q.popleft()
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
                                ep = Path(entry.path)
                                # Only pay the stat() cost at actual mount points;
                                # normal subdirs are always on the same device.
                                if ep in self.known_mounts:
                                    try:
                                        if entry.stat(follow_symlinks=False
                                                      ).st_dev != self.dev_id:
                                            continue
                                    except OSError:
                                        continue
                                q.append(ep)
                            elif entry.is_file(follow_symlinks=False):
                                yield Path(entry.path)
                        except (PermissionError, OSError):
                            continue
            except (PermissionError, OSError):
                continue


# ── disk / mount detection ────────────────────────────────────────────────────

def real_mounts() -> set[Path]:
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
    try:
        return os.stat(p).st_dev != os.stat(p.parent).st_dev
    except OSError:
        return False


def group_by_device(roots: list[Path]) -> dict[int, list[Path]]:
    groups: dict[int, list[Path]] = defaultdict(list)
    seen: set[tuple] = set()
    for root in roots:
        try:
            dev = os.stat(root).st_dev
            key = (dev, str(root))
            if key not in seen:
                seen.add(key)
                groups[dev].append(root)
        except OSError as e:
            print(f'  ⚠  Cannot stat {root}: {e}')
    return groups


def default_roots() -> list[Path]:
    candidates = [Path.home()]
    mounts = real_mounts()
    for base in ['/media', '/mnt', '/Volumes', '/run/media']:
        bp = Path(base)
        if not bp.exists():
            continue
        try:
            for child in bp.iterdir():
                if child.is_dir() and (child in mounts or
                                       is_real_mountpoint(child)):
                    candidates.append(child)
        except PermissionError:
            pass
    if sys.platform == 'win32':
        import string
        for letter in string.ascii_uppercase:
            p = Path(f'{letter}:/')
            if p.exists():
                candidates.append(p)
    return [r for r in candidates if r.exists()]


# ── interactive prompts ───────────────────────────────────────────────────────

def prompt(label: str, hint: str = '', default: str = '') -> str:
    hint_str    = f'  ({hint})' if hint else ''
    default_str = f'  [{default}]' if default else ''
    print(f'\n  {label}{hint_str}{default_str}')
    try:
        val = input('  > ').strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val if val else default


def prompt_config() -> SearchConfig:
    W = 62
    print(f'\n{"─"*W}\n  fsearch  —  Fast Multi-Disk File Search\n{"─"*W}')

    raw_terms = ''
    while not raw_terms:
        raw_terms = prompt('Search terms',
                           hint='comma-separated; matched in filenames AND content')
        if not raw_terms:
            print('  ✗  At least one search term is required.')
    terms = [t.strip() for t in raw_terms.split(',') if t.strip()]

    raw_exts = prompt('File types',
                      hint='e.g. pdf, txt, docx — Enter for all readable types')
    extensions = [e.strip() for e in raw_exts.split(',') if e.strip()]

    case_raw       = prompt('Case sensitive?', hint='y / N', default='n').lower()
    case_sensitive = case_raw in ('y', 'yes')

    raw_paths = prompt('Paths to search',
                       hint='space or comma-separated — Enter to auto-detect')
    if raw_paths:
        roots = [Path(p).expanduser().resolve()
                 for p in raw_paths.replace(',', ' ').split() if p.strip()]
        roots = [r for r in roots if r.exists()]
        if not roots:
            print('  ⚠  No valid paths — falling back to auto-detect.')
            roots = default_roots()
    else:
        roots = default_roots()

    cfg = build_config(terms, extensions, case_sensitive, roots)
    print(f'\n{"─"*W}\n  Ready to search:\n{cfg.describe()}\n{"─"*W}')
    if prompt('Start?', hint='Y / n', default='y').lower() in ('n', 'no'):
        print('  Cancelled.')
        sys.exit(0)
    return cfg


# ── CLI parser ────────────────────────────────────────────────────────────────

def parse_cli() -> SearchConfig | None:
    if len(sys.argv) == 1:
        return None

    p = argparse.ArgumentParser(
        prog='fsearch',
        description='Fast multi-disk file search — filenames and content.',
        epilog=('examples:\n'
                '  fsearch.py -t "rule of phase"\n'
                '  fsearch.py -t "Q3 budget" -e pdf docx -p /home /mnt/nas\n'
                '  fsearch.py -t secret -c'),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('-t', '--terms', nargs='+', required=True, metavar='TERM')
    p.add_argument('-p', '--paths', nargs='+', metavar='PATH')
    p.add_argument('-e', '--ext',   nargs='+', metavar='EXT')
    p.add_argument('-c', '--case',  action='store_true',
                   help='case-sensitive (default: insensitive)')
    args = p.parse_args()

    roots = ([Path(x).expanduser().resolve() for x in args.paths]
             if args.paths else default_roots())
    roots = [r for r in roots if r.exists()]
    return build_config(args.terms, args.ext or [], args.case, roots)


# ── search runner ─────────────────────────────────────────────────────────────

def run_search(cfg: SearchConfig) -> list[tuple[Path, str]]:
    groups  = group_by_device(cfg.roots)
    mounts  = real_mounts()
    W       = 62

    print(f'\n{"="*W}')
    print(f'  fsearch  —  {datetime.now().strftime("%H:%M:%S")}')
    print(f'  Disks   : {len(groups)}')
    for dev, dev_roots in groups.items():
        print(f'    {dev:#010x}  →  {", ".join(str(r) for r in dev_roots)}')
    print(f'  Workers : {WORKERS_PER_DISK}/disk  '
          f'({len(groups) * WORKERS_PER_DISK} total)  '
          f'· backpressure cap {WORKERS_PER_DISK * BACKPRESSURE_MULT}/disk')
    print(f'{"="*W}\n')

    out_q      = queue.Queue()
    stats      = {'checked': 0, 'submitted': 0}
    stats_lock = threading.Lock()
    hits: list[tuple[Path, str]] = []
    start_time = time.monotonic()

    scanners = [
        DiskScanner(dev_roots, dev_id, cfg, out_q, stats, stats_lock, mounts)
        for dev_id, dev_roots in groups.items()
    ]
    threads = [
        threading.Thread(target=sc.run, daemon=True, name=f'walker-{i}')
        for i, sc in enumerate(scanners)
    ]
    for t in threads:
        t.start()

    stop_announced = False
    last_line      = ''

    try:
        while any(t.is_alive() for t in threads):
            if _stop.is_set() and not stop_announced:
                print(f'\r{" " * len(last_line)}\r'
                      '  Ctrl-C — stopping cleanly, please wait…', flush=True)
                stop_announced = True

            # Event-driven: block until a hit arrives or 0.3s elapses
            try:
                hit = out_q.get(timeout=0.3)
                hits.append(hit)
                print(f'\r{" " * len(last_line)}\r'
                      f'  ✓  [{hit[1]:14s}]  {hit[0]}')
            except queue.Empty:
                pass

            # Update progress line
            with stats_lock:
                c, s = stats['checked'], stats['submitted']
            elapsed = time.monotonic() - start_time
            rate    = f'{c / elapsed:,.0f}/s' if elapsed > 1 else '…'
            last_line = (f'  …  {c:>9,} scanned'
                         f'  /  {s:>9,} queued'
                         f'  /  {len(hits)} hit{"s" if len(hits) != 1 else ""}'
                         f'  [{rate}]')
            print(last_line, end='\r', flush=True)

    except KeyboardInterrupt:
        _stop.set()

    for t in threads:
        t.join(timeout=SHUTDOWN_TIMEOUT)

    # Final drain
    while True:
        try:
            hit = out_q.get_nowait()
            hits.append(hit)
            print(f'  ✓  [{hit[1]:14s}]  {hit[0]}')
        except queue.Empty:
            break

    with stats_lock:
        c, s = stats['checked'], stats['submitted']
    elapsed = time.monotonic() - start_time
    rate    = f'{c / elapsed:,.0f} files/s' if elapsed > 0 else 'n/a'
    status  = 'Stopped' if _stop.is_set() else 'Done'

    print(f'\n\n{"="*W}')
    print(f'  {status:<10}  {datetime.now().strftime("%H:%M:%S")}')
    print(f'  Queued  :  {s:>10,}')
    print(f'  Scanned :  {c:>10,}  ({rate})')
    print(f'  Matches :  {len(hits):>10,}')
    print(f'{"="*W}')

    if hits:
        print('\nResults:')
        for path, reason in sorted(hits, key=lambda x: str(x[0])):
            try:
                kb = path.stat().st_size / 1024
            except OSError:
                kb = 0
            print(f'  [{reason:14s}]  {path}  ({kb:.0f} KB)')
    else:
        print('\n  No matches found.')
        print('  Tip: content search only covers readable file types.')

    return hits


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    cfg = parse_cli() or prompt_config()
    run_search(cfg)


if __name__ == '__main__':
    main()
