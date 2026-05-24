#!/usr/bin/env python3
"""
fsearch — General-purpose fast recursive file search.

Searches filenames AND file contents across multiple disks in parallel,
with one independent thread pool per physical device.

Interactive mode (no args):
    python3 fsearch.py

CLI mode (skip prompts):
    python3 fsearch.py -t "rule of phase" "ethereal phase" -p /home /mnt/raid0
    python3 fsearch.py -t "budget 2024" -e pdf docx -c          # case-sensitive
    python3 fsearch.py --help
"""

import os, sys, re, zlib, zipfile, io, signal, threading, queue, time, argparse
from dataclasses import dataclass, field
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from datetime import datetime

# ── constants ─────────────────────────────────────────────────────────────────

WORKERS_PER_DISK = 6
MAX_FILE_MB      = 50
SKIP_DIRS        = {
    '.git', 'node_modules', '__pycache__', '.Trash', 'lost+found',
    'System Volume Information', '$Recycle.Bin',
    'Windows', 'proc', 'sys', 'dev', 'run',
}

# Default extensions whose contents are worth reading
READABLE_EXT = {
    '.txt', '.md', '.rst', '.html', '.htm', '.xml',
    '.pdf', '.epub', '.djvu',
    '.docx', '.odt', '.rtf', '.tex', '.csv', '.log',
}

# ── graceful stop ─────────────────────────────────────────────────────────────

_stop = threading.Event()

def _sigint(sig, frame):
    if not _stop.is_set():
        print('\n\n  Ctrl-C — stopping cleanly, please wait…', flush=True)
        _stop.set()

signal.signal(signal.SIGINT, _sigint)

# ── search config ─────────────────────────────────────────────────────────────

@dataclass
class SearchConfig:
    terms:          list[str]          # raw terms as entered by user
    name_re:        re.Pattern         # compiled pattern for filename matching
    content_sigs:   list[bytes]        # encoded terms for content matching
    extensions:     set[str]           # empty set → use READABLE_EXT default
    case_sensitive: bool
    roots:          list[Path]

    @property
    def effective_extensions(self) -> set[str]:
        return self.extensions if self.extensions else READABLE_EXT

    def describe(self) -> str:
        lines = []
        terms_str = ',  '.join(f'"{t}"' for t in self.terms)
        lines.append(f'  Terms      : {terms_str}')
        exts = ', '.join(sorted(self.extensions)) if self.extensions \
               else 'all readable types'
        lines.append(f'  File types : {exts}')
        lines.append(f'  Case       : {"sensitive" if self.case_sensitive else "insensitive"}')
        lines.append(f'  Paths      : {", ".join(str(r) for r in self.roots)}')
        return '\n'.join(lines)


def build_config(terms: list[str], extensions: list[str],
                 case_sensitive: bool, roots: list[Path]) -> SearchConfig:
    flags   = 0 if case_sensitive else re.IGNORECASE
    pattern = '|'.join(re.escape(t) for t in terms)
    name_re = re.compile(pattern, flags)
    encode  = str.encode
    if case_sensitive:
        sigs = [encode(t) for t in terms]
    else:
        # store both cases; raw bytes search is case-folded at match time
        sigs = [encode(t) for t in terms]
    exts = {('.' + e.lstrip('.').lower()) for e in extensions} if extensions else set()
    return SearchConfig(
        terms          = terms,
        name_re        = name_re,
        content_sigs   = sigs,
        extensions     = exts,
        case_sensitive = case_sensitive,
        roots          = roots,
    )

# ── text extraction ───────────────────────────────────────────────────────────

def _sigs_in(data: bytes, cfg: SearchConfig) -> bool:
    hay = data if cfg.case_sensitive else data.lower()
    sigs = cfg.content_sigs if cfg.case_sensitive \
           else [s.lower() for s in cfg.content_sigs]
    return any(s in hay for s in sigs)


def extract_pdf_streams(data: bytes) -> bytes:
    chunks = []
    for m in re.finditer(rb'stream\r?\n(.*?)\r?\nendstream', data, re.DOTALL):
        try:
            chunks.append(zlib.decompress(m.group(1)))
        except Exception:
            pass
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


# ── file checker ──────────────────────────────────────────────────────────────

def check_file(path: Path, cfg: SearchConfig) -> tuple[Path, str] | None:
    if _stop.is_set():
        return None

    # Gate 1: filename — zero I/O
    if cfg.name_re.search(path.name):
        return (path, 'filename')

    # Gate 2: extension filter
    ext = path.suffix.lower()
    if ext not in cfg.effective_extensions:
        return None

    # Gate 3: size — one stat call
    try:
        size = path.stat().st_size
        if size == 0 or size > MAX_FILE_MB * 1_048_576:
            return None
        raw = path.read_bytes()
    except OSError:
        return None

    # Gate 4: raw bytes scan — no decompression
    if _sigs_in(raw, cfg):
        return (path, 'content')

    # Gate 5: PDF stream decompression — only if raw scan missed
    if ext == '.pdf':
        streams = extract_pdf_streams(raw)
        if streams and _sigs_in(streams, cfg):
            return (path, 'content(pdf)')

    # Gate 6: DOCX XML extraction
    if ext in ('.docx', '.odt'):
        xml = extract_docx(raw)
        if xml and _sigs_in(xml, cfg):
            return (path, 'content(docx)')

    return None


# ── per-disk scanner ──────────────────────────────────────────────────────────

class DiskScanner:
    def __init__(self, roots, dev_id, cfg, out_q, stats, lock):
        self.roots  = roots
        self.dev_id = dev_id
        self.cfg    = cfg
        self.out_q  = out_q
        self.stats  = stats
        self.lock   = lock
        self._pool  = ThreadPoolExecutor(
            max_workers=WORKERS_PER_DISK,
            thread_name_prefix=f'scan-{dev_id:#06x}',
        )

    def run(self):
        def _on_done(future):
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
                        f = self._pool.submit(check_file, path, self.cfg)
                        f.add_done_callback(_on_done)
                        with self.lock:
                            self.stats['submitted'] += 1
                    except RuntimeError:
                        break
        finally:
            self._pool.shutdown(
                wait=not _stop.is_set(),
                cancel_futures=_stop.is_set(),
            )

    def _walk(self, root: Path):
        """Iterative DFS — no recursion, stays on same physical device."""
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
    return [r for r in candidates if r.exists()]


# ── interactive prompts ───────────────────────────────────────────────────────

def prompt(label: str, hint: str = '', default: str = '') -> str:
    """Print a labelled prompt and return stripped input."""
    hint_str  = f'  ({hint})' if hint else ''
    default_str = f'  [{default}]' if default else ''
    print(f'\n  {label}{hint_str}{default_str}')
    try:
        val = input('  > ').strip()
    except (EOFError, KeyboardInterrupt):
        print()
        _stop.set()
        sys.exit(0)
    return val if val else default


def prompt_config() -> SearchConfig:
    width = 62
    print(f'\n{"─"*width}')
    print(f'  fsearch  —  Fast Multi-Disk File Search')
    print(f'{"─"*width}')

    # ── terms ─────────────────────────────────────────────────────────────────
    raw_terms = ''
    while not raw_terms:
        raw_terms = prompt(
            'Search terms',
            hint='comma-separated; matched in filenames AND file contents',
        )
        if not raw_terms:
            print('  ✗  At least one search term is required.')
    terms = [t.strip() for t in raw_terms.split(',') if t.strip()]

    # ── file types ────────────────────────────────────────────────────────────
    raw_exts = prompt(
        'File types to search',
        hint='e.g.  pdf, txt, docx  — or Enter for all readable types',
    )
    extensions = [e.strip() for e in raw_exts.split(',') if e.strip()]

    # ── case sensitivity ──────────────────────────────────────────────────────
    case_raw      = prompt('Case sensitive?', hint='y / N', default='n').lower()
    case_sensitive = case_raw in ('y', 'yes')

    # ── paths ─────────────────────────────────────────────────────────────────
    raw_paths = prompt(
        'Paths to search',
        hint='space or comma-separated — or Enter to auto-detect all disks',
    )
    if raw_paths:
        # accept both space- and comma-separated
        raw_paths = raw_paths.replace(',', ' ')
        roots = [Path(p).expanduser().resolve()
                 for p in raw_paths.split() if p.strip()]
        roots = [r for r in roots if r.exists()]
        if not roots:
            print('  ⚠  No valid paths given — falling back to auto-detect.')
            roots = default_roots()
    else:
        roots = default_roots()

    cfg = build_config(terms, extensions, case_sensitive, roots)

    # ── confirmation ──────────────────────────────────────────────────────────
    print(f'\n{"─"*width}')
    print('  Ready to search:')
    print(cfg.describe())
    print(f'{"─"*width}')
    go = prompt('Start?', hint='Y / n', default='y').lower()
    if go in ('n', 'no'):
        print('  Cancelled.')
        sys.exit(0)

    return cfg


# ── argument parser (CLI / non-interactive mode) ──────────────────────────────

def parse_cli() -> SearchConfig | None:
    """Return a SearchConfig from CLI args, or None to trigger interactive mode."""
    if len(sys.argv) == 1:
        return None  # no args → interactive

    parser = argparse.ArgumentParser(
        prog='fsearch',
        description='Fast multi-disk file search — filenames and content.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap_dedent('''\
            examples:
              python3 fsearch.py -t "rule of phase"
              python3 fsearch.py -t "budget 2024" "q3 forecast" -e pdf docx xlsx
              python3 fsearch.py -t secret -p /home /mnt/nas -c
        '''),
    )
    parser.add_argument(
        '-t', '--terms', nargs='+', required=True, metavar='TERM',
        help='one or more search terms (filename + content)',
    )
    parser.add_argument(
        '-p', '--paths', nargs='+', metavar='PATH',
        help='paths to search (default: auto-detect all disks)',
    )
    parser.add_argument(
        '-e', '--ext', nargs='+', metavar='EXT',
        help='file extensions to search content of (default: all readable)',
    )
    parser.add_argument(
        '-c', '--case', action='store_true',
        help='case-sensitive matching (default: case-insensitive)',
    )
    args = parser.parse_args()

    if args.paths:
        roots = [Path(p).expanduser().resolve() for p in args.paths]
        roots = [r for r in roots if r.exists()]
    else:
        roots = default_roots()

    return build_config(
        terms          = args.terms,
        extensions     = args.ext or [],
        case_sensitive = args.case,
        roots          = roots,
    )


def textwrap_dedent(s: str) -> str:
    """Minimal dedent — avoids importing textwrap."""
    lines = s.splitlines()
    indent = min((len(l) - len(l.lstrip()) for l in lines if l.strip()), default=0)
    return '\n'.join(l[indent:] for l in lines)


# ── search runner ─────────────────────────────────────────────────────────────

def run_search(cfg: SearchConfig) -> list[tuple[Path, str]]:
    groups = group_by_device(cfg.roots)
    width  = 62

    print(f'\n{"="*width}')
    print(f'  fsearch  —  {datetime.now().strftime("%H:%M:%S")}')
    print(f'  Disks    : {len(groups)}', end='')
    for dev, dev_roots in groups.items():
        label = ", ".join(str(r) for r in dev_roots)
        print(f'\n    {dev:#010x}  →  {label}', end='')
    print(f'\n  Workers  : {WORKERS_PER_DISK}/disk  '
          f'({len(groups) * WORKERS_PER_DISK} total)')
    print(f'{"="*width}\n')

    out_q   = queue.Queue()
    stats   = {'checked': 0, 'submitted': 0}
    lock    = threading.Lock()
    hits: list[tuple[Path, str]] = []

    scanners = [
        DiskScanner(dev_roots, dev_id, cfg, out_q, stats, lock)
        for dev_id, dev_roots in groups.items()
    ]
    threads = [
        threading.Thread(target=sc.run, daemon=True, name=f'walker-{i}')
        for i, sc in enumerate(scanners)
    ]
    for t in threads:
        t.start()

    last_line = ''
    try:
        while any(t.is_alive() for t in threads):
            while True:
                try:
                    hit = out_q.get_nowait()
                    hits.append(hit)
                    print(f'\r{" " * len(last_line)}\r'
                          f'  ✓  [{hit[1]:14s}]  {hit[0]}')
                except queue.Empty:
                    break
            with lock:
                c, s = stats['checked'], stats['submitted']
            last_line = (f'  …  {c:>9,} scanned'
                         f'  /  {s:>9,} queued'
                         f'  /  {len(hits)} hit{"s" if len(hits) != 1 else ""}')
            print(last_line, end='\r', flush=True)
            time.sleep(0.3)
    except KeyboardInterrupt:
        _stop.set()

    for t in threads:
        t.join(timeout=5)

    # final drain
    while True:
        try:
            hit = out_q.get_nowait()
            hits.append(hit)
            print(f'  ✓  [{hit[1]:14s}]  {hit[0]}')
        except queue.Empty:
            break

    with lock:
        c, s = stats['checked'], stats['submitted']

    status = 'Stopped' if _stop.is_set() else 'Done'
    print(f'\n\n{"="*width}')
    print(f'  {status:<10}  {datetime.now().strftime("%H:%M:%S")}')
    print(f'  Queued  :  {s:>10,}')
    print(f'  Scanned :  {c:>10,}')
    print(f'  Matches :  {len(hits):>10,}')
    print(f'{"="*width}')

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
        if not cfg.extensions:
            print('  Tip: content search only covers readable file types.')
            print('       Use -e to add more extensions.')

    return hits


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    cfg = parse_cli() or prompt_config()
    run_search(cfg)


if __name__ == '__main__':
    main()
