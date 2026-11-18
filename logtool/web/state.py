"""Everything the web app keeps in memory: the loaded files, their parsed chunks, the caches, the locks, the limits.

Two kinds of names live here, and they are used differently:

* containers and locks (FILES, ORDER, CHUNKS, the caches, LOCK, ...) are only ever changed in place, so other
  modules import them by name;
* ONLY_DB and ONLY_GEN are rebound by `set_only_db()`, so other modules must read them as `state.ONLY_DB` /
  `state.ONLY_GEN` (a `from .state import ONLY_DB` would keep the value it had at import time).
"""

from __future__ import annotations

import argparse
import threading
import uuid
from collections import OrderedDict


MAX_ROWS = 5000         # operations sent to the UI per view (the table must not hide the long tail)
MAX_HIST_OPS = 500      # operations that also get their own timeline (for the histogram's search tags);
                        # a search tag on a busier operation than this just won't have a line -- the top
                        # 500 by time already covers what anyone would realistically want to compare
MAX_ISSUES = 60         # distinct warning / error messages sent to the UI
MAX_SQ_ROWS = 5000      # slow-query shapes sent to the UI (those with the most time)
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
SLOWQ = {}              # file id -> {"entries": [olp.SlowQuery], "shapes": {shape id: text}}: a slow-query log, parsed
LOCK = threading.RLock()
LOAD_LOCK = threading.RLock()   # parsing a new file: held without LOCK, so other requests keep being answered meanwhile
DIR_LOCK = threading.Lock()
FILTER_LOCK = threading.Lock()   # _prepare_filter: held without LOCK (LOCK is only ever taken inside it, never around it)
ONLY_DB = None          # read only this database's log lines (and those without one); None: every database (see set_only_db)
ONLY_GEN = 0            # bumped by set_only_db: a parse that started under an older setting must not be cached
PAIRS = [None, None]    # [key, pairs]: the cron runs killed in another chunk or file than they started in (_cron_pairs)
NOFILT = (None, None)   # no fact filter: (cache key, args.fact_filter)
BOOT_ID = uuid.uuid4().hex[:8]   # this run of the server: settings that should not outlive it are stored under it


class NoData(Exception):
    """The request cannot be answered from what is loaded; the message is meant for the user."""


def _args(fact_filter=None):
    """Same defaults the CLI would give, minus every output option."""
    return argparse.Namespace(top=MAX_ROWS, by="detail", min_calls=1, min_total=0.0, fold=1,
                              events=False, event_top=6, logins=False, hours=False,
                              unclassified=False, all=False, since=None, until=None,
                              db=None, grep=None, csv=None, json=None, out=None, split_db=True,
                              fact_filter=fact_filter, only_db=ONLY_DB)


def _forget_selections():
    """Drop every cached merge (they can hold a lot of memory and belong to files that are going away)."""
    for cache in (FULL, BASE, CUT_BASE, CUTS, FCHUNKS, TIMELINES):
        cache.clear()


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
        for fid in list(SLOWQ):                     # read again too, with the new database
            del SLOWQ[fid]
        SLICES.clear()
        FCHUNKS.clear()
        _forget_selections()
    return True
