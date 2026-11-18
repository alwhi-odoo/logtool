"""Loading files: recognising what each one is (Odoo log, slow-query log, exported summary), decompressing,
parsing it once into per-database chunks.
"""

from __future__ import annotations

import bz2
import gzip
import json
import os
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import state
from . import storage
from .. import profiler as olp
from .parse_cache import _cache_load, _cache_save, _parse_key
from .state import CHUNKS, FILES, INDEX, LOAD_LOCK, LOCK, ORDER, SLOWQ, SUMMARIES, _args
from .storage import _free_bytes, _remove_file, _upload_dir


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
    can only chunk and byte-seek into a plain file (see Chunk in parser/parallel.py); a
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
    free = _free_bytes(storage.UPLOAD_DIR)
    if estimate is not None and free is not None and estimate > free - reserved - DECOMPRESS_MARGIN:
        return None
    out = os.path.join(storage.UPLOAD_DIR, uuid.uuid4().hex[:10] + ".log")
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


def _loaded(fid):
    return fid in CHUNKS or fid in SUMMARIES or fid in SLOWQ


def _load(f):
    """Parse one file once (see _load_batch)."""
    if not _loaded(f["id"]):
        _load_batch([f])


def _load_pending():
    """Parse the files added since the last request -- all of them in one go, before taking LOCK, so the file
    list, popups and other requests keep being answered while big logs are read."""
    with LOCK:
        todo = [FILES[i] for i in ORDER if not _loaded(i)]
    if todo:
        _load_batch(todo)
    with LOCK:                                      # unloaded while it was being read: drop what was parsed
        for cache in (CHUNKS, INDEX, SUMMARIES, SLOWQ):
            for fid in [i for i in cache if i not in FILES]:
                del cache[fid]


def _load_batch(files):
    """Parse files once: Odoo logs (their per-database profiles and minute index), PostgreSQL slow-query logs
    (see olp.sq_parse, recognised by their name or first lines) or previously exported summaries (see export(),
    recognised by their .json extension). Many files at once go the fast way: every
    compressed one is decompressed at the same time (zlib / bz2 release the GIL, so threads use every core),
    then all of them are parsed in one profile_files() call, so their chunks share the worker pool instead of
    one file after the other."""
    with LOAD_LOCK:                                 # one load at a time; another thread may have done these meanwhile
        files = [f for f in files if not _loaded(f["id"])]
        logs = []
        for f in files:
            if f["name"].lower().endswith(".json"):
                summary = _try_parse_summary(f["path"])
                f["kind"], f["usable"] = "summary", summary is not None
                f["problem"] = None if summary is not None else "not a logtool export"
                SUMMARIES[f["id"]] = summary
            elif olp.sq_sniff(f["path"], f["name"]):
                f["kind"], err = "slowq", None
                try:
                    entries, shapes = olp.sq_parse(f["path"], state.ONLY_DB)
                except olp.READ_ERRORS as e:
                    entries, shapes, err = [], {}, "could not be read (%s)" % e
                SLOWQ[f["id"]] = {"entries": entries, "shapes": shapes}
                f["usable"] = bool(entries)
                f["problem"] = err or (None if entries else "no slow queries recognised" if state.ONLY_DB is None
                                       else "no slow queries of database %s (or without one)" % state.ONLY_DB)
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
            f["problem"] = err or (None if f["usable"] else "no Odoo log lines recognised" if state.ONLY_DB is None
                                   else "no log lines of database %s (or without one)" % state.ONLY_DB)


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
