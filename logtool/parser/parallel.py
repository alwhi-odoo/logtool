"""Parsing in parallel: the worker pool, chunks (byte ranges of a plain file) and `profile_files()`.

PAR_* and `_cpus` are read here at call time: a test that forces several chunks sets them on this module.
"""

from __future__ import annotations

import multiprocessing
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import NamedTuple, Optional

from .bucket import _pack
from .jobs import book_cron_kills, cron_marks, pair_cron_kills
from .logio import READ_ERRORS, _entry_start, _lines, opener
from .profile import Profile
from .scan import scan_issue_occurrences, scan_op_occurrences


PAR_MIN_TOTAL = 24 * 1024 * 1024     # below this, one process is faster than sharing the work (pool not started yet)
PAR_WARM_TOTAL = 4 * 1024 * 1024     # ... and once warm_pool() has started the workers
PAR_MIN_CHUNK = 8 * 1024 * 1024
PAR_MAX_CHUNK = 64 * 1024 * 1024     # chunks are kept, each with its own copy of what it saw (facts, examples, ...):
                                     # with many big files, fewer bigger chunks hold less memory; but a time interval
                                     # re-reads the part of its two edge chunks inside it. 64 MB weighs the two
                                     # against each other


def _scan_task(task):
    kind, path, piece, kw = task
    return (scan_op_occurrences if kind == "op" else scan_issue_occurrences)(path, piece=piece, **kw)


def scan_pieces(kind, tasks, kw, enough=None):
    """One scan_op_occurrences / scan_issue_occurrences (`kind` "op" / "issue", keyword arguments `kw`) per task,
    in parallel on the worker pool; [result per task], in task order. tasks: [(path, piece)] -- piece (start,
    end, first line) of a plain file, or None for a whole file in one go (a compressed one); several files'
    pieces go in one call, so they share the pool. With `enough`, stops once the tasks read so far, in order,
    hold that many results (the first N lines)."""
    tasks = [(kind, path, piece, kw) for path, piece in tasks]
    if len(tasks) > 1:
        try:
            futures = [_pool().submit(_scan_task, t) for t in tasks]
            out, n = [], 0
            for fut in futures:
                out.append(fut.result())
                n += len(out[-1])
                if enough is not None and n >= enough:
                    break
            for fut in futures[len(out):]:              # not needed any more: drop those not started yet
                fut.cancel()
            return out
        except (BrokenProcessPool, RuntimeError) as e:   # a worker died (or the pool shut down): scan serially
            sys.stderr.write("worker pool unavailable (%s); scanning serially\n" % e)
            shutdown_pool()
    out, n = [], 0
    for t in tasks:
        out.append(_scan_task(t))
        n += len(out[-1])
        if enough is not None and n >= enough:
            break
    return out


def _work(a, path, start, end):
    """Profile one file, or the byte range [start, end) of a plain one.
    Returns (profile, index, error): a file that breaks half way keeps what was read before that."""
    p = Profile(a)
    idx = []
    err = None
    try:
        if start is None:
            with opener(path) as fh:
                p.feed(fh)
        else:
            p.feed(_lines(path, start, end, idx))
    except READ_ERRORS as e:
        err = "%s: %s" % (type(e).__name__, e)
    _pack(p)
    return p, idx, err


_POOL = None
_POOL_WARM = False          # every worker process is running
_POOL_LOCK = threading.Lock()


def _cpus():
    """CPUs this process may use (respects taskset and container limits)."""
    try:
        return len(os.sched_getaffinity(0)) or 1
    except AttributeError:
        return os.cpu_count() or 1


def _mp_context():
    """forkserver: workers are forked from a small clean helper, so they start in milliseconds and never
    inherit the server's threads or sockets (plain fork does, and can deadlock)."""
    methods = multiprocessing.get_all_start_methods()
    ctx = multiprocessing.get_context("forkserver" if "forkserver" in methods else "spawn")
    if ctx.get_start_method() == "forkserver":
        ctx.set_forkserver_preload([__name__])
    return ctx


def _worker_init():
    """Ctrl+C in a terminal reaches every process in it. Workers ignore it (each one would print a
    traceback); the parent decides when they stop, and they exit when their pool is shut down."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _pool():
    """One shared pool of worker processes, started once (see warm_pool) and reused for every load
    and every time-interval selection."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ProcessPoolExecutor(max_workers=_cpus(), mp_context=_mp_context(), initializer=_worker_init)
        return _POOL


def shutdown_pool():
    global _POOL, _POOL_WARM
    with _POOL_LOCK:
        pool, _POOL, _POOL_WARM = _POOL, None, False
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def _checkin(args):
    """Warm-up task: hold this worker until all of them have started, so none can be reused for another."""
    folder, n = args
    open(os.path.join(folder, str(os.getpid())), "w").close()
    deadline = time.monotonic() + 60
    while len(os.listdir(folder)) < n and time.monotonic() < deadline:
        time.sleep(0.02)


def warm_pool():
    """Start every worker process now, so the first big file does not wait for them (the pool adds
    one process per task until it has as many as it needs). Once they run,
    even a few MB are worth sharing between processes (see PAR_WARM_TOTAL)."""
    global _POOL_WARM
    n = _cpus()
    folder = tempfile.mkdtemp(prefix="olp_warm_")
    try:
        list(_pool().map(_checkin, [(folder, n)] * n))
        _POOL_WARM = True
    except Exception:                                    # warming is an optimisation, never a failure
        shutdown_pool()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


class Chunk(NamedTuple):
    """One independently parsed byte range of a plain file: the per-database profiles of its lines, the first /
    last timestamp in it, and how many lines it has (so the number of any chunk's first line is known without
    reading the file again: 1 + the lines of the chunks before it). A time interval reuses every chunk that lies
    inside it; the raw-line scanners read chunk by chunk."""
    start: Optional[int]          # None: a compressed file, parsed as one piece
    end: Optional[int]
    parts: dict
    t_min: Optional[str]
    t_max: Optional[str]
    lines: int = 0
    crons: tuple = ([], [], {}, [])   # see cron_marks


def _chunk(start, end, part):
    stamps = [(c.t_min, c.t_max) for c in part.parts.values() if c.t_min]
    return Chunk(start, end, part.parts, min(s[0] for s in stamps) if stamps else None,
                 max(s[1] for s in stamps) if stamps else None, part.lines, cron_marks(part))


def profile_files(a, paths, jobs=None, spans=None, chunks=None, min_chunk=PAR_MIN_CHUNK):
    """Profile several logs into one Profile, splitting big plain files across processes.

    spans:  optional {path: (start_byte, end_byte)} to read only part of a plain file.
    chunks: optional dict that receives {path: [Chunk, ...]} (one per parsed piece, in file order); the
            returned Profile then holds only the index and the errors, not the merged counts.
    min_chunk: smallest piece worth a process of its own.
    The result's `.index` maps each plain path to its [(byte_offset, minute)] list and `.errors` maps
    the paths that could not be read completely to the reason."""
    jobs = jobs or _cpus()
    spans = spans or {}
    plan, total = [], 0
    prof = Profile(a)
    prof.index = {}
    prof.errors = {}
    for path in paths:
        if path == "-":
            plan.append((path, None, None))
            continue
        try:
            size = os.path.getsize(path)
        except OSError as e:
            prof.errors[path] = "%s: %s" % (type(e).__name__, e)
            continue
        if path.endswith((".gz", ".bz2")):
            plan.append((path, None, None))               # compressed: cannot seek, one task
            total += size
        else:
            lo, hi = spans.get(path, (0, size))
            plan.append((path, lo, hi))
            total += hi - lo

    def take(path, s, e, result):
        part, idx, err = result
        if err:
            prof.errors[path] = err
        if idx:
            prof.index.setdefault(path, []).extend(idx)
        if chunks is None:
            prof.merge(part)
        else:                                            # the caller merges the chunks it needs, when it needs them
            chunks.setdefault(path, []).append(_chunk(s, e, part))

    tasks = []
    if jobs > 1 and total >= (PAR_WARM_TOTAL if _POOL_WARM else PAR_MIN_TOTAL) and all(p != "-" for p, _, _ in plan):
        size = min(PAR_MAX_CHUNK, max(min_chunk, -(-total // jobs)))
        for path, s, e in plan:
            if s is None or e - s <= size:
                tasks.append((path, s, e))
            else:                                        # cut at log entries: a traceback stays with its header
                cuts = [s] + sorted({_entry_start(path, x, e) for x in range(s + size, e, size)} - {s, e}) + [e]
                tasks.extend((path, a, b) for a, b in zip(cuts, cuts[1:]))
        try:
            results = list(_pool().map(_work, [a] * len(tasks), *zip(*tasks)))
        except KeyboardInterrupt:
            shutdown_pool()                              # drop the queued chunks, then let the interrupt through
            raise
        except (BrokenProcessPool, RuntimeError) as e:  # a worker died (or the pool shut down): read serially
            sys.stderr.write("worker pool unavailable (%s); reading serially\n" % e)
            shutdown_pool()
            results = None
    else:
        tasks = plan
        results = None
    if results is None:
        results = [_work(a, path, s, e) for path, s, e in tasks]
    marks = []
    for (path, s, e), result in zip(tasks, results):
        take(path, s, e, result)
        marks.append((None, cron_marks(result[0])))
    if chunks is None:                                   # runs killed in another piece than they started in
        book_cron_kills([r for _, r in pair_cron_kills(marks)],
                        prof._part if getattr(a, "split_db", False) else (lambda db: prof))
    for lst in prof.index.values():
        lst.sort()
    return prof.finalize()
