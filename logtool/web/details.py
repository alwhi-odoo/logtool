"""What the details popup of one row shows: example lines, untimed lines, facts, durations, requests during a cron.
"""

from __future__ import annotations

import bisect
import sys

from . import state
from .. import profiler as olp
from .durations import DUR_EDGES, _bin_add, _bins, _durations
from .results import _facts_json, _ip_logins, _span
from .selection import _carried, _check_on_disk, _heavy, _pieces, _prof_for, _select
from .state import LOCK, NOFILT
from .util import _stamp_secs


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


def request_durations(exclude_db, exclude_file, interval, flt=NOFILT):
    """How long the requests of the selection took (the "Request time" statistic's histogram): see _durations."""
    with LOCK:
        _, inc, _, exclude_db = _select(exclude_db, exclude_file, False)
        ids, excl = tuple(f["id"] for f in inc), frozenset(exclude_db)
        prof = _heavy(_prof_for(inc, ids, excl, interval, flt))
        return {"durations": _durations([b for op, b in prof.det.items()
                                         if prof.det_fam.get(op) not in olp.JOB_KINDS])}


def cron_requests(name, exclude_db, exclude_file, interval, flt=NOFILT):
    """The requests that ran while cron operation `name` was running, re-found by re-reading the loaded files: a
    request counts when its span (its end, less its duration) overlaps a run's, to the second. {"runs", "requests",
    "avg_ms" (None: none), "durations" (see _durations)}."""
    with LOCK:
        _, inc, _, exclude_db = _select(exclude_db, exclude_file, False)
        files = [(f["id"], f["name"], f["path"]) for f in inc]
    lo, hi = interval if interval else (None, None)
    _check_on_disk(files)
    kw = dict(by="operation", name=None, exclude_db=exclude_db, lo=lo, hi=hi, limit=sys.maxsize, ff=flt[1],
              only_db=state.ONLY_DB, carried=_carried())
    tasks = [(f[2], piece) for f in files for piece in _pieces(f[0], f[2], lo, hi)]
    runs, reqs = [], []
    for part in olp.scan_pieces("op", tasks, kw):
        for o in part:
            if o["seconds"] is None:
                continue
            end = _stamp_secs(o["stamp"])
            if o["operation"] == name:
                runs.append((end - o["seconds"], end))
            elif o["family"] not in olp.JOB_KINDS:
                reqs.append((end - o["seconds"], end, o["seconds"]))
    merged = []                                    # the runs' spans, overlapping ones joined: [(start, end)] by start
    for a, b in sorted(runs):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    starts = [m[0] for m in merged]
    during = []
    for a, b, secs in reqs:
        k = bisect.bisect_right(starts, b) - 1     # the last run starting by the request's end
        if k >= 0 and merged[k][1] >= a:
            during.append(secs)
    during.sort()
    counts = [0.0] * (len(DUR_EDGES) + 1)
    _bin_add(counts, during)
    return {"runs": len(runs), "requests": len(during),
            "avg_ms": round(1000 * sum(during) / len(during), 3) if during else None,
            "durations": {"bins": _bins(counts), "estimated": False}}


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
                "durations": _durations([b]),
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
            "durations": _durations([prof.det[op] for op in ops]),
            "facts": facts, "logged_facts": logged, "calls": calls,
            "first": first, "last": last, "active_minutes": active}


def detail(by, name, exclude_db, exclude_file, interval, flt=NOFILT):
    """The popup data for one row of the operation / app / type table, for the same selection the table shows."""
    with LOCK:
        _, inc, _, exclude_db = _select(exclude_db, exclude_file, False)
        ids, excl = tuple(f["id"] for f in inc), frozenset(exclude_db)
        prof = _heavy(_prof_for(inc, ids, excl, interval, flt))
        prof0 = _heavy(_prof_for(inc, ids, excl, interval)) if flt[0] else prof
        d = _detail(prof, by, name, _facts_view(prof, prof0, by, name, flt),
                    _facts_view(prof, prof0, by, name, flt, logged=True))
        if d["found"] and olp.F_DUR in ((flt[1] or {}).get(by, {}).get(name) or {}):
            # filtered on its durations: every call's, from the unfiltered selection, so more spans can be picked
            d["durations"] = dict(_durations([prof0.det[op] for op in _group_ops(prof0, by, name)]), unfiltered=True)
        return d


def issue_detail(key, exclude_db, exclude_file, interval, flt=NOFILT):
    """Every kept detail behind one warning / error (the analysis only sends the most frequent few)."""
    with LOCK:
        _, inc, _, exclude_db = _select(exclude_db, exclude_file, False)
        ids, excl = tuple(f["id"] for f in inc), frozenset(exclude_db)
        prof = _prof_for(inc, ids, excl, interval, flt)
        prof0 = _prof_for(inc, ids, excl, interval) if flt[0] else prof
        return {"found": key in prof.issues, "facts": _facts_view(prof, prof0, "issue", key, flt)}
