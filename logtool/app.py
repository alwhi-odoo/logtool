#!/usr/bin/env python3
"""
logtool.app - localhost web UI for the Odoo log profiler.

    logtool                             # start on http://127.0.0.1:5000
    logtool odoo.log other.log.gz       # preload one or more logs
    logtool --port 8080 --no-browser

Logs are loaded through the browser (drag & drop or file picker) and can be
unloaded again. The analysis is filtered in three steps:

    file  ->  database  ->  time interval

Each file is parsed once, in chunks (in parallel for big files). Every chunk is kept as one
profile per database and remembers where each minute of the file starts (byte offsets).
Selecting files / databases only merges those cached pieces. A time interval merges the
chunks that lie inside it and re-reads just the two edges of the interval.
"""

from __future__ import annotations

import argparse
import bz2
import datetime
import errno
import gzip
import hashlib
import json
import logging
import multiprocessing.util
import os
import pickle
import re
import shutil
import signal
import sys
import tempfile
import threading
import uuid
import webbrowser
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from flask import Flask, Response, abort, jsonify, render_template, request
from werkzeug.serving import make_server

from . import __version__
from . import profiler as olp

app = Flask(__name__)

UPLOAD_DIR = None       # created on first upload
MAX_ROWS = 5000         # operations sent to the UI per view (the table must not hide the long tail)
MAX_HIST_OPS = 500      # operations that also get their own timeline (for the histogram's search tags);
                        # a search tag on a busier operation than this just won't have a line -- the top
                        # 500 by time already covers what anyone would realistically want to compare
MAX_ISSUES = 60         # distinct warning / error messages sent to the UI
MAX_EXPORT_ISSUES = 5000   # ... and written to an export (all of them, on any realistic log)
FACT_TOP = 12           # values of one detail (client IP, user id, ...) shown per popup / exported per row
MAX_ISSUE_LINES = 200   # occurrences of one issue the details popup lists to pick from
MAX_CONTEXT = 200        # raw lines the details popup may ask for on either side of one occurrence
NONE_DB = "-"           # key of log lines that carry no database name
EDGE_CHUNK = 1 << 20    # smallest slice of a time interval's edge worth its own process
LOOPBACK = ("127.0.0.1", "localhost", "::1")

FILES = {}              # id -> {"id", "name", "path", "size", "owned", "problem", "usable", "kind"}
ORDER = []              # load order
CHUNKS = {}             # file id -> [olp.Chunk]: the file parsed once, per byte range: {database: child Profile} each
INDEX = {}              # file id -> [(byte offset, minute)] or None (compressed / no timestamps)
# Below: LRU caches (see _cache_get / _remember) capped well past a normal drag-across-the-histogram
# session, so scrubbing through many time intervals in a row can't evict a still-relevant entry.
SLICES = OrderedDict()  # (file id, from, to, filter key) -> [{database: child Profile}]: what an interval reads from one file
FCHUNKS = OrderedDict() # (file id, filter key) -> [olp.Chunk]: the file parsed again with fact filters (see _ff_arg)
TIMELINES = OrderedDict()  # (selection, filter key, from, to, issues) -> a focused histogram (see timeline())
FULL = OrderedDict()    # (included file ids, excluded dbs, excluded ops) -> whole-selection result
BASE = OrderedDict()    # (included file ids, excluded dbs) -> merged Profile: excluding operations starts here
CUT_BASE = OrderedDict()   # (included file ids, excluded dbs, from, to) -> merged Profile for one time interval
CUTS = OrderedDict()    # (included file ids, excluded dbs, excluded ops, from, to) -> interval result
SUMMARIES = {}          # file id -> a previously exported summary (see export()), or None if the .json wasn't one
LOCK = threading.RLock()
LOAD_LOCK = threading.RLock()   # parsing a new file: held without LOCK, so other requests keep being answered meanwhile
DIR_LOCK = threading.Lock()
FILTER_LOCK = threading.Lock()   # _prepare_filter: held without LOCK (LOCK is only ever taken inside it, never around it)
ONLY_DB = None          # read only this database's log lines (and those without one); None: every database (see set_only_db)
ONLY_GEN = 0            # bumped by set_only_db: a parse that started under an older setting must not be cached
NOFILT = (None, None)   # no fact filter: (cache key, args.fact_filter)
BOOT_ID = uuid.uuid4().hex[:8]   # this run of the server: settings that should not outlive it are stored under it


class NoData(Exception):
    """The request cannot be answered from what is loaded; the message is meant for the user."""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _args(fact_filter=None):
    """Same defaults the CLI would give, minus every output option."""
    return argparse.Namespace(top=MAX_ROWS, by="detail", min_calls=1, min_total=0.0, fold=1,
                              events=False, event_top=6, logins=False, hours=False,
                              unclassified=False, all=False, since=None, until=None,
                              db=None, grep=None, csv=None, json=None, out=None, split_db=True,
                              fact_filter=fact_filter, only_db=ONLY_DB)


def _iso(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _secs(stamp):
    """'2026-09-14T13:05:09' -> seconds since the unix epoch (the log's own clock)."""
    return olp.stamp_minute(stamp[:10], stamp[11:16]) * 60 + int(stamp[17:19])


def _duration(t0, t1):
    try:
        return (datetime.datetime.fromisoformat(t1) - datetime.datetime.fromisoformat(t0)).total_seconds()
    except (TypeError, ValueError):
        return None


def _cache_get(cache, key):
    """LRU read from an OrderedDict cache: a hit counts as this entry's most recent use, so a run of
    later insertions (e.g. dragging across the histogram) doesn't evict something still in play."""
    if key not in cache:
        return None
    cache.move_to_end(key)
    return cache[key]


def _remember(cache, key, value, limit=24):
    """LRU write: caches `value`, evicting the least recently used entry once `cache` is over `limit`."""
    if key not in cache and len(cache) >= limit:
        cache.popitem(last=False)
    cache[key] = value
    cache.move_to_end(key)
    return value


def _forget_selections():
    """Drop every cached merge (they can hold a lot of memory and belong to files that are going away)."""
    for cache in (FULL, BASE, CUT_BASE, CUTS, FCHUNKS, TIMELINES):
        cache.clear()


def _remove_file(rec):
    if rec["owned"]:
        try:
            os.remove(rec["path"])
        except OSError:
            pass


def _disk_msg(e):
    """A message worth showing a user for a failed write, with the out-of-space case called out (it is
    the one they can actually act on: unload some files, or free up space where UPLOAD_DIR lives)."""
    if isinstance(e, OSError) and e.errno in (errno.ENOSPC, errno.EDQUOT):
        return ("out of disk space (or over its quota) where logtool keeps the files it loads%s. "
                 "Unload some files, or free up space there, then try again."
                 % (" (%s)" % UPLOAD_DIR if UPLOAD_DIR else ""))
    return "%s: %s" % (type(e).__name__, e)


def _free_bytes(path):
    """Free space on the filesystem holding `path` (a directory, or one that will be created), or None
    if that cannot be determined -- callers then just proceed and let the write itself fail or not."""
    d = path if os.path.isdir(path) else os.path.dirname(path) or "."
    try:
        return shutil.disk_usage(d).free
    except OSError:
        return None


OWNER_FILE = ".owner"     # in each UPLOAD_DIR: the pid of the logtool process that uses it


def _proc_start(pid):
    """When process `pid` started (clock ticks since boot, from /proc), or "" where that cannot be read:
    tells a live owner from a new process that was handed the same pid (a container restart, ...)."""
    try:
        with open("/proc/%d/stat" % pid) as fh:
            return fh.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError, ValueError):
        return ""


def _upload_dir():
    """This process's own folder for uploaded and decompressed logs (created once, whichever thread asks
    first), marked with its pid and start time so another logtool starting meanwhile (see _pick_tmp_dir)
    knows it is in use."""
    global UPLOAD_DIR
    with DIR_LOCK:
        if UPLOAD_DIR is None:
            d = tempfile.mkdtemp(prefix="logtool_")
            try:
                with open(os.path.join(d, OWNER_FILE), "w") as fh:
                    fh.write("%d %s" % (os.getpid(), _proc_start(os.getpid())))
            except OSError:
                pass
            UPLOAD_DIR = d
        return UPLOAD_DIR


def _abandoned(folder):
    """Is this 'logtool_*' folder left over from a process that is gone? A folder without an owner file
    (written by an older version) counts as abandoned only once it has not been touched for two days."""
    try:
        with open(os.path.join(folder, OWNER_FILE)) as fh:
            owner = fh.read().split()
        pid, started = int(owner[0]), owner[1] if len(owner) > 1 else ""
    except (OSError, ValueError, IndexError):
        try:
            return os.path.getmtime(folder) < datetime.datetime.now().timestamp() - 2 * 86400
        except OSError:
            return False
    if pid <= 0 or pid == os.getpid():           # this process has no folder yet: one with its pid is an old run's
        return True
    try:
        os.kill(pid, 0)                          # signal 0: only asks whether the process exists
    except ProcessLookupError:
        return True
    except OSError:                              # exists, owned by someone else
        pass
    return bool(started) and started != _proc_start(pid)     # a new process that got the same pid


def _pick_tmp_dir():
    """Where every file logtool loads ends up, one way or another: uploaded logs, their decompressed
    copies (see _decompress_once), and -- because this also becomes tempfile.tempdir, see main() --
    even Werkzeug's own internal buffering of an incoming upload, before this module's code ever runs.
    The OS default temp directory (usually /tmp) is often its own small, separately quota'd filesystem
    (a tmpfs capped well below the machine's real disk), so a single log or two can exhaust it even
    though there is plenty of room elsewhere; a directory under the user's home is far more likely to
    have real space. Falls back to the OS default (None) if that is not writable either.

    Best-effort sweeps this directory of the 'logtool_*' folders left behind by a previous run that
    did not shut down cleanly (a crash, SIGKILL, ...). Only abandoned ones (see _abandoned): another
    logtool still running keeps its files -- it re-reads them for raw log lines and time intervals."""
    base = os.path.join(os.path.expanduser("~"), ".cache", "logtool")
    try:
        os.makedirs(base, exist_ok=True)
        probe = tempfile.mkdtemp(prefix=".write_check_", dir=base)
        os.rmdir(probe)
    except OSError:
        return None
    try:
        listening = _unix_socket_paths()
        for name in os.listdir(base):
            path = os.path.join(base, name)
            if name.startswith("logtool_") and _abandoned(path):
                shutil.rmtree(path, ignore_errors=True)
            elif name.startswith("pymp-") and listening is not None and _stale_pymp(path, listening):
                shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass
    return base


def _unix_socket_paths():
    """The paths of the Unix sockets some process is listening on (from /proc/net/unix), or None."""
    try:
        with open("/proc/net/unix") as fh:
            return {line.split(None, 7)[7].strip() for line in fh if len(line.split(None, 7)) == 8}
    except OSError:
        return None


def _stale_pymp(folder, listening):
    """Is this 'pymp-*' folder (the worker pool's sockets, which versions before 1.2 left in ~/.cache/logtool
    when killed) unused? It is once no process listens on any socket in it any more."""
    try:
        return not any(os.path.join(folder, n) in listening for n in os.listdir(folder))
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #

NICE_MIN = (1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 10080)   # bucket sizes, minutes
MAX_BINS = 1440         # finest time resolution sent to the UI: at most this many buckets (the histogram
                        # scrolls horizontally past its panel width, so a dense chart stays readable)


def _packed_rows(hist):
    """(minute, n, sql, py, unsplit) of a per-minute hist, read straight from a packed one (no slice per minute)."""
    if isinstance(hist, olp.PackedHist) and hist.w == 4:
        vs = hist.vals
        return ((m, vs[4 * i], vs[4 * i + 1], vs[4 * i + 2], vs[4 * i + 3]) for i, m in enumerate(hist.keys))
    return ((m, v[0], v[1], v[2], v[3]) for m, v in hist.items())


def _add_hist(into, hist):
    """Add {minute: [n, sql, py, unsplit]} into `into` (which owns its lists)."""
    for m, n, sql, py, u in _packed_rows(hist):
        e = into.get(m)
        if e is None:
            into[m] = [n, sql, py, u]
        else:
            e[0] += n
            e[1] += sql
            e[2] += py
            e[3] += u


def _rebin(hist, base):
    """{minute: [n, sql, py, unsplit]} -> sorted [[bucket_start_minute, n, sql_s, py_s, unsplit_s], ...]"""
    out = {}
    for m, n, sql, py, u in _packed_rows(hist):
        k = m // base * base
        e = out.get(k)
        if e is None:
            out[k] = [n, sql, py, u]
        else:
            e[0] += n
            e[1] += sql
            e[2] += py
            e[3] += u
    return [[k, int(e[0]), round(e[1], 3), round(e[2], 3), round(e[3], 3)] for k, e in sorted(out.items())]


def _totals(prof):
    """Whole-selection numbers. 'calls'/'total_s' include background jobs (cron, automation,
    module loading) so they match the table; req_* are the HTTP requests alone."""
    fams = list(prof.fam.items())
    req = [b for k, b in fams if k not in olp.JOB_KINDS]
    jobs = [b for k, b in fams if k in olp.JOB_KINDS]
    allb = req + jobs
    return {
        "calls": sum(b.n for b in allb),
        "total_s": sum(b.total for b in allb),
        "sql_s": sum(b.sql for b in allb),
        "python_s": sum(b.py for b in allb),
        "unsplit_s": sum(b.unsplit for b in allb),
        "queries": sum(b.nq for b in allb),
        "errors": sum(b.errors for b in allb),
        "max_s": max((b.mx for b in allb), default=0.0),
        "req_calls": sum(b.n for b in req),
        "req_total_s": sum(b.total for b in req),
        "jobs_calls": sum(b.n for b in jobs),
        "jobs_total_s": sum(b.total for b in jobs),
    }


FACT_FIRST = (olp.F_USER, olp.F_REC)                         # then the numbers behind '#', then these:
FACT_LAST = (olp.F_IP, olp.F_SESSION, olp.F_LOGIN, olp.F_URL)


def _ip_logins(prof):
    """{client IP: [accounts that logged in successfully from it]}: the closest the log gets to a user per request."""
    got = getattr(prof, "_ipl", None)
    if got is None:
        got = {}
        for (login, ip, res), _n in prof.logins.items():
            if res == "successful" and login not in got.setdefault(ip, []):
                got[ip].append(login)
        prof._ipl = got
    return got


def _facts_json(facts, ip_logins=None, top=FACT_TOP, times=None):
    """What grouping collapsed for one row (see olp._fact), most frequent values first:
    [{"detail", "values": [[value, n] or [value, n, [logins from that IP]]], "distinct", "other_n", "total"}].
    times: the seconds twin of facts (see olp._fact_time); adds "avg_ms": the average time of the requests /
    jobs behind each listed value, in the order of "values" (None where unknown)."""
    def rank(slot):
        return (0, FACT_FIRST.index(slot)) if slot in FACT_FIRST else (2, FACT_LAST.index(slot)) if slot in FACT_LAST else (1, 0)
    out = []
    for slot in sorted(facts or {}, key=rank):
        vals = facts[slot]
        other = vals.get(olp.FACT_OTHER, 0)
        items = sorted(((v, n) for v, n in vals.items() if v != olp.FACT_OTHER), key=lambda kv: -kv[1])
        shown = []
        for v, n in items[:top]:
            logins = ip_logins.get(v) if ip_logins and slot == olp.F_IP else None
            shown.append([v, n, logins] if logins else [v, n])
        out.append({"detail": slot, "values": shown, "distinct": len(items), "other_n": other,
                    "total": sum(vals.values())})
        if times is not None:
            t = times.get(slot) or {}
            out[-1]["avg_ms"] = [round(1000 * t[v] / n, 1) if v in t and n else None for v, n, *_ in shown]
    return out


def _span(*bs):
    """First and last minute one or more operations ran, as timestamps, and in how many distinct minutes."""
    mins = set().union(*(b.hist for b in bs))
    if not mins:
        return None, None, 0
    return _iso(min(mins) * 60)[:16], _iso(max(mins) * 60)[:16], len(mins)


def _issue_json(prof, key, e, base):
    """One warning / error message plus the operations that were running when it was logged, and (like an
    operation) its own per-bucket timeline -- so it can get an overlay line on the histogram the same way
    a search tag does (see hist in _timeline_base for the shape and what `base` is)."""
    hit = prof.issue_ops.get(key) or {}
    ops = sorted(((op, n) for op, n in hit.items() if op in prof.det), key=lambda kv: -kv[1])
    return {"level": key[0], "logger": key[1], "message": key[2], "n": e[0], "first": e[1], "last": e[2],
            "sample": e[3], "exc": e[4], "src": e[5], "hist": _rebin(e[6], base),
            "facts": _facts_json(prof.issue_facts.get(key), _ip_logins(prof)),
            "ops": ops[:40], "ops_n": len(ops), "unattributed": max(0, e[0] - sum(n for _, n in ops))}


def _op_rows(prof):
    """Every operation's row (top MAX_ROWS by time), computed once per merged profile. They do not change
    when operations are excluded (only their share of the total does, which the client works out)."""
    rows = getattr(prof, "_rows", None)
    if rows is None:
        rows = prof.as_rows(("operation",), limit=MAX_ROWS)
        for r in rows:
            del r["scope"]
        prof._rows = rows
    return rows


def _summary(prof, files, max_issues=MAX_ISSUES):
    """Stats + the type and app tables of one profile."""
    t = _totals(prof)
    all_rows = prof.as_rows(("family", "app"))
    groups = {"family": [r for r in all_rows if r["scope"] == "family"],
              "app": [r for r in all_rows if r["scope"] == "app"]}
    for r in all_rows:
        del r["scope"]
    issues = sorted(prof.issues.items(), key=lambda kv: -kv[1][0])[:max_issues]
    duration = _duration(prof.t_min, prof.t_max)
    base = _timeline_base(prof)["base"]                # same bucket resolution the histogram itself uses
    return {
        "files": [f["name"] for f in files],
        "totals": t,
        "groups": groups,
        "levels": {"errors": prof.levels["ERROR"] + prof.levels["CRITICAL"], "warnings": prof.levels["WARNING"]},
        "issues": [_issue_json(prof, k, e, base) for k, e in issues],
        "issues_distinct": len(prof.issues),
        "stats": {
            "window": [prof.t_min, prof.t_max],
            "duration_s": duration,
            "lines": prof.lines,
            "header_lines": prof.header_ok,
            "requests": t["req_calls"],
            "calls_all": t["calls"],
            "untimed": prof.access_untimed,
            "held_n": prof.held_n,
            "held_s": prof.held_s,
            "formats": prof.formats.most_common(),
            "unparsable": prof.access_bad,
            "total_s": t["total_s"],
            "request_s": t["req_total_s"],
            "jobs": t["jobs_calls"],
            "jobs_s": t["jobs_total_s"],
            "sql_s": t["sql_s"],
            "python_s": t["python_s"],
            "queries": t["queries"],
            "errors_5xx": t["errors"],
            "operations": len(prof.det),
            "types": len(groups["family"]),
            "apps": len(groups["app"]),
            "status": prof.status.most_common(8),
            "req_per_s": t["req_calls"] / duration if duration else None,
        },
    }


def _timeline_base(prof):
    """When the requests happened, for the whole selection: the per-minute totals plus each big
    operation's own timeline, rebinned to at most MAX_BINS buckets. Computed once per merged profile."""
    tl = getattr(prof, "_tl", None)
    if tl is None:
        total = {}
        for b in prof.det.values():
            _add_hist(total, b.hist)
        span = (max(total) - min(total) + 1) if total else 1
        base = next((n for n in NICE_MIN if span / n <= MAX_BINS), NICE_MIN[-1])
        top = sorted(prof.det.items(), key=lambda kv: -kv[1].total)[:MAX_HIST_OPS]
        tl = prof._tl = {
            "total": total, "base": base,
            "ops": [{"family": prof.det_fam.get(k, ""), "app": prof.det_app.get(k, ""), "operation": k,
                     "hist": _rebin(b.hist, base)} for k, b in top],
        }
    return tl


def _timeline_for(prof):
    """The whole selection's timeline: its totals and each big operation's own, at its bucket size."""
    tl = _timeline_base(prof)
    return {"base": tl["base"], "all": _rebin(tl["total"], tl["base"]), "ops": tl["ops"]}


# --------------------------------------------------------------------------- #
# parsing + selection
# --------------------------------------------------------------------------- #

SUMMARY_KEYS = ("totals", "stats", "groups", "rows", "levels", "issues", "issues_distinct")


def _try_parse_summary(path):
    """A file loaded from a previous 'Export summary' (see export()), or None if it isn't one. Every
    key export() has ever written is required, so a file this check accepts is one _summary_view()
    can always read."""
    try:
        with open(path, "rt", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not all(k in data for k in SUMMARY_KEYS):
        return None
    n = data.get("files_n", 0)
    if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= 100000:
        return None
    return data


def _gz_uncompressed_size(path):
    """The exact uncompressed size a .gz file's own trailer claims (mod 2**32 -- unreliable past 4 GiB,
    which no realistic single log file reaches), or None if it cannot be read."""
    try:
        with open(path, "rb") as f:
            f.seek(-4, os.SEEK_END)
            return int.from_bytes(f.read(4), "little")
    except OSError:
        return None


DECOMPRESS_MARGIN = 256 * 1024 * 1024   # keep at least this much free after decompressing a log


def _decompressed_size(path):
    """About how big a .gz / .bz2 log gets decompressed (see _decompress_once), or None."""
    try:
        return _gz_uncompressed_size(path) if path.lower().endswith(".gz") else os.path.getsize(path) * 12
    except OSError:
        return None


def _decompress_once(path, reserved=0):
    """Best-effort: decompress a .gz / .bz2 log to a new plain temp file, or None if that is skipped
    or fails (the caller then falls back to parsing the original compressed file, as before, which
    still works -- just without the seeking _read_edge / _slice_parts otherwise get). profile_files()
    can only chunk and byte-seek into a plain file (see the Chunk docstring in profiler.py); a
    compressed file is always read as a single unseekable piece, so without this, every time-interval
    change would re-decompress and re-parse the whole file from byte 0 instead of just its two edges.

    Decompressed logs can be many times the size of the file loaded (gzip commonly manages 5-10x on
    text), and with many files loaded this can exhaust the disk where UPLOAD_DIR lives; a rough size
    estimate skips the decompression (rather than writing most of the way through it and then failing)
    once it would leave too little room. gzip's own trailer gives an exact figure; bz2 has no such
    hint, so it gets a deliberately conservative multiple of the compressed size instead. `reserved`: bytes
    other decompressions running at the same time will still write (see _load_batch)."""
    _upload_dir()
    is_gz = path.lower().endswith(".gz")
    estimate = _decompressed_size(path)
    free = _free_bytes(UPLOAD_DIR)
    if estimate is not None and free is not None and estimate > free - reserved - DECOMPRESS_MARGIN:
        return None
    out = os.path.join(UPLOAD_DIR, uuid.uuid4().hex[:10] + ".log")
    opener = gzip.open if is_gz else bz2.open
    try:
        with opener(path, "rb") as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1 << 20)
        return out
    except olp.READ_ERRORS:
        try:
            os.remove(out)
        except OSError:
            pass
        return None


def _load(f):
    """Parse one file once (see _load_batch)."""
    if f["id"] not in CHUNKS and f["id"] not in SUMMARIES:
        _load_batch([f])


def _load_pending():
    """Parse the files added since the last request -- all of them in one go, before taking LOCK, so the file
    list, popups and other requests keep being answered while big logs are read."""
    with LOCK:
        todo = [FILES[i] for i in ORDER if i not in CHUNKS and i not in SUMMARIES]
    if todo:
        _load_batch(todo)
    with LOCK:                                      # unloaded while it was being read: drop what was parsed
        for fid in [i for i in CHUNKS if i not in FILES]:
            for cache in (CHUNKS, INDEX):
                cache.pop(fid, None)


def _load_batch(files):
    """Parse files once: Odoo logs (their per-database profiles and minute index) or previously exported
    summaries (see export(), recognised by their .json extension). Many files at once go the fast way: every
    compressed one is decompressed at the same time (zlib / bz2 release the GIL, so threads use every core),
    then all of them are parsed in one profile_files() call, so their chunks share the worker pool instead of
    one file after the other."""
    with LOAD_LOCK:                                 # one load at a time; another thread may have done these meanwhile
        files = [f for f in files if f["id"] not in CHUNKS and f["id"] not in SUMMARIES]
        logs = []
        for f in files:
            if f["name"].lower().endswith(".json"):
                summary = _try_parse_summary(f["path"])
                f["kind"], f["usable"] = "summary", summary is not None
                f["problem"] = None if summary is not None else "not a logtool export"
                SUMMARIES[f["id"]] = summary
            else:
                f["kind"] = "log"
                logs.append(f)
        packed = [f for f in logs if f["path"].lower().endswith((".gz", ".bz2"))]
        if packed:
            reserve, jobs = [], []                  # the disk room each one needs, counting the ones before it
            for f in packed:
                reserve.append(sum(r or 0 for r in jobs))
                jobs.append(_decompressed_size(f["path"]))
            with ThreadPoolExecutor(max_workers=min(len(packed), olp._cpus())) as pool:
                plains = list(pool.map(lambda fr: _decompress_once(fr[0]["path"], fr[1]), zip(packed, reserve)))
            for f, plain in zip(packed, plains):
                if plain:
                    with LOCK:
                        gone = f["id"] not in FILES
                    if gone:                        # unloaded (or reset) while it was being decompressed
                        _remove_file({"path": plain, "owned": True})
                        continue
                    if f["owned"]:                  # the compressed copy is ours and now unused: drop it
                        _remove_file(f)
                    f["path"], f["owned"] = plain, True
            logs = [f for f in logs if f["id"] in FILES]
        paths = list(dict.fromkeys(f["path"] for f in logs))   # the same file added twice is read once
        done = {}                                   # path -> (chunks, index, error)
        keys = {p: _parse_key(p) for p in paths}
        for p in paths:
            hit = _cache_load(keys[p])
            if hit is not None:
                done[p] = hit
        todo = [p for p in paths if p not in done]
        if todo:
            chunks = {}
            prof = olp.profile_files(_args(), todo, chunks=chunks)
            for p in todo:
                done[p] = (chunks.get(p, []), prof.index.get(p), prof.errors.get(p))
                if not done[p][2]:                  # a complete parse only: worth reading back next time
                    _cache_save(keys[p], done[p])
        for f in logs:
            CHUNKS[f["id"]], INDEX[f["id"]], err = done[f["path"]]
            f["usable"] = any(p.header_ok for c in CHUNKS[f["id"]] for p in c.parts.values())
            f["problem"] = err or (None if f["usable"] else "no Odoo log lines recognised" if ONLY_DB is None
                                   else "no log lines of database %s (or without one)" % ONLY_DB)


# --------------------------------------------------------------------------- #
# parse cache: loading the same logs again (a restart, the same files dropped again) reads their parsed
# chunks back from disk instead of parsing them again
# --------------------------------------------------------------------------- #

PARSE_CACHE = os.environ.get("LOGTOOL_PARSE_CACHE", "1") != "0"
PARSE_CACHE_MAX = 4 << 30           # bytes of parsed chunks kept on disk; the least recently used go first
_PARSER_ID = None


def _parsed_dir():
    """Where parsed chunks are kept (~/.cache/logtool/logtool-parsed, see main), or None when it cannot be
    trusted: the cache is unpickled, so it must be a directory of this user's that nobody else can write to
    (a shared /tmp fallback would let another local user plant a file that runs code when loaded)."""
    d = os.path.join(tempfile.gettempdir(), "logtool-parsed")
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.lstat(d)
    except OSError:
        return None
    if not os.path.isdir(d) or os.path.islink(d) or st.st_uid != os.getuid() or st.st_mode & 0o022:
        return None
    return d


def _parse_key(path):
    """What a parse of `path` depends on: its content (size, first and last MiB -- logs only grow, and two
    different logs of the same size do not start and end with the same bytes), the parser's own code, the
    chunking settings and the database it was limited to (ONLY_DB). None when it cannot be read (it is then parsed, and fails, as usual)."""
    global _PARSER_ID
    if not PARSE_CACHE:
        return None
    try:
        if _PARSER_ID is None:
            with open(olp.__file__, "rb") as fh:
                _PARSER_ID = hashlib.sha1(fh.read()).hexdigest()
        size = os.path.getsize(path)
        h = hashlib.sha1(("%s|%d|%d|%d|%s|" % (_PARSER_ID, size, olp.PAR_MIN_CHUNK, olp.PAR_MAX_CHUNK,
                                               "" if ONLY_DB is None else "db=" + ONLY_DB)).encode())
        with open(path, "rb") as fh:
            h.update(fh.read(1 << 20))
            if size > 1 << 20:                      # the last MiB (or what follows the first one, if less)
                fh.seek(max(1 << 20, size - (1 << 20)))
                h.update(fh.read())
        return h.hexdigest()
    except OSError:
        return None


def _cache_load(key):
    d = _parsed_dir() if key is not None else None
    if d is None:
        return None
    path = os.path.join(d, key + ".pickle")
    try:
        with open(path, "rb") as fh:
            chunks, index = pickle.load(fh)
        os.utime(path)                              # recently used: evicted last
        return chunks, index, None
    except FileNotFoundError:
        return None
    except Exception:                               # corrupt / from another Python: parse again (and replace it)
        return None


def _cache_save(key, parsed):
    """Write one file's parsed chunks (best effort: no room, no cache), then trim the cache to PARSE_CACHE_MAX."""
    d = _parsed_dir() if key is not None else None
    if d is None:
        return
    try:
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, ".%s.%d.tmp" % (key, os.getpid()))
        with open(tmp, "wb") as fh:
            pickle.dump((parsed[0], parsed[1]), fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, os.path.join(d, key + ".pickle"))
        entries = sorted((e for e in os.scandir(d) if e.name.endswith(".pickle")), key=lambda e: e.stat().st_mtime, reverse=True)
        total = 0
        for e in entries:
            total += e.stat().st_size
            if total > PARSE_CACHE_MAX:
                os.remove(e.path)
    except OSError:
        try:
            os.remove(tmp)
        except (OSError, UnboundLocalError):
            pass


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
        gen = ONLY_GEN
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
            if gen != ONLY_GEN:                     # "only read database" changed meanwhile: this parse is stale
                raise NoData("the database to read changed while the filter was applied: try again")
            for fid, path in todo:
                out[fid] = chunks.get(path, [])
                FCHUNKS[(fid, flt[0])] = out[fid]   # one entry per file: the current filter's (freed above)
    return [out[f["id"]] for f in files]


def _prepare_filter(flt):
    """Re-parse the selected files with a new filter before taking LOCK (see _filter_parse)."""
    if flt[0] is None:
        return
    with LOCK:
        files = [FILES[i] for i in ORDER if FILES[i].get("kind") == "log" and FILES[i].get("usable")
                 and i not in request.args.getlist("exclude_file")]
    with FILTER_LOCK:                               # one filter re-parse at a time (concurrent ones would just repeat it)
        _filter_parse(files, flt)


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


def _merge(pieces, exclude_db):
    """One profile of the given per-database pieces. It is cached and only read from then on, so what it holds
    per minute is packed (see olp.PackedHist) once merged."""
    merged = olp.Profile(_args())
    kids = []
    for parts in pieces:
        for db, child in parts.items():
            if db not in exclude_db:
                merged.merge(child, heavy=False)        # the rest on first need (see _heavy)
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
    logs = [f for f in files if f.get("kind") != "summary"]
    if not logs:
        raise NoData("no files loaded" if not files else "no log files loaded (only imported summaries)")
    inc = [f for f in logs if f["id"] not in exclude_file and f["usable"]]
    if not inc:
        raise NoData("no files selected" if any(f["usable"] for f in logs)
                     else "none of the loaded files contains Odoo log lines")

    # databases that exist in the selected files (a file's private dbs vanish with it)
    counts = {}
    for f in inc:
        for c in CHUNKS[f["id"]]:
            for db, child in c.parts.items():
                counts[db] = counts.get(db, 0) + child.header_ok
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
        prof = _remember(BASE, key, _merge([c.parts for i in ids for c in _chunks(FILES[i], flt)], excl), 4)
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
        prof = _remember(CUT_BASE, key, _merge([p for f in inc for p in _slice_parts(f, lo, hi, flt, edges)], excl), 6)
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


def _summary_view(entry, files):
    """An analyze()-shaped result built entirely from a previously exported summary (see export()) --
    no raw log data backs it, so the file / database / time-interval controls stay empty; the client
    hides them once it sees no window. A newer export's own "hist" carries the histogram along with it
    (an older one, from before export() saved it, has none, so the histogram stays empty too)."""
    hist = entry.get("hist")
    return {
        "files": [_file_json(f) for f in files],
        "databases": [],
        "excluded_db": [],
        "interval": None,
        "window": None,
        # export() drops the real file names for privacy, keeping only their count (files_n); the view
        # normally carries one entry per file (only their number is ever shown), so a same-length filler
        # reproduces that without inventing names that were never there.
        "view": dict(entry, rows_key="import", files=[None] * entry.get("files_n", 0)),
        "full_totals": entry["totals"],
        "hist_key": "import" if hist else None,
        "hist": hist if hist else {"base": 1, "all": [], "ops": []},
        "imported": True,
        "pinned_ops": [],
    }


def analyze(exclude_db, exclude_file, interval, init, have_hist=None, have_rows=None, flt=NOFILT, pins=None):
    """Files -> databases -> time interval. Raises NoData when there is nothing to show."""
    with LOCK:
        files = [FILES[i] for i in ORDER]
        for f in files:
            _load(f)
        if not files:
            raise NoData("no files loaded")
        has_logs = any(f.get("kind") != "summary" and f["usable"] and f["id"] not in exclude_file for f in files)
        if not has_logs:
            summ = next((f for f in files
                         if f.get("kind") == "summary" and f["usable"] and f["id"] not in exclude_file), None)
            if summ is not None:
                return _summary_view(SUMMARIES[summ["id"]], files)

        files, inc, dbs, exclude_db = _select(exclude_db, exclude_file, init)
        ids = tuple(f["id"] for f in inc)
        excl = frozenset(exclude_db)
        full = _cache_get(FULL, (ids, excl, flt[0]))
        if full is None:
            prof = _base(ids, excl, flt)
            full = _remember(FULL, (ids, excl, flt[0]), {
                "summary": _summary(prof, inc),
                "timeline": _timeline_for(prof),
                "rows": _op_rows(prof),
                "window": [_secs(prof.t_min), _secs(prof.t_max) + 1] if prof.t_min else None,
            })

        sel = "%s|%s|%s|%s" % (",".join(ids), "\x1f".join(sorted(excl)), flt[0] or "", ONLY_DB or "")
        hist_key = sel                                                          # the operation timelines depend on this only
        view, rows, rows_key = full["summary"], full["rows"], sel + "|full"
        if interval:
            lo, hi = interval
            if full["window"]:                                                  # nothing outside the logs can be selected
                lo, hi = max(lo, full["window"][0]), min(hi, full["window"][1])
            if not full["window"] or lo >= hi:
                raise NoData("the interval lies outside the loaded logs")
            if (lo, hi) == tuple(full["window"]):
                interval = None
            else:
                interval = (lo, hi)
                key = (ids, excl, lo, hi, flt[0])
                cut = _cache_get(CUTS, key)
                if cut is None:
                    prof = _cut(inc, ids, excl, lo, hi, flt)
                    cut = _remember(CUTS, key, {"summary": _summary(prof, inc), "rows": _op_rows(prof)})
                view, rows, rows_key = cut["summary"], cut["rows"], "%s|%d-%d" % (sel, lo, hi)

        timeline = full["timeline"]
        # what the client has pinned (chart items, selected errors) even when it is outside the top MAX_ISSUES
        # messages / MAX_HIST_OPS operation timelines that are sent anyway
        pinned_ops = []
        pin_issues = [tuple(k) for k in (pins or {}).get("issues", [])
                      if tuple(k) not in {(r["level"], r["logger"], r["message"]) for r in view["issues"]}]
        pin_ops = [o for o in (pins or {}).get("ops", []) if o not in {x["operation"] for x in timeline["ops"]}]
        if pin_issues or pin_ops:
            whole = _base(ids, excl, flt)
            prof = _cut(inc, ids, excl, interval[0], interval[1], flt) if interval else whole
            base = _timeline_base(prof)["base"]
            rows = [dict(_issue_json(prof, k, prof.issues[k], base), pinned=True) for k in pin_issues if k in prof.issues]
            if rows:
                view = dict(view, issues=view["issues"] + rows)
            pinned_ops = [{"family": whole.det_fam.get(o, ""), "app": whole.det_app.get(o, ""), "operation": o,
                           "hist": _rebin(whole.det[o].hist, timeline["base"])} for o in pin_ops if o in whole.det]
        return {
            "files": [_file_json(f) for f in files],                             # parsing may just have found problems
            "databases": dbs,
            "excluded_db": sorted(excl),
            "interval": list(interval) if interval else None,
            "window": full["window"],
            "view": dict(view, rows=None if have_rows == rows_key else rows, rows_key=rows_key),
            "full_totals": full["summary"]["totals"],
            # the histogram is always the whole file + database selection, so it only travels when that
            # selection changed (the client says which one it already has)
            "hist_key": hist_key,
            "hist": dict(timeline, ops=None) if have_hist == hist_key else timeline,
            "pinned_ops": pinned_ops,
        }


MAX_EXAMPLES = 3        # operations of an app / type whose slowest log line is shown


def _examples(op, b):
    """The real access / job lines kept for one operation: slowest, first and first failing (duplicates dropped)."""
    ex, out, seen = b.ex or {}, [], set()
    for kind in ("slow", "first", "err"):
        e = ex.get(kind)
        if e and e[-1] not in seen:
            seen.add(e[-1])
            out.append({"kind": {"slow": "slowest", "first": "first seen", "err": "first failing"}[kind], "operation": op,
                        "line": e[-1], "seconds": round(e[0], 3) if kind == "slow" else None})
    return out


def _sources(prof, ops, calls):
    """Untimed log lines that were logged while `ops` ran (matched by worker and time), most frequent first."""
    agg = {}
    for op in ops:
        for key, (n, line, runs, secs) in prof.src.get(op, {}).items():
            e = agg.get(key)
            if e is None:
                e = agg[key] = {"key": key, "n": 0, "sample": line, "ops": {}, "runs": 0, "secs": 0.0}
            e["n"] += n
            e["runs"] += runs
            e["secs"] += secs
            e["sample"] = e["sample"] or line
            e["ops"][op] = e["ops"].get(op, 0) + n
    out = sorted(agg.values(), key=lambda e: -e["n"])
    for e in out:
        e["per_call"] = round(e["n"] / calls, 2) if calls else None
        e["avg_ms"] = round(1000 * e.pop("secs") / e["runs"], 1) if e["runs"] else None   # of the requests / jobs that logged it
        e["ops"] = sorted(e["ops"].items(), key=lambda kv: -kv[1])[:3]
    return {"total": sum(e["n"] for e in out), "distinct": len(out), "items": out}


def _group_ops(prof, by, name):
    """The operations of one operation / app / type row, heaviest first."""
    if by == "operation":
        return [name] if name in prof.det else []
    owner = prof.det_app if by == "app" else prof.det_fam
    return sorted((op for op in prof.det if owner.get(op) == name), key=lambda op: -prof.det[op].total)


def _row_facts(prof, by, name, logged=False):
    """The facts dict (see olp._fact) behind one row: an operation's own, or those of every operation of an app / type,
    and its seconds twin (see olp._fact_time; None for a warning / error, which has no time of its own).
    logged: those of the lines logged while its requests ran instead (users and records, see Profile._logged_facts)."""
    if by == "issue":
        return ({} if logged else prof.issue_facts.get(name) or {} if name in prof.issues else {}), None
    own, own_t = (prof.op_logged, prof.op_logged_s) if logged else (prof.op_facts, prof.op_facts_s)
    ops = _group_ops(prof, by, name)
    if by == "operation":
        return (own.get(name) or {}, own_t.get(name) or {}) if ops else ({}, {})
    facts, times = {}, {}
    for op in ops:
        olp.merge_facts(facts, own.get(op) or {})
        olp.merge_fact_times(facts, times, own_t.get(op) or {})
    return facts, times


def _facts_view(prof, prof0, by, name, flt, logged=False):
    """Every kept value behind one row, for its details tab. A detail the row is filtered on lists its values
    from the unfiltered selection instead (prof0), so more of them can still be picked."""
    facts, times = _row_facts(prof, by, name, logged)
    facts, times = dict(facts), None if times is None else dict(times)
    want = (flt[1] or {}).get(by, {}).get(name) or {}
    if want:
        unf, unf_t = _row_facts(prof0, by, name, logged)
        for slot in want:
            if slot in unf:
                facts[slot] = unf[slot]
                if times is not None:
                    times[slot] = unf_t.get(slot) or {}
    out = _facts_json(facts, _ip_logins(prof0), olp.FACT_KEEP, times)
    for f in out:
        f["unfiltered"] = f["detail"] in want
    return out


def _detail(prof, by, name, facts, logged=()):
    """What the popup of one operation / app / type shows: example log lines and the untimed lines matched to it.
    logged: the details of the lines logged while it ran (users, records), like facts but from those lines."""
    ops = _group_ops(prof, by, name)
    if not ops:                                 # filtered away, or gone from the selection
        return dict({"found": False}, **({"facts": facts} if facts else {}), **({"logged_facts": logged} if logged else {}))
    if by == "operation":
        b = prof.det[name]
        first, last, active = _span(b)
        return {"found": True, "by": by, "name": name, "examples": _examples(name, b),
                "top_ops": [], "sources": _sources(prof, [name], b.n), "facts": facts, "logged_facts": logged,
                "calls": b.n, "first": first, "last": last, "active_minutes": active}
    first, last, active = _span(*(prof.det[op] for op in ops))
    calls = sum(prof.det[op].n for op in ops)
    examples = []
    for op in ops[:MAX_EXAMPLES]:
        examples += _examples(op, prof.det[op])[:1]                 # the slowest line of each heaviest operation
    return {"found": True, "by": by, "name": name, "examples": examples,
            "top_ops": [{"operation": op, "calls": prof.det[op].n, "total_s": round(prof.det[op].total, 3)}
                        for op in ops],
            "ops_n": len(ops),
            "sources": _sources(prof, ops, calls),
            "facts": facts, "logged_facts": logged, "calls": calls,
            "first": first, "last": last, "active_minutes": active}


def detail(by, name, exclude_db, exclude_file, interval, flt=NOFILT):
    """The popup data for one row of the operation / app / type table, for the same selection the table shows."""
    with LOCK:
        _, inc, _, exclude_db = _select(exclude_db, exclude_file, False)
        ids, excl = tuple(f["id"] for f in inc), frozenset(exclude_db)
        prof = _heavy(_prof_for(inc, ids, excl, interval, flt))
        prof0 = _heavy(_prof_for(inc, ids, excl, interval)) if flt[0] else prof
        return _detail(prof, by, name, _facts_view(prof, prof0, by, name, flt),
                       _facts_view(prof, prof0, by, name, flt, logged=True))


def issue_detail(key, exclude_db, exclude_file, interval, flt=NOFILT):
    """Every kept detail behind one warning / error (the analysis only sends the most frequent few)."""
    with LOCK:
        _, inc, _, exclude_db = _select(exclude_db, exclude_file, False)
        ids, excl = tuple(f["id"] for f in inc), frozenset(exclude_db)
        prof = _prof_for(inc, ids, excl, interval, flt)
        prof0 = _prof_for(inc, ids, excl, interval) if flt[0] else prof
        return {"found": key in prof.issues, "facts": _facts_view(prof, prof0, "issue", key, flt)}


def timeline(exclude_db, exclude_file, interval, flt, issue_keys, pin_ops=()):
    """The histogram of just seconds [from, to) (the "Focus interval" button), at the finest bucket size that
    span allows -- down to 1 minute however long the whole log is -- plus the timelines of the pinned
    warnings / errors (issue_keys) over the same span. Same shapes as _timeline_for()."""
    with LOCK:
        _, inc, _, exclude_db = _select(exclude_db, exclude_file, False)
        ids, excl = tuple(f["id"] for f in inc), frozenset(exclude_db)
        prof = _base(ids, excl, flt)
        if not prof.t_min:
            raise NoData("the selection has no timestamps")
        w0, w1 = _secs(prof.t_min), _secs(prof.t_max) + 1
        lo, hi = interval or (w0, w1)
        lo, hi = max(lo, w0), min(hi, w1)
        if lo >= hi:
            raise NoData("the interval lies outside the loaded logs")
        key = (ids, excl, flt[0], lo, hi, tuple(issue_keys), tuple(pin_ops))
        hit = _cache_get(TIMELINES, key)
        if hit is not None:
            return hit
        m0, m1 = lo // 60, -(-hi // 60)
        base = next((n for n in NICE_MIN if (m1 - m0) / n <= MAX_BINS), NICE_MIN[-1])

        def cut(h):
            return {m: v for m, v in h.items() if m0 <= m < m1}
        total, per = {}, []
        for op, b in prof.det.items():
            h = cut(b.hist)
            if h:
                _add_hist(total, h)
                per.append((sum(v[1] + v[2] + v[3] for v in h.values()), op, h))
        per.sort(key=lambda x: -x[0])
        issues = []
        for k in issue_keys:
            e = prof.issues.get(k)
            issues.append({"level": k[0], "logger": k[1], "message": k[2], "hist": _rebin(cut(e[6]), base) if e else []})
        return _remember(TIMELINES, key, {
            "base": base, "from": lo, "to": hi, "all": _rebin(total, base), "issues": issues,
            "ops": [{"family": prof.det_fam.get(op, ""), "app": prof.det_app.get(op, ""), "operation": op,
                     "hist": _rebin(h, base)} for i, (_, op, h) in enumerate(per) if i < MAX_HIST_OPS or op in pin_ops],
        }, 8)


EXPORT_NOTE = ("Database names, uploaded file names, and raw log-line samples are left out entirely. Known "
               "database and file names are blanked out wherever they appeared in free text as whole words "
               "(except names shorter than 4 characters and common words such as 'odoo' or 'test', which would "
               "wreck every logger and message), along with email and IP (v4 and v6) addresses. Client IPs, session ids and login names are replaced by pseudonyms (ip-1, "
               "session-3, user-2) that stay the same throughout this file, so one client can be followed across "
               "operations, messages and time without saying who it is. User ids and record ids are kept as "
               "numbers. Operation, cron and automation-rule names come straight from this database's own "
               "configuration (for example a custom cron job's title) and are not further redacted -- skim "
               "them before sharing this file widely.")
EXPORT_FIELDS = {
    "timestamps": "The log's own clock, no time zone. 'first' / 'last' / '*_seen' are YYYY-MM-DDTHH:MM[:SS].",
    "hist": "Timelines. hist.base is the bucket size in minutes; hist.all and each hist.ops[].hist are "
            "[[bucket start (minutes since 1970-01-01), runs, sql s, python s, job s], ...], buckets with nothing "
            "left out. Every cron / automation / module-load operation has one, plus the busiest operations.",
    "rows": "One per operation (request pattern, cron, automation rule), whole selection. first_seen / last_seen "
            "and active_minutes say when it ran; facts lists what grouping collapsed.",
    "facts": "Per row / message: [{detail, values: [[value, count] or [value, count, [logins from that ip]]], "
             "distinct, other_n, total}], most frequent first. details: 'client IP', 'session' (Odoo 19.4+), 'URL' "
             "(the raw path, when ids or a query were collapsed), 'cron id', 'status', 'remaining', 'rule id', "
             "'records processed', 'user id' and 'records' (named in a message or its traceback), and '<word> #' "
             "(the number that followed <word> where the message shows '#'). A message's 'client IP' / 'session' is "
             "the request its worker was running when it was logged. Only the first 50 distinct values of a "
             "detail are told apart; other_n counts the rest.",
    "issues": "Every distinct warning / error message (numbers replaced by #), with its own timeline.",
    "clients": "Who sent requests when (assets and scanner probes left out): a client is an ip, or ip + session "
               "from Odoo 19.4. logins are the accounts that logged in from that ip; ops are its busiest operations.",
    "logins": "Login lines paired with the access line of the same worker: account, ip, result, count, first / last.",
    "log_levels": "Log lines per level. What is missing here (e.g. no DEBUG) is missing from everything else.",
    "limits": "What this log cannot show, worked out from what it contains. Read before drawing conclusions.",
}
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# IPv6: all eight groups, or a "::" shortening (so 13:05:09 times stay), with an optional zone (%eth0)
_H = "[0-9A-Fa-f]{1,4}"
_IP6_RE = re.compile(r"(?<![\w:.])(?:{h}(?::{h}){{7}}|{h}(?::{h}){{0,6}}::(?:{h}(?::{h}){{0,6}})?|::{h}(?::{h}){{0,6}})"
                     r"(?:%[\w.]+)?(?![\w:])".format(h=_H))
_LOG_SUFFIXES = (".gz", ".bz2", ".json", ".log", ".txt")
# words too common in an Odoo log to blank out: a file named odoo.log must not turn every 'odoo.http' into [redacted]
_GENERIC_NAMES = {"odoo", "odoo-server", "openerp", "server", "log", "logs", "test", "tests", "debug", "error",
                  "errors", "warning", "info", "http", "web", "base", "main", "prod", "production", "staging", "dev",
                  "local", "db", "database", "postgres", "default", "none", "worker", "cron", "access", "out"}


def _secret_names(dbs, files):
    """The database and file names an export must not show, as words to blank out (see _scrub). A file name
    loses its suffixes ('acme-prod.log.gz' -> 'acme-prod'); names shorter than 4 characters or common words
    ('odoo', 'test') are left alone: blanking them would wreck every logger and message that contains them."""
    names = set(dbs)
    for f in files:
        name = f["name"]
        while name.lower().endswith(_LOG_SUFFIXES):
            name = os.path.splitext(name)[0]
        names.add(name)
    names = {n for n in names if n and len(n) >= 4 and n.lower() not in _GENERIC_NAMES}
    return sorted(names, key=len, reverse=True)


def _scrub(text, secrets):
    """Blank out this selection's own database / file names wherever they show up in free text as a whole
    name (not inside a longer word), plus email and IP (v4 and v6) addresses. Numbers and ids are already
    replaced by '#' upstream (see the issue message template); this only catches what that pass does not."""
    if not text:
        return text
    for s in secrets:
        if s in text:
            text = re.sub(r"(?<![\w-])%s(?![\w-])" % re.escape(s), "[redacted]", text)
    return _IP6_RE.sub("[ip]", _IP_RE.sub("[ip]", _EMAIL_RE.sub("[email]", text)))


class _Alias:
    """Stable pseudonyms: the same (kind, value) always gets the same name, e.g. ("ip", "10.0.0.7") -> "ip-3"."""

    def __init__(self):
        self.maps = {}

    def __call__(self, kind, value):
        m = self.maps.setdefault(kind, {})
        if value not in m:
            m[value] = "%s-%d" % (kind, len(m) + 1)
        return m[value]


def _scrub_url(url, secrets):
    """A raw path with its query values and token-like segments blanked (they can be access tokens); ids stay."""
    path, q, query = url.partition("?")
    path = "/".join("<token>" if olp._TOKEN.match(p) and any(c.isdigit() for c in p) else p for p in path.split("/"))
    return _scrub(path + ("?" + re.sub(r"=[^&]*", "=…", query) if q else ""), secrets)


def _scrub_facts(facts, secrets, alias):
    """_facts_json() output with clients and accounts pseudonymized (see _Alias) and free text scrubbed."""
    def one(slot, v):
        if v == olp.FACT_OTHER:
            return v
        if slot == olp.F_IP:
            return alias("ip", v)
        if slot == olp.F_SESSION:
            return alias("session", v)
        if slot == olp.F_LOGIN:
            return alias("user", v)
        if slot == olp.F_URL:
            return _scrub_url(v, secrets)
        return _scrub(v, secrets)
    return [dict(f, detail=_scrub(f["detail"], secrets),
                 values=[[one(f["detail"], e[0]), e[1]] + ([[alias("user", x) for x in e[2]]] if len(e) > 2 else [])
                         for e in f["values"]])
            for f in facts]


def _scrub_row(r, secrets):
    return dict(r, family=_scrub(r["family"], secrets), app=_scrub(r["app"], secrets),
                operation=_scrub(r["operation"], secrets))


def _scrub_timeline_op(o, secrets):
    return dict(o, family=_scrub(o["family"], secrets), app=_scrub(o["app"], secrets),
                operation=_scrub(o["operation"], secrets))


def _scrub_issue(i, secrets, alias):
    return {"level": i["level"], "logger": _scrub(i["logger"], secrets), "exc": _scrub(i["exc"], secrets),
            "src": _scrub(i["src"], secrets), "message": _scrub(i["message"], secrets), "n": i["n"],
            "first": i["first"], "last": i["last"], "ops_n": i["ops_n"], "unattributed": i["unattributed"],
            "ops": [[_scrub(op, secrets), n] for op, n in i["ops"]], "hist": i["hist"],
            "facts": _scrub_facts(i["facts"], secrets, alias)}


def _export_clients(prof, secrets, alias, base):
    """Per client: when it was active (a timeline at the histogram's resolution), what it ran and who logged in from it."""
    ipl = _ip_logins(prof)
    by_ip = {}
    for op, f in prof.op_facts.items():
        if op in prof.det:
            for ip, n in (f.get(olp.F_IP) or {}).items():
                by_ip.setdefault(ip, []).append((op, n))
    out = []
    for c, h in sorted(prof.clients.items(), key=lambda kv: -sum(v[0] for v in kv[1].values())):
        other = c == olp.CLIENT_OTHER
        ip, _, sess = ("", "", "") if other else c.partition(" ")
        bins = {}
        for m, (n, secs) in h.items():
            e = bins.setdefault(m // base * base, [0, 0.0])
            e[0] += n
            e[1] += secs
        out.append({
            "client": c if other else alias("ip", ip) + (" " + alias("session", sess) if sess else ""),
            "logins": [] if other else [alias("user", x) for x in ipl.get(ip, [])],
            "requests": int(sum(v[0] for v in h.values())),
            "seconds": round(sum(v[1] for v in h.values()), 3),
            "first": _iso(min(h) * 60)[:16], "last": _iso(max(h) * 60)[:16],
            "hist": [[k, int(e[0]), round(e[1], 3)] for k, e in sorted(bins.items())],
            "ops": [] if other else [[_scrub(op, secrets), n] for op, n in
                                     sorted(by_ip.get(ip, []), key=lambda kv: -kv[1])[:10]],
        })
    return out


def _limits(prof, issues_n, clients):
    """What this log cannot tell, worked out from what it contains (see EXPORT_FIELDS["limits"])."""
    lv, fm = prof.all_levels, prof.formats
    out = []
    if not lv.get("DEBUG"):
        out.append("No DEBUG lines. Automation rules triggered on save (on create, on write, on change, ...) log "
                   "only at DEBUG%s, so the rows show time-based rules and crons only; the absence of lines at a "
                   "given time says nothing about rules that run on save."
                   % (" (Odoo 20 does log them, with a duration, when slower than 0.1 s)" if any(
                       k.startswith("automation 20") for k in fm) else ""))
    if not any(k.startswith("access: odoo.http.server") for k in fm):
        out.append("No session per request: before Odoo 19.4 the access line carries only the client IP, so a "
                   "client is an IP address, and everyone behind one proxy or office network shares it.")
    out.append("No user per request: Odoo does not log the user on the access line. 'logins' ties an account to "
               "the IP it logged in from, at login time only; a user id is known only where a message names it "
               "(facts 'user id', e.g. access errors).")
    out.append("Crons, automation rules and module loading are timed from their own log messages: one duration per "
               "run, no SQL / Python split. Server actions have no timing of their own; their time is part of the "
               "request, cron or rule that ran them.")
    ips = {c["client"].split(" ")[0] for c in clients if c["client"] != olp.CLIENT_OTHER}
    reqs = sum(c["requests"] for c in clients)
    if 0 < len(ips) <= 2 and reqs >= 1000:
        out.append("All %d requests come from %d IP address%s: probably a reverse proxy in front of Odoo without "
                   "--proxy-mode, so the IP does not identify the client." % (reqs, len(ips), "" if len(ips) == 1 else "es"))
    if any(c["client"] == olp.CLIENT_OTHER for c in clients):
        out.append("Only the first %d clients are told apart; later ones are grouped as '%s'."
                   % (olp.CLIENT_KEEP, olp.CLIENT_OTHER))
    if issues_n > MAX_EXPORT_ISSUES:
        out.append("Only the %d most frequent of %d distinct warning / error messages are included."
                   % (MAX_EXPORT_ISSUES, issues_n))
    return out


def export(exclude_db, exclude_file, interval, flt=NOFILT):
    """A scrubbed copy of the current selection's summary, safe to hand to someone outside the team
    (or an AI): no database or file names, no raw log-line samples, clients and accounts pseudonymized.
    See EXPORT_NOTE for what this cannot catch."""
    with LOCK:
        files, inc, dbs, exclude_db = _select(exclude_db, exclude_file, False)
        ids, excl = tuple(f["id"] for f in inc), frozenset(exclude_db)
        prof = _heavy(_prof_for(inc, ids, excl, interval, flt))
        view = _summary(prof, inc, MAX_EXPORT_ISSUES)
        secrets = _secret_names({d for d, _ in dbs if d and d not in (NONE_DB, "?", "None")}, inc)
        alias = _Alias()
        ipl = _ip_logins(prof)
        groups = {scope: [_scrub_row(r, secrets) for r in rows] for scope, rows in view["groups"].items()}
        timeline = _timeline_for(prof)
        have = {o["operation"] for o in timeline["ops"]}
        jobs = [{"family": prof.det_fam.get(k, ""), "app": prof.det_app.get(k, ""), "operation": k,
                 "hist": _rebin(b.hist, timeline["base"])}
                for k, b in prof.det.items() if prof.det_fam.get(k) in olp.JOB_KINDS and k not in have]
        timeline = dict(timeline, ops=[_scrub_timeline_op(o, secrets) for o in timeline["ops"] + jobs])
        rows = []
        for r in _op_rows(prof):
            first, last, active = _span(prof.det[r["operation"]])
            rows.append(dict(_scrub_row(r, secrets), first_seen=first, last_seen=last, active_minutes=active,
                             facts=_scrub_facts(_facts_json(prof.op_facts.get(r["operation"]), ipl,
                                                            times=prof.op_facts_s.get(r["operation"]) or {}), secrets, alias)))
        clients = _export_clients(prof, secrets, alias, timeline["base"])
        return {
            "note": EXPORT_NOTE,
            "fields": EXPORT_FIELDS,
            "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "files_n": len(inc),
            "databases_n": len(dbs),
            "limits": _limits(prof, view["issues_distinct"], clients),
            "log_levels": dict(prof.all_levels.most_common()),
            "totals": view["totals"],
            "levels": view["levels"],
            "stats": view["stats"],
            "groups": groups,
            "rows": rows,
            "issues": [_scrub_issue(i, secrets, alias) for i in view["issues"]],
            "issues_distinct": view["issues_distinct"],
            "clients": clients,
            "logins": [{"login": alias("user", login), "ip": alias("ip", ip), "result": res, "n": n,
                        "first": (prof.login_seen.get((login, ip, res)) or [None])[0],
                        "last": (prof.login_seen.get((login, ip, res)) or [None, None])[1]}
                       for (login, ip, res), n in prof.logins.most_common()],
            # the "Requests over time" histogram, so an imported summary (see _summary_view()) can still
            # show it; older exports simply lack this key and import with no histogram, as before.
            "hist": timeline,
        }


def _file_json(f):
    return {"id": f["id"], "name": f["name"], "size": f["size"], "problem": f.get("problem"),
            "kind": f.get("kind", "log")}


def _add_file(path, name, owned):
    fid = uuid.uuid4().hex[:10]
    rec = {"id": fid, "name": name, "path": path, "size": os.path.getsize(path), "owned": owned,
           "problem": None, "usable": True}
    with LOCK:
        FILES[fid] = rec
        ORDER.append(fid)
    return rec


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #

@app.before_request
def _guard():
    """The server only listens on this machine, but any web page open in the same browser can still send
    it requests. Refuse those: a foreign Origin on anything that changes state, and (when bound to
    loopback) a Host that is not a loopback name, which is what DNS rebinding looks like."""
    host = request.host
    host = host[1:host.find("]")] if host.startswith("[") else host.rsplit(":", 1)[0]    # [::1]:5000, [::1], name:5000
    if app.config.get("LOOPBACK_ONLY") and host not in LOOPBACK:
        abort(403)
    origin = request.headers.get("Origin")
    if request.method not in ("GET", "HEAD") and origin and urlsplit(origin).netloc != request.host:
        abort(403)


@app.get("/")
def index():
    return render_template("index.html", boot=BOOT_ID)


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.post("/api/reset")
def reset():
    """Unload every file and drop every cache, as if the app had just started (with every database read)."""
    set_only_db(None)
    with LOCK:
        gone = list(FILES.values())
        for cache in (FILES, CHUNKS, INDEX, SLICES, SUMMARIES):
            cache.clear()
        _forget_selections()
        ORDER.clear()
    for rec in gone:
        _remove_file(rec)
    return jsonify({"ok": True})


@app.get("/api/files")
def list_files():
    with LOCK:
        return jsonify([_file_json(FILES[i]) for i in ORDER])


@app.post("/api/files")
def upload():
    _upload_dir()
    added = []
    for up in request.files.getlist("files"):
        if not up.filename:
            continue
        ext = os.path.splitext(up.filename)[1].lower()
        ext = ext if ext in (".gz", ".bz2", ".json") else ".log"      # opener() picks codec by suffix
        path = os.path.join(UPLOAD_DIR, uuid.uuid4().hex[:10] + ext)
        try:
            up.save(path)
        except OSError as e:                            # e.g. out of disk space / over quota: the files
            try:                                         # saved before this one are already usable
                os.remove(path)
            except OSError:
                pass
            logging.getLogger("logtool").exception("upload failed")
            return jsonify({"added": added, "error": _disk_msg(e)}), 500
        name = up.filename.replace("\\", "/").rsplit("/", 1)[-1]          # a bare file name, whatever the client sent
        added.append(_file_json(_add_file(path, name, True)))
    return jsonify(added)


def set_only_db(name):
    """Read only database `name`'s log lines (and those without a database) from now on; None or "": every
    database. The other databases' lines are skipped right after their header, unparsed, which is what makes a
    big multi-database log load faster. Logs already loaded are parsed again with it at the next analysis."""
    global ONLY_DB, ONLY_GEN
    name = (name or "").strip() or None
    with LOAD_LOCK:                                 # let a running load finish first, without holding LOCK meanwhile
        pass
    with LOCK, LOAD_LOCK:                           # (in the order every other caller takes them: no deadlock)
        if name == ONLY_DB:
            return False
        ONLY_DB = name
        ONLY_GEN += 1
        for fid in [i for i in CHUNKS if FILES.get(i, {}).get("kind") != "summary"]:
            CHUNKS.pop(fid, None)
            INDEX.pop(fid, None)
            if fid in FILES:
                FILES[fid]["usable"], FILES[fid]["problem"] = True, None
        SLICES.clear()
        FCHUNKS.clear()
        _forget_selections()
    return True


@app.get("/api/only_db")
def api_only_db_get():
    return jsonify({"db": ONLY_DB})


@app.post("/api/only_db")
def api_only_db_post():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "send {\"db\": \"name\"}"}), 400
    name = body.get("db") or ""
    if not isinstance(name, str) or len(name) > 200 or len(name.split()) > 1:
        return jsonify({"error": "a database name is one word"}), 400
    changed = set_only_db(name)
    return jsonify({"db": ONLY_DB, "changed": changed})


@app.delete("/api/files/<fid>")
def unload(fid):
    with LOCK:
        rec = FILES.pop(fid, None)
        if rec is None:
            return jsonify({"error": "unknown file"}), 404
        ORDER.remove(fid)
        _forget_selections()
        for cache in (CHUNKS, INDEX, SUMMARIES):
            cache.pop(fid, None)
        for cache in (SLICES, FCHUNKS):
            for key in [k for k in cache if k[0] == fid]:
                cache.pop(key, None)
    _remove_file(rec)
    return jsonify({"ok": True})


def _interval_arg():
    """The requested time interval (from, to in seconds) or None. Raises NoData with a message for the user."""
    if request.args.get("from") is None or request.args.get("to") is None:
        return None
    try:
        interval = (int(request.args["from"]), int(request.args["to"]))
    except ValueError:
        raise NoData("the interval must be two whole numbers of seconds")
    interval = tuple(max(0, min(x, 10 ** 11)) for x in interval)    # far outside any log: clamped (dates overflow past that)
    if interval[0] >= interval[1]:
        raise NoData("the interval must end after it starts")
    return interval


def _ff_arg():
    """The fact filters (see olp._facts_ok) from the `ff` argument, as (cache key, args.fact_filter), or NOFILT.
    ff is a JSON list of {"by": operation | app | family | issue, "name": row name, or [level, logger, message]
    for an issue, "facts": {detail: [values]}}. Raises NoData with a message for the user."""
    raw, xd = request.args.get("ff"), request.args.get("xd")
    if not raw and not xd:
        return NOFILT
    bad = NoData("ff must be a list of {by, name, facts: {detail: [values]}}")
    try:
        items = json.loads(raw or "[]")
    except (ValueError, RecursionError):
        raise bad
    if not isinstance(items, list):
        raise bad
    ff = {}
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("facts"), dict):
            raise bad
        by, name = it.get("by"), it.get("name")
        if by == "issue" and isinstance(name, list) and len(name) == 3 and all(isinstance(x, str) for x in name):
            name = tuple(name)
        elif by not in ("operation", "app", "family") or not isinstance(name, str) or not name:
            raise bad
        want = ff.setdefault(by, {}).setdefault(name, {})
        for slot, vals in it["facts"].items():
            if not isinstance(vals, list) or not all(isinstance(v, str) for v in vals):
                raise bad
            if by == "issue" and slot in olp.ISSUE_UNFILTERABLE:
                continue
            vals = {v for v in vals if v != olp.FACT_OTHER}
            if vals:
                want[slot] = frozenset(want.get(slot, frozenset()) | vals)
        if not want:
            del ff[by][name]
    ff = {by: rows for by, rows in ff.items() if rows}
    extra = _xd_arg(xd)
    if extra:
        ff["extra"] = extra
    if not ff:
        return NOFILT
    canon = sorted([by, list(name) if by == "issue" else [name], sorted([k, sorted(v)] for k, v in want.items())]
                   for by, rows in ff.items() if by != "extra" for name, want in rows.items())
    return json.dumps([canon, extra], ensure_ascii=False, sort_keys=True), ff


def _xd_arg(xd):
    """The part of the global domain the app's own filters cannot hold (the `xd` argument, a JSON prefix list),
    validated; None when there is none. Raises NoData with a message for the user."""
    if not xd:
        return None
    try:
        node = olp.dom_tree(json.loads(xd))
    except (ValueError, RecursionError) as e:           # DomainError is a ValueError too
        raise NoData("xd: %s" % e)
    if node is not None and olp.dom_mentions(node, olp.DOM_SELECTION):
        raise NoData("xd: file, database and time are chosen with their own arguments")
    if node is not None and olp.dom_mentions(node, olp.DOM_VIEW):
        raise NoData("xd: view settings are not conditions")
    return olp.dom_prefix(node) or None


def _pins_arg():
    """{"issues": [[level, logger, message]], "ops": [operation]} from the `pins` argument (see analyze()), or None."""
    raw = request.args.get("pins")
    if not raw:
        return None
    try:
        pins = json.loads(raw)
    except (ValueError, RecursionError):
        raise NoData("pins must be JSON")
    ok = isinstance(pins, dict) and isinstance(pins.get("issues", []), list) and isinstance(pins.get("ops", []), list) and \
        all(isinstance(k, list) and len(k) == 3 and all(isinstance(x, str) for x in k) for k in pins.get("issues", [])) and \
        all(isinstance(o, str) for o in pins.get("ops", []))
    if not ok:
        raise NoData("pins must be {issues: [[level, logger, message]], ops: [operation]}")
    return {"issues": pins.get("issues", [])[:100], "ops": pins.get("ops", [])[:200]}


def _issue_key_arg(level, logger, msg):
    return (level, logger, msg) if level and logger and msg is not None else None


@app.get("/api/detail")
def api_detail():
    by, name = request.args.get("by"), request.args.get("name")
    if by not in ("operation", "app", "family") or not name:
        return jsonify({"error": "by must be operation, app or family, and name is required"}), 400
    try:
        _prepare_filter(_ff_arg())
        return jsonify(detail(by, name, set(request.args.getlist("exclude_db")), set(request.args.getlist("exclude_file")),
                              _interval_arg(), _ff_arg()))
    except NoData as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("detail failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500


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


@app.get("/api/issue_detail")
def api_issue_detail():
    key = _issue_key_arg(request.args.get("level"), request.args.get("logger"), request.args.get("msg"))
    if key is None:
        return jsonify({"error": "level, logger and msg are required"}), 400
    try:
        _prepare_filter(_ff_arg())
        return jsonify(issue_detail(key, set(request.args.getlist("exclude_db")), set(request.args.getlist("exclude_file")),
                                    _interval_arg(), _ff_arg()))
    except NoData as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("issue_detail failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500


@app.get("/api/timeline")
def api_timeline():
    try:
        _prepare_filter(_ff_arg())
        try:
            keys = json.loads(request.args.get("issues") or "[]")
        except ValueError:
            raise NoData("issues must be JSON")
        if not isinstance(keys, list) or not all(isinstance(k, list) and len(k) == 3 and all(isinstance(x, str) for x in k)
                                                 for k in keys):
            raise NoData("issues must be a list of [level, logger, message]")
        return jsonify(timeline(set(request.args.getlist("exclude_db")), set(request.args.getlist("exclude_file")),
                                _interval_arg(), _ff_arg(),
                                [tuple(k) for k in keys[:50]], frozenset(request.args.getlist("pin_op")[:200])))
    except NoData as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("timeline failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500


@app.get("/api/issue_lines")
def api_issue_lines():
    """Every occurrence of one warning / error, re-found by re-reading the loaded files (see
    scan_issue_occurrences): the details popup lists these so the user can pick which one to see
    context around, instead of only ever seeing the first one the parser happened to keep."""
    level, logger, msg = request.args.get("level"), request.args.get("logger"), request.args.get("msg")
    if not level or not logger or msg is None:
        return jsonify({"error": "level, logger and msg are required"}), 400
    try:
        with LOCK:
            _, inc, _, exclude_db = _select(set(request.args.getlist("exclude_db")),
                                             set(request.args.getlist("exclude_file")), False)
            interval = _interval_arg()
            lo, hi = interval if interval else (None, None)
            files = [(f["id"], f["name"], f["path"]) for f in inc]
        ff = _ff_arg()[1] or {}
        _check_on_disk(files)
        kw = dict(level=level, logger=logger, template=msg, exclude_db=exclude_db, lo=lo, hi=hi, limit=MAX_ISSUE_LINES, ff=ff,
                  only_db=ONLY_DB)
        tasks = [(f, piece) for f in files for piece in _pieces(f[0], f[2], lo, hi)]      # every file's, in order
        out = []
        for (f, _piece), part in zip(tasks, olp.scan_pieces("issue", [(f[2], p) for f, p in tasks], kw, MAX_ISSUE_LINES)):
            out += [{"file": f[0], "file_name": f[1], "line": o["line"], "stamp": o["stamp"]} for o in part]
        return jsonify(out[:MAX_ISSUE_LINES])
    except NoData as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("issue_lines failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500


def _slowest_floor(by, name, inc, exclude_db, interval, ff):
    """(calls, seconds): how many calls an operation made in the selection, and a time the slowest
    MAX_ISSUE_LINES of them surely all took more than -- from its percentile sample, with a 2x margin -- so a
    slowest-first scan can pass over the faster lines without parsing them. (0, None) when it cannot tell."""
    if by != "operation":
        return 0, None
    try:
        with LOCK:
            ids, excl = tuple(f["id"] for f in inc), frozenset(exclude_db)
            b = _prof_for(inc, ids, excl, interval, _ff_arg()).det.get(name)
    except NoData:
        return 0, None
    if b is None or not b.sample or b.n <= 2 * MAX_ISSUE_LINES:
        return (b.n if b else 0), None
    s = sorted(b.sample)
    return b.n, s[int(len(s) * (1 - 2.0 * MAX_ISSUE_LINES / b.n))]


@app.get("/api/op_lines")
def api_op_lines():
    """The timed lines (access lines, job messages) of one operation / app / type, re-found by re-reading the
    loaded files (see scan_op_occurrences): the first ones, or with order=slow the slowest. Like
    /api/issue_lines, the details popup lists them to pick one and see the raw lines around it."""
    by, name = request.args.get("by"), request.args.get("name")
    if by not in ("operation", "app", "family") or not name:
        return jsonify({"error": "by must be operation, app or family, and name is required"}), 400
    try:
        with LOCK:
            _, inc, _, exclude_db = _select(set(request.args.getlist("exclude_db")),
                                             set(request.args.getlist("exclude_file")), False)
            interval = _interval_arg()
            lo, hi = interval if interval else (None, None)
            files = [(f["id"], f["name"], f["path"]) for f in inc]
        slowest = request.args.get("order") == "slow"
        ff = _ff_arg()[1]
        _check_on_disk(files)
        kw = dict(by=by, name=name, exclude_db=exclude_db, lo=lo, hi=hi, limit=MAX_ISSUE_LINES, slowest=slowest, ff=ff,
                  only_db=ONLY_DB)
        tasks = [(f, piece) for f in files for piece in _pieces(f[0], f[2], lo, hi)]      # every file's, in order
        n_calls, kw["min_seconds"] = _slowest_floor(by, name, inc, exclude_db, interval, ff) if slowest else (0, None)

        def scan():
            out = []
            for (f, _piece), part in zip(tasks, olp.scan_pieces("op", [(f[2], p) for f, p in tasks], kw,
                                                                None if slowest else MAX_ISSUE_LINES)):
                out += [dict(o, file=f[0], file_name=f[1]) for o in part]
            return out
        out = scan()
        if kw["min_seconds"] and len(out) < min(MAX_ISSUE_LINES, n_calls):    # the estimate was too high: all of them
            kw["min_seconds"] = None
            out = scan()
        if slowest:
            out = sorted(out, key=lambda o: (-(o["seconds"] or 0.0), o["file"], o["line"]))
        return jsonify(out[:MAX_ISSUE_LINES])
    except NoData as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("op_lines failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500


@app.get("/api/issue_context")
def api_issue_context():
    """The raw lines around one occurrence found via /api/issue_lines or /api/op_lines (see read_context)."""
    fid = request.args.get("file")
    try:
        line = int(request.args.get("line", "0"))
        before = max(0, min(MAX_CONTEXT, int(request.args.get("before", "3"))))
        after = max(0, min(MAX_CONTEXT, int(request.args.get("after", "3"))))
    except ValueError:
        return jsonify({"error": "line, before and after must be whole numbers"}), 400
    if line < 1:
        return jsonify({"error": "line is required"}), 400
    with LOCK:
        f = FILES.get(fid)
    if f is None:
        return jsonify({"error": "unknown file"}), 404
    if not os.path.isfile(f["path"]):
        return jsonify({"error": "%s is no longer on disk (deleted since it was loaded): unload and load it again" % f["name"]}), 410
    try:
        piece = None                                         # start at the chunk holding the first line wanted, not the top
        for p in _pieces(fid, f["path"]):
            if p is not None and p[2] <= max(1, line - before):
                piece = (p[0], None, p[2])
        out = olp.read_context(f["path"], line, before, after, piece)
        by, name = request.args.get("by"), request.args.get("name")
        issue = _issue_key_arg(request.args.get("level"), request.args.get("logger"), request.args.get("msg"))
        if out["lines"]:                                     # what each line is to this occurrence, for the colors
            out["marks"] = olp.mark_context(out["lines"], out["match_at"], by if by in ("operation", "app", "family") else None,
                                            name, issue)
        return jsonify(out)
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("issue_context failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500


@app.get("/api/domain")
def api_domain_get():
    """The current global filter (the same arguments /api/analyze takes) and view (`view`: JSON, the shape
    domain_state() returns under "view") written as a domain (see FILTERS.md)."""
    try:
        try:
            view = json.loads(request.args.get("view") or "null")
        except ValueError:
            raise NoData("view must be JSON")
        flt, interval = _ff_arg(), _interval_arg()
        with LOCK:
            files = [FILES[i]["name"] for i in request.args.getlist("exclude_file") if i in FILES]
        dbs = [None if d == NONE_DB else d for d in request.args.getlist("exclude_db")]
        facts = [(by, name, {k: sorted(v) for k, v in want.items()})
                 for by, rows in (flt[1] or {}).items() if by != "extra" for name, want in rows.items()]
        return jsonify({"domain": olp.dom_render(files, dbs, interval, facts,
                                                 (flt[1] or {}).get("extra"), view if isinstance(view, dict) else None)})
    except (NoData, olp.DomainError) as e:
        return jsonify({"error": str(e)}), 400
    except (TypeError, KeyError, AttributeError, RecursionError) as e:       # a view of the wrong shape
        return jsonify({"error": "view: not the shape the filter editor sends (%s)" % e}), 400


def domain_state(text):
    """Domain text -> the app's filter state: {"exclude_file": [ids], "exclude_db": [names], "interval":
    [from | None, to | None] | None, "ff": [...], "xd": prefix list | None, "view": {items,
    total, bucket, fit, focus, timezone, table: {group, search, sort, hidden_columns}, errors: {level, search},
    panels_hidden}} (every view setting filled in: what the domain leaves out gets its default).
    Raises DomainError / NoData with a message for the user (or agent) to fix the domain."""
    parts = olp.dom_split(olp.dom_tree(olp.dom_parse_text(text)))
    with LOCK:
        files = [FILES[i] for i in ORDER]
        for f in files:
            _load(f)
        logs = [f for f in files if f.get("kind") != "summary" and f["usable"]]
        if not logs:
            raise NoData("no log file is loaded (an imported summary cannot be filtered)")

        def choose(conds, universe, what):
            """[(op, values)] over names -> the excluded names."""
            out = set()
            for op, vals in conds:
                unknown = [v for v in vals if v not in universe]
                if unknown and op in ("!=", "not in"):          # leaving out what is not there: nothing to do
                    vals = [v for v in vals if v in universe]
                elif unknown:
                    shown = ", ".join(json.dumps(u) for u in sorted(universe, key=lambda u: (u is None, str(u))))
                    raise NoData("no %s %s; the loaded ones are: %s" % (
                        what, ", ".join("false" if u is None else json.dumps(u) for u in unknown), shown.replace("null", "false")))
                out |= (set(universe) - set(vals)) if op in ("=", "in") else set(vals)
            return out

        names = {}
        for f in logs:
            names.setdefault(f["name"], []).append(f["id"])
        ex_names = choose(parts["file"], list(names), "file is named")
        ex_files = {i for n in ex_names for i in names[n]}
        if len(ex_files) >= len(logs):
            raise NoData("the domain leaves out every loaded file")
        _, _, dbs, _ = _select(set(), ex_files, False)
        universe = [None if d == NONE_DB else d for d, _ in dbs]
        dbvals = [(op, [None if v is False else v for v in vals]) for op, vals in parts["database"]]
        ex_dbs = choose(dbvals, universe, "database is named")
        if universe and len(ex_dbs) >= len(universe):
            raise NoData("the domain leaves out every database of the selected files")
    lo = max([v for op, v in parts["time"] if op == ">="] + [v + 1 for op, v in parts["time"] if op == ">"], default=None)
    hi = min([v for op, v in parts["time"] if op == "<"] + [v + 1 for op, v in parts["time"] if op == "<="], default=None)
    if lo is not None and hi is not None and lo >= hi:
        raise NoData("the time conditions leave no time at all")
    return {
        "exclude_file": sorted(ex_files),
        "exclude_db": sorted(NONE_DB if d is None else d for d in ex_dbs),
        "interval": None if lo is None and hi is None else [lo, hi],
        "ff": [{"by": by, "name": list(name) if by == "issue" else name, "facts": {k: sorted(v) for k, v in fs.items()}}
               for (by, name), fs in parts["facts"].items()],
        "xd": olp.dom_prefix(parts["extra"]) or None,
        "view": parts["view"],
    }


@app.post("/api/domain")
def api_domain_post():
    """Check a domain and turn it into the app's filter state (the client then analyzes with it)."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("domain"), str):
        return jsonify({"error": "send {\"domain\": \"...\"}"}), 400
    try:
        return jsonify({"state": domain_state(body["domain"])})
    except (olp.DomainError, NoData) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("domain failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500


@app.get("/api/analyze")
def api_analyze():
    try:
        _load_pending()
        _prepare_filter(_ff_arg())
        interval = _interval_arg()
        return jsonify(analyze(set(request.args.getlist("exclude_db")), set(request.args.getlist("exclude_file")),
                               interval, request.args.get("init") == "1", request.args.get("hist"),
                               request.args.get("rows"), _ff_arg(), _pins_arg()))
    except NoData as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("analysis failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500


@app.get("/api/export")
def api_export():
    try:
        _prepare_filter(_ff_arg())
        data = export(set(request.args.getlist("exclude_db")), set(request.args.getlist("exclude_file")),
                      _interval_arg(), _ff_arg())
    except NoData as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("export failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500
    resp = Response(json.dumps(data, indent=2, ensure_ascii=False), mimetype="application/json")
    resp.headers["Content-Disposition"] = 'attachment; filename="logtool-export-%s.json"' % \
        datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return resp


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #

def main(argv=None):
    # Before anything else: point every temp file this process ever creates -- ours (UPLOAD_DIR) and
    # Werkzeug's own internal buffering of an incoming upload alike -- away from the OS default temp
    # directory (see _pick_tmp_dir for why).
    multiprocessing.util.get_temp_dir()    # the worker pool's sockets: keep them in the OS temp dir, not below
    tmp = _pick_tmp_dir()
    if tmp:
        tempfile.tempdir = tmp

    ap = argparse.ArgumentParser(prog="logtool", description="Web UI for the Odoo log profiler.")
    ap.add_argument("logs", nargs="*", help="log files to preload")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to listen on (default 127.0.0.1; anything else exposes the loaded logs)")
    ap.add_argument("--port", type=int, default=5000, help="port to listen on (default 5000)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    ap.add_argument("--db", metavar="NAME", help="read only this database's log lines (and those without one); "
                                                 "the others are skipped unparsed (can be changed on the page)")
    a = ap.parse_args(argv)
    set_only_db(a.db)

    for path in a.logs:
        if not os.path.isfile(path):
            ap.error("no such file: %s" % path)
        _add_file(os.path.abspath(path), os.path.basename(path), False)

    app.config["LOOPBACK_ONLY"] = a.host in LOOPBACK
    url = "http://%s:%d" % (a.host if ":" not in a.host else "[%s]" % a.host, a.port)
    print("Odoo log profiler UI on", url)
    if a.host not in LOOPBACK:
        print("WARNING: listening on %s: anyone who can reach this machine can read the loaded logs." % a.host)
    threading.Thread(target=olp.warm_pool, daemon=True).start()      # worker processes take a while to start
    if not a.no_browser and (sys.platform != "linux" or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()      # no desktop session (e.g. ssh): just print the URL
    def stop(signum, frame):        # kill / a closed terminal: clean up like Ctrl+C (workers, uploaded files)
        raise KeyboardInterrupt
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)
    try:
        # make_server (not app.run) so werkzeug prints no dev-server warning or access log
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        make_server(a.host, a.port, app, threaded=True).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        olp.shutdown_pool()
        if UPLOAD_DIR:
            shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

