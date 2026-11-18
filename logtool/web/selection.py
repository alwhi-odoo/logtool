"""Selection: files -> databases -> time interval -> one merged Profile, from the cached chunks.

Selecting files / databases only merges cached pieces; a time interval reuses the chunks inside it and re-reads
just its two edges; fact filters parse the files again (once per filter). Also the pieces of a file the
raw-line scanners read (`_pieces`).
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from . import state
from .. import profiler as olp
from .loading import _load
from .slowq import _sq_files
from .state import (
    BASE, CHUNKS, CUT_BASE, EDGE_CHUNK, FCHUNKS, FILES, INDEX, LOCK, NOFILT, NONE_DB, NoData, ORDER, PAIRS, SLICES,
    SLOWQ, _args,
)
from .util import _cache_get, _iso, _remember, _secs


def _read_edge(f, chunk, lo, hi, fact_filter=None):
    """Parse the lines of `chunk` that fall in seconds [lo, hi), seeking to them where the minute index allows."""
    _gone_check(f)
    a = _args(fact_filter)
    a.since, a.until = _iso(lo), _iso(hi - 1)
    spans = None
    if chunk.start is not None:
        start, end = chunk.start, chunk.end
        idx = INDEX.get(f["id"])
        if idx:                                     # seek to the interval (3 min margin for out-of-order lines)
            # idx is sorted by byte offset, but NOT reliably by minute: Odoo interleaves several workers'
            # lines in one file, so the minute column can dip locally (confirmed empirically -- a plain
            # bisect on it silently skipped real in-range lines). A linear scan stays correct regardless;
            # idx only grows one entry per distinct minute seen, so it is small even for a huge log.
            lo_m, hi_m = lo // 60, -(-hi // 60)
            begin = next((o for o, m in idx if m >= lo_m - 3), end)
            start = max(start, begin)
            end = min(end, next((o for o, m in idx if o > begin and m > hi_m + 2), end))
        if start >= end:
            return {}
        spans = {f["path"]: (start, end)}
    return olp.profile_files(a, [f["path"]], spans=spans, min_chunk=EDGE_CHUNK).parts


def _gone_check(f):
    """Re-reading a file that was deleted since it was loaded would quietly find nothing: say so instead."""
    if not os.path.isfile(f["path"]):
        raise NoData("%s is no longer on disk (deleted since it was loaded): unload it and load it again" % f["name"])


def _chunks(f, flt=NOFILT):
    """The parsed chunks of one file: as loaded, or parsed again with a filter (only the current filter's are
    kept: a whole re-parse each). Usually already done by _prepare_filter, outside LOCK."""
    if flt[0] is None:
        return CHUNKS[f["id"]]
    hit = _cache_get(FCHUNKS, (f["id"], flt[0]))
    return hit if hit is not None else _filter_parse([f], flt)[0]


def _filter_parse(files, flt):
    """Parse files again with a filter -- all in one profile_files() call, so their chunks share the worker
    pool -- and keep the result (dropping the last filter's first). Returns their chunks, in order. LOCK is only
    taken for the cache, never while waiting for another lock, so this cannot deadlock with LOCK holders."""
    out, todo = {}, []
    with LOCK:
        gen = state.ONLY_GEN
        for f in files:
            hit = _cache_get(FCHUNKS, (f["id"], flt[0]))
            if hit is not None:                     # another request parsed it meanwhile
                out[f["id"]] = hit
                continue
            _gone_check(f)
            for k in [k for k in FCHUNKS if k[0] == f["id"]]:      # free the last filter's before reading again
                del FCHUNKS[k]
            for k in [k for k in SLICES if k[0] == f["id"] and k[3] is not None]:
                del SLICES[k]
            todo.append((f["id"], f["path"]))
    if todo:
        chunks = {}
        olp.profile_files(_args(flt[1]), list(dict.fromkeys(p for _, p in todo)), chunks=chunks)   # the long part
        with LOCK:
            if gen != state.ONLY_GEN:                     # "only read database" changed meanwhile: this parse is stale
                raise NoData("the database to read changed while the filter was applied: try again")
            for fid, path in todo:
                out[fid] = chunks.get(path, [])
                FCHUNKS[(fid, flt[0])] = out[fid]   # one entry per file: the current filter's (freed above)
    return [out[f["id"]] for f in files]


def _slice_parts(f, lo, hi, flt=NOFILT, edges=None):
    """The per-database pieces of one file for the lines in seconds [lo, hi): chunks that lie inside the
    interval are reused as they are, only the chunks the interval cuts through are read again (or taken from
    `edges`, {chunk start: parts} read beforehand, see _cut)."""
    key = (f["id"], lo, hi, flt[0])
    hit = _cache_get(SLICES, key)
    if hit is not None:
        return hit
    pieces = []
    for c in _edge_plan(f, lo, hi, flt):
        if c[1]:
            pieces.append(c[0].parts)
        else:
            got = (edges or {}).get((f["id"], c[0].start))
            pieces.append(got if got is not None else _read_edge(f, c[0], lo, hi, flt[1]))
    return _remember(SLICES, key, pieces, 24)


def _edge_plan(f, lo, hi, flt):
    """[(chunk, inside?)] of the chunks of `f` that seconds [lo, hi) touch: inside ones are reused whole, the
    others (the interval's edges) are read again."""
    out = []
    for c in _chunks(f, flt):
        if c.t_min is None:
            continue
        c_lo, c_hi = _secs(c.t_min), _secs(c.t_max)
        if c_hi < lo or c_lo >= hi:
            continue
        out.append((c, lo <= c_lo and c_hi < hi))
    return out


def _cron_pairs():
    """[(file id, run)]: the cron runs started in one chunk or file and killed in another (olp.pair_cron_kills),
    paired over every loaded log, the run going with the file of its kill (where a run that ended would be
    counted). Cheap (a few tuples per chunk); kept until the loaded logs or the database to read change."""
    with LOCK:
        logs = [f for f in (FILES[i] for i in ORDER) if f.get("kind") == "log" and f["id"] in CHUNKS]
        key = (state.ONLY_GEN, tuple(f["id"] for f in logs))
        if PAIRS[0] != key:
            seen, pieces = set(), []
            for f in logs:
                if f["path"] not in seen:            # the same file added twice is one file
                    seen.add(f["path"])
                    pieces += [(f["id"], c.crons) for c in CHUNKS[f["id"]]]
            PAIRS[:] = [key, olp.pair_cron_kills(pieces)]
        return PAIRS[1]


def _carried():
    """_cron_pairs() the way the raw-line scanners take them (see olp.scan_op_occurrences)."""
    return {(r[0], r[5]): (r[2], r[3], r[4], r[1]) for _, r in _cron_pairs()} or None


def _merge(pieces, exclude_db, runs=(), flt=NOFILT):
    """One profile of the given per-database pieces, and of the killed cron `runs` no piece could pair (see
    _cron_pairs; counted with filter `flt`, like the pieces were). It is cached and only read from then on, so
    what it holds per minute is packed (see olp.PackedHist) once merged."""
    merged = olp.Profile(_args())
    kids = []
    for parts in pieces:
        for db, child in parts.items():
            if db not in exclude_db:
                merged.merge(child, heavy=False)        # the rest on first need (see _heavy)
                kids.append(child)
    runs = [r for r in runs if r[1] not in exclude_db]
    if runs:
        child = olp.Profile(_args(flt[1]))
        olp.book_cron_kills(runs, lambda db: child)
        olp._pack(child)
        merged.merge(child, heavy=False)
        kids.append(child)
    merged.finalize()
    olp._pack(merged)
    merged._kids = kids
    return merged


def _heavy(prof):
    """A merged profile with what only an item's details and the export read merged in too (op_facts, op_logged,
    src, clients): done once, on first need, so analyses and time intervals never pay for it."""
    with LOCK:
        kids = getattr(prof, "_kids", None)
        if kids:
            for child in kids:
                prof.merge_heavy(child)
            prof.clients = {c: olp.PackedHist(h, 2) for c, h in prof.clients.items()}
            prof._kids = None
    return prof


def _select(exclude_db, exclude_file, init):
    """Files -> databases. Returns (all files, the selected log files, [(database, log lines)], excluded
    databases). Imported summaries (see export() / _try_parse_summary()) do not participate: analyze()
    shows one on its own, before this is ever called."""
    files = [FILES[i] for i in ORDER]
    for f in files:
        _load(f)
    logs = [f for f in files if f.get("kind") == "log"]
    slow = [f for f in files if f.get("kind") == "slowq"]
    if not logs and not slow:
        raise NoData("no files loaded" if not files else "no log files loaded (only imported summaries)")
    inc = [f for f in logs if f["id"] not in exclude_file and f["usable"]]
    if not inc and not _sq_files(exclude_file):
        raise NoData("no files selected" if any(f["usable"] for f in logs + slow)
                     else "none of the loaded files contains Odoo log lines")

    # databases that exist in the selected files (a file's private dbs vanish with it)
    counts = {}
    for f in inc:
        for c in CHUNKS[f["id"]]:
            for db, child in c.parts.items():
                counts[db] = counts.get(db, 0) + child.header_ok
    for f in _sq_files(exclude_file):                    # a slow query counts as one line of its database
        for q in SLOWQ[f["id"]]["entries"]:
            counts[q.db or NONE_DB] = counts.get(q.db or NONE_DB, 0) + 1
    dbs = sorted(((d, n) for d, n in counts.items() if d != NONE_DB), key=lambda kv: -kv[1])
    if NONE_DB in counts:
        dbs.append((NONE_DB, counts[NONE_DB]))
    if init and dbs:                        # first load: most active database + lines without a db
        top = dbs[0][0] if dbs[0][0] != NONE_DB else None
        exclude_db = {d for d, _ in dbs if d != top and d != NONE_DB}
    elif dbs and all(d in exclude_db for d, _ in dbs):
        exclude_db = set()                  # everything excluded (e.g. its file was unloaded): show everything
    return files, inc, dbs, exclude_db


def _base(ids, excl, flt=NOFILT):
    """The merged profile of the selected files and databases, merged once (excluding operations derives from it)."""
    key = (ids, excl, flt[0])
    prof = _cache_get(BASE, key)
    if prof is None:
        runs = [r for fid, r in _cron_pairs() if fid in ids]
        prof = _remember(BASE, key, _merge([c.parts for i in ids for c in _chunks(FILES[i], flt)], excl, runs, flt), 4)
    return prof


def _cut(inc, ids, excl, lo, hi, flt=NOFILT):
    """The merged profile of the selected files and databases, limited to seconds [lo, hi). Kept in its
    own cache (CUT_BASE) so scrubbing through many time intervals can't push out _base()'s whole-selection
    profile, which detail() and export() also need."""
    key = (ids, excl, lo, hi, flt[0])
    prof = _cache_get(CUT_BASE, key)
    if prof is None:
        # the edges (the interval's first and last chunk, often in two different files) are read at the same time
        todo = [(f, c) for f in inc if _cache_get(SLICES, (f["id"], lo, hi, flt[0])) is None
                for c, inside in _edge_plan(f, lo, hi, flt) if not inside]
        edges = {}
        if len(todo) > 1:
            with ThreadPoolExecutor(max_workers=len(todo)) as pool:
                for (f, c), parts in zip(todo, pool.map(lambda fc: _read_edge(fc[0], fc[1], lo, hi, flt[1]), todo)):
                    edges[(f["id"], c.start)] = parts
        # killed runs the pieces miss: those that started in another chunk, and in an edge chunk, those that
        # started before the interval (the edge is read from `lo` on)
        runs = [r for fid, r in _cron_pairs() if fid in ids and lo <= int(r[5]) < hi]
        runs += [r for f in inc for c, inside in _edge_plan(f, lo, hi, flt) if not inside
                 for r in c.crons[3] if int(r[4]) < lo <= int(r[5]) < hi]
        prof = _remember(CUT_BASE, key, _merge([p for f in inc for p in _slice_parts(f, lo, hi, flt, edges)], excl,
                                               runs, flt), 6)
    return prof


def _prof_for(inc, ids, excl, interval, flt=NOFILT):
    """The merged profile of the selection, cut to `interval` when that is narrower than the logs."""
    prof = _base(ids, excl, flt)
    if interval and prof.t_min:
        lo, hi = max(interval[0], _secs(prof.t_min)), min(interval[1], _secs(prof.t_max) + 1)
        if lo >= hi:
            raise NoData("the interval lies outside the loaded logs")
        if (lo, hi) != (_secs(prof.t_min), _secs(prof.t_max) + 1):
            prof = _cut(inc, ids, excl, lo, hi, flt)
    return prof


def _pieces(fid, path, lo=None, hi=None):
    """The parts of one file worth scanning for raw lines: the parser's own chunks (byte ranges, so a scan
    claims lines the way the parser did and runs on every core), only those that overlap seconds [lo, hi);
    [None] for a file read in one piece (compressed)."""
    with LOCK:
        chunks = CHUNKS.get(fid) or []
    if not chunks or chunks[0].start is None:
        return [None]
    out, first = [], 1                              # a chunk's first line: 1 + the lines of the chunks before it
    for c in chunks:
        if lo is None or (c.t_min is not None and _secs(c.t_max) >= lo and _secs(c.t_min) < hi):
            out.append((c.start, c.end, first))
        first += c.lines
    return out


def _check_on_disk(files):
    """Raw lines are re-read from the files: say so when they are gone, instead of finding nothing."""
    gone = [name for _fid, name, path in files if not os.path.isfile(path)]
    if gone:
        raise NoData("%s no longer on disk (deleted since it was loaded): unload and load %s again to see raw lines"
                     % (", ".join(gone) + (" is" if len(gone) == 1 else " are"), "it" if len(gone) == 1 else "them"))
