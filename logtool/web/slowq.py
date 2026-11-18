"""The slow queries of the selection: which ones the filters keep, and how the table and the exports show them.
"""

from __future__ import annotations

from .. import profiler as olp
from .state import FILES, MAX_SQ_ROWS, NOFILT, NONE_DB, NoData, ORDER, SLOWQ
from .util import _iso


def _sq_files(exclude_file):
    """The selected slow-query logs."""
    return [FILES[i] for i in ORDER if FILES[i].get("kind") == "slowq" and FILES[i].get("usable") and i not in exclude_file]


def _sq_keep(flt):
    """A predicate: is this olp.SlowQuery kept by the filters? The domain's custom conditions see it as kind
    "slow_query" (see olp.sq_fields); its ip keeps none (a slow query has no client). Item filters narrow rows of
    the other tables, so they leave slow queries alone."""
    ff = flt[1] or {}
    if ff.get("client"):
        return lambda q: False
    xp = olp.dom_compile(ff["extra"]) if ff.get("extra") else None
    return (lambda q: True) if xp is None else (lambda q: xp(olp.sq_fields(q)))


def _slow_queries(exclude_db, exclude_file, interval=None, flt=NOFILT):
    """The slow queries of the selection (logged in the interval, seconds [from, to)), in time order:
    [(id, olp.SlowQuery)], the id being "<file id>:<index in it>"."""
    keep, (lo, hi) = _sq_keep(flt), interval or (None, None)
    out = []
    for f in _sq_files(exclude_file):
        for i, q in enumerate(SLOWQ[f["id"]]["entries"]):
            if (q.db or NONE_DB) not in exclude_db and (lo is None or lo <= q.end < hi) and keep(q):
                out.append(("%s:%d" % (f["id"], i), q))
    out.sort(key=lambda x: x[1].end)
    return out


def _sq_get(qid):
    """One loaded slow query by id (see _slow_queries)."""
    fid, _, i = (qid or "").partition(":")
    try:
        return SLOWQ[fid]["entries"][int(i)]
    except (KeyError, ValueError, IndexError):
        raise NoData("this slow query is not loaded any more")


def _sq_shape_text(sid):
    for sq in SLOWQ.values():
        if sid in sq["shapes"]:
            return sq["shapes"][sid]
    return ""


def _sq_head(sql):
    """The first characters of a query, on one line (the table shows them)."""
    return " ".join(sql[:400].split())[:200]


def _sq_view(qs):
    """The slow queries of the selection, as the table and the statistic show them: {"rows": one per shape (the
    MAX_SQ_ROWS with the most time), each {"shape", "id" (its longest run), "n" (runs), "total_s", "avg_s", "max_s",
    "last" (when the last one was logged, seconds), "dbs" (their databases), "head" (its shape's text, on one line),
    "runs": [[logged at, seconds], ...] in time order}, "n" (runs), "total_s", "max_s", "shapes", "labels": {shape:
    text} of the rows}."""
    per = {}
    for k, q in qs:
        e = per.get(q.shape)
        if e is None:
            e = per[q.shape] = {"shape": q.shape, "id": k, "s": q.seconds, "dbs": set(), "runs": []}
        elif q.seconds > e["s"]:
            e["id"], e["s"] = k, q.seconds
        e["dbs"].add(q.db)
        e["runs"].append([round(q.end, 3), q.seconds])
    rows = []
    for e in sorted(per.values(), key=lambda e: -sum(s for _, s in e["runs"]))[:MAX_SQ_ROWS]:
        total = sum(s for _, s in e["runs"])
        rows.append({"shape": e["shape"], "id": e["id"], "n": len(e["runs"]), "total_s": total, "avg_s": total / len(e["runs"]),
                     "max_s": e.pop("s"), "last": e["runs"][-1][0], "dbs": sorted(e["dbs"], key=lambda d: d or ""),
                     "head": _sq_head(_sq_shape_text(e["shape"])), "runs": e["runs"]})
    return {"rows": rows, "n": len(qs), "total_s": sum(q.seconds for _, q in qs),
            "max_s": max((q.seconds for _, q in qs), default=0), "shapes": len(per),
            "labels": {r["shape"]: _sq_shape_text(r["shape"])[:300] for r in rows}}


SQ_EXPORT_TOP = 100     # shapes and runs of the slow queries an export lists


def _sq_export(qs):
    """The slow queries for export(): numbers and shape ids only, no SQL and no database (see EXPORT_FIELDS)."""
    per = {}
    for _, q in qs:
        e = per.setdefault(q.shape, {"shape": q.shape, "n": 0, "total_s": 0.0, "max_s": 0.0, "first": q.end, "last": q.end})
        e["n"] += 1
        e["total_s"] += q.seconds
        e["max_s"] = max(e["max_s"], q.seconds)
        e["last"] = q.end
    stamp = lambda t: _iso(t) + ".%03d" % int(t % 1 * 1000)   # noqa: E731
    return {"n": len(qs), "total_s": sum(q.seconds for _, q in qs), "max_s": max((q.seconds for _, q in qs), default=0),
            "shapes": len(per),
            "by_shape": [dict(e, first=stamp(e["first"]), last=stamp(e["last"]))
                         for e in sorted(per.values(), key=lambda e: -e["total_s"])[:SQ_EXPORT_TOP]],
            "longest": [{"t": stamp(q.end), "s": q.seconds, "shape": q.shape}
                        for _, q in sorted(qs, key=lambda kq: -kq[1].seconds)[:SQ_EXPORT_TOP]]}
