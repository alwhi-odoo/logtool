"""One slow query's form, what ran while it did, and the slow queries that ran while an operation did.
"""

from __future__ import annotations

import bisect
import sys

from . import state
from .. import profiler as olp
from .details import _group_ops
from .durations import DUR_EDGES, _bin_add, _bins
from .selection import _base, _carried, _check_on_disk, _pieces, _select
from .slowq import _slow_queries, _sq_get, _sq_head, _sq_shape_text
from .state import LOCK, MAX_ISSUE_LINES, NOFILT
from .util import _stamp_secs


def slowq_detail(qid, exclude_db, exclude_file, interval, flt=NOFILT):
    """One slow query's form: the query, its SQL in full, and the runs of its shape in the selection ("same":
    their count, time, durations histogram and the MAX_ISSUE_LINES longest of them, in time order)."""
    with LOCK:
        _, _, _, exclude_db = _select(exclude_db, exclude_file, False)
        q = _sq_get(qid)
        same = [(k, x) for k, x in _slow_queries(exclude_db, exclude_file, interval, flt) if x.shape == q.shape]
        text = _sq_shape_text(q.shape)
    secs = sorted(x.seconds for _, x in same)
    counts = [0.0] * (len(DUR_EDGES) + 1)
    _bin_add(counts, secs)
    shown = sorted(sorted(same, key=lambda kx: -kx[1].seconds)[:MAX_ISSUE_LINES], key=lambda kx: kx[1].end)
    return {"id": qid, "t": q.end, "db": q.db, "s": q.seconds, "sql": q.sql, "cut": q.cut, "shape": q.shape,
            "shape_text": text,
            "same": {"n": len(same), "total_s": sum(secs), "max_s": secs[-1] if secs else 0,
                     "first": same[0][1].end if same else None, "last": same[-1][1].end if same else None,
                     "durations": {"bins": _bins(counts), "estimated": False},
                     "items": [{"id": k, "t": x.end, "db": x.db, "s": x.seconds} for k, x in shown]}}


def slowq_running(qid, exclude_db, exclude_file, interval, flt=NOFILT):
    """The requests and jobs that ran while one slow query did (their span overlaps its, to the second), in its
    database, re-found by re-reading the loaded logs: {"n", "items": the MAX_ISSUE_LINES longest, each
    {"operation", "family", "stamp" (when it ended), "seconds"}}."""
    with LOCK:
        _, inc, dbs, exclude_db = _select(exclude_db, exclude_file, False)
        q = _sq_get(qid)
        files = [(f["id"], f["name"], f["path"]) for f in inc]
        excl = set(exclude_db) | ({d for d, _ in dbs} - {q.db} if q.db else set())
        prof = _base(tuple(f["id"] for f in inc), frozenset(exclude_db), flt)
        longest = max((b.mx for b in prof.det.values()), default=0.0)    # a request ending this late may still overlap
    start = q.end - q.seconds
    lo, hi = int(start), int(q.end + longest) + 2
    _check_on_disk(files)
    kw = dict(by="operation", name=None, exclude_db=excl, lo=lo, hi=hi, limit=sys.maxsize, ff=flt[1], only_db=state.ONLY_DB,
              carried=_carried())
    tasks = [(f[2], piece) for f in files for piece in _pieces(f[0], f[2], lo, hi)]
    out = []
    for part in olp.scan_pieces("op", tasks, kw):
        for o in part:
            end = _stamp_secs(o["stamp"])
            if o["seconds"] is not None and end - o["seconds"] <= q.end and end + 1 > start:   # stamps are to the second
                out.append(o)
    out.sort(key=lambda o: -o["seconds"])
    return {"n": len(out), "items": [{"operation": o["operation"], "family": o["family"], "stamp": o["stamp"],
                                      "seconds": o["seconds"]} for o in out[:MAX_ISSUE_LINES]]}


def op_slowq(by, name, exclude_db, exclude_file, interval, flt=NOFILT):
    """The slow queries that ran while the calls of one operation / app / type did (their spans overlap, to the
    second, in the same database), its calls re-found by re-reading the loaded logs around them, one item per shape:
    {"n" (shapes), "runs", "items": the MAX_ISSUE_LINES shapes with the most time, each {"shape", "id" (its longest
    such run), "n" (runs), "total_s", "max_s", "last", "dbs", "head" (its shape's text), "calls" (how many of the
    calls they overlap, added up)}}."""
    with LOCK:
        _, inc, _, exclude_db = _select(exclude_db, exclude_file, False)
        slow = _slow_queries(exclude_db, exclude_file, interval, flt)
        files = [(f["id"], f["name"], f["path"]) for f in inc]
        prof = _base(tuple(f["id"] for f in inc), frozenset(exclude_db), flt)
        longest = max((prof.det[op].mx for op in _group_ops(prof, by, name)), default=0.0)
    if not slow or not files:
        return {"n": 0, "runs": 0, "items": []}
    lo, hi = int(min(q.end - q.seconds for _, q in slow)), int(slow[-1][1].end + longest) + 2
    if interval:
        lo, hi = max(lo, interval[0]), min(hi, interval[1])
    _check_on_disk(files)
    kw = dict(by=by, name=name, exclude_db=exclude_db, lo=lo, hi=hi, limit=sys.maxsize, ff=flt[1], only_db=state.ONLY_DB,
              carried=_carried())
    tasks = [(f[2], piece) for f in files for piece in _pieces(f[0], f[2], lo, hi)]
    calls = sorted((_stamp_secs(o["stamp"]), o["seconds"], o["db"]) for part in olp.scan_pieces("op", tasks, kw)
                   for o in part if o["seconds"] is not None)
    ends = [c[0] for c in calls]
    per, runs = {}, 0
    for k, q in slow:
        start = q.end - q.seconds                  # a call overlapping it ends after it started, and by q.end + longest
        n = sum(1 for end, secs, db in calls[bisect.bisect_left(ends, int(start)):bisect.bisect_right(ends, q.end + longest + 1)]
                if end - secs <= q.end and end + 1 > start and (q.db is None or db == q.db))
        if not n:
            continue
        runs += 1
        e = per.get(q.shape)
        if e is None:
            e = per[q.shape] = {"shape": q.shape, "id": k, "n": 0, "total_s": 0.0, "max_s": q.seconds, "dbs": set(), "calls": 0}
        elif q.seconds > e["max_s"]:
            e["id"], e["max_s"] = k, q.seconds
        e["n"] += 1
        e["total_s"] += q.seconds
        e["last"] = q.end
        e["dbs"].add(q.db)
        e["calls"] += n
    items = sorted(per.values(), key=lambda e: -e["total_s"])[:MAX_ISSUE_LINES]
    return {"n": len(per), "runs": runs, "items": [dict(e, dbs=sorted(e["dbs"], key=lambda d: d or ""),
                                                        head=_sq_head(_sq_shape_text(e["shape"]))) for e in items]}
