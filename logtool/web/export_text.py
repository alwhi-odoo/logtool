"""Export text: the selection as a plain-text report, with every occurrence's raw log lines.
"""

from __future__ import annotations

import sys

from . import state
from .. import profiler as olp
from .analysis import analyze
from .details import _facts_view
from .results import NICE_MIN, _rebin, _timeline_base
from .selection import _carried, _check_on_disk, _heavy, _pieces, _prof_for, _select
from .state import LOCK, NOFILT, NONE_DB, NoData
from .util import _iso


TEXT_BARS = 60          # Export text: at most this many histogram buckets (one line each), ...
TEXT_BAR_W = 50         # ... the longest bar, in characters ...
TEXT_AROUND = 3         # ... and the log lines shown before and after each occurrence
TEXT_RULE = "=" * 100


def _text_minutes(m):
    return "%d min" % m if m < 60 else "%d h" % (m // 60) if m < 1440 else "%d d" % (m // 1440)


def export_text(exclude_db, exclude_file, interval, flt=NOFILT):
    """Export text: the selection as a plain-text report, for one database and the lines without one. The requests
    histogram, every operation (name, calls, average ms) with the details behind it but its client IPs, then every
    occurrence's log line with the TEXT_AROUND lines before and after it, oldest first -- the database's own lines
    only, not those without one. Raises NoData when more than one database is selected."""
    a = analyze(exclude_db, exclude_file, interval, False, flt=flt)
    if a.get("imported"):
        raise NoData("an imported summary has no log lines to export as text")
    with LOCK:
        _, inc, dbs, excl = _select(set(a["excluded_db"]), exclude_file, False)
        keep = {d for d, _ in dbs} - set(excl)
        if len(keep - {NONE_DB}) > 1:
            raise NoData("Export text holds one database (and the lines without one): select only one of %s"
                         % ", ".join(sorted(keep - {NONE_DB})))
        ids, excl = tuple(f["id"] for f in inc), frozenset(excl)
        iv = tuple(a["interval"]) if a["interval"] else None
        prof = _heavy(_prof_for(inc, ids, excl, iv, flt))
        prof0 = _heavy(_prof_for(inc, ids, excl, iv)) if flt[0] else prof
        rows = [(r, _facts_view(prof, prof0, "operation", r["operation"], flt)) for r in prof.as_rows(("operation",))]
        per_min = _timeline_base(prof)["total"]
        files = [(f["id"], f["name"], f["path"]) for f in inc]
    out = []
    w = out.append

    # the requests histogram: one line per bucket, empty ones included
    if per_min:
        lo_m, hi_m = min(per_min), max(per_min)
        base = next((n for n in NICE_MIN if (hi_m - lo_m + 1) / n <= TEXT_BARS), NICE_MIN[-1])
        bins = {b[0]: b[1] for b in _rebin(per_min, base)}
        top = max(bins.values()) or 1
        w("REQUESTS OVER TIME (%s buckets, UTC as logged)\n\n" % _text_minutes(base))
        for m in range(lo_m // base * base, hi_m + 1, base):
            n = bins.get(m, 0)
            bar = "#" * (max(1, round(TEXT_BAR_W * n / top)) if n else 0)
            w("%s  %-*s %s\n" % (_iso(m * 60)[:16].replace("T", " "), TEXT_BAR_W, bar, format(n, ",")))
    else:
        w("REQUESTS OVER TIME\n\nNo requests in the selection.\n")
    w("\n%s\n\n" % TEXT_RULE)

    # every operation, with the details behind the grouping (the values its operation name collapses)
    w("OPERATIONS (%s, by total time)\n\n" % format(len(rows), ","))
    width = min(80, max([len(r["operation"]) for r, _ in rows] + [9]))
    w("%-*s %10s %12s\n" % (width, "operation", "calls", "avg ms"))
    for r, facts in rows:
        w("\n%-*s %10s %12s\n" % (width, r["operation"], format(r["calls"], ","), format(round(r["avg_ms"]), ",")))
        for f in facts:
            if f["detail"] == olp.F_IP:                     # not shown (it still filters, like every detail)
                continue
            w("    %s (%s%s distinct, %s in all)\n" % (f["detail"], format(f["distinct"], ","), "+" if f["other_n"] else "",
                                                     format(f["total"], ",")))
            vw = min(60, max([len(v[0]) for v in f["values"]] + [5]))
            w("        %-*s %10s %7s %9s %10s%s\n" % (vw, "value", "count", "share", "of calls", "avg ms",
                                                  "  accounts logged in from it" if any(v[2:] and v[2] for v in f["values"]) else ""))
            for k, v in enumerate(f["values"]):
                avg = (f.get("avg_ms") or [None] * (k + 1))[k]
                w("        %-*s %10s %6.1f%% %8.1f%% %10s%s\n" % (
                    vw, v[0], format(v[1], ","), 100.0 * v[1] / f["total"] if f["total"] else 0,
                    100.0 * v[1] / r["calls"] if r["calls"] else 0, "" if avg is None else format(round(avg), ","),
                    "  " + ", ".join(v[2]) if v[2:] and v[2] else ""))
            if f["distinct"] > len(f["values"]):
                w("        ...and %s more values not kept\n" % format(f["distinct"] - len(f["values"]), ","))
            if f["other_n"]:
                w("        %s more not told apart (too many distinct values)\n" % format(f["other_n"], ","))
    w("\n%s\n\n" % TEXT_RULE)

    # the slow queries, one line per shape, most time first: how often, how long (their SQL is not written out)
    slow = a["slowq"]
    if slow["n"]:
        w("SLOW QUERIES (%s runs of %s shapes, %s s in all, by total time%s)\n\n" % (
            format(slow["n"], ","), format(slow["shapes"], ","), format(round(slow["total_s"]), ","),
            "" if slow["shapes"] <= len(slow["rows"]) else "; the first %s" % format(len(slow["rows"]), ",")))
        w("%-12s %8s %12s %10s %10s  %s\n" % ("shape", "runs", "total s", "avg s", "max s", "last logged (UTC)"))
        for r in slow["rows"]:
            w("%-12s %8s %12s %10s %10s  %s\n" % (r["shape"], format(r["n"], ","), format(r["total_s"], ",.1f"),
                                                format(r["avg_s"], ",.1f"), format(r["max_s"], ",.1f"),
                                                _iso(r["last"]).replace("T", " ") + ".%03d" % int(r["last"] % 1 * 1000)))
        w("\n%s\n\n" % TEXT_RULE)

    # every occurrence's own line, with the lines around it, oldest first
    lo, hi = interval if interval else (None, None)
    _check_on_disk(files)
    kw = dict(by="operation", name=None, exclude_db=excl, lo=lo, hi=hi, limit=sys.maxsize, ff=flt[1], only_db=state.ONLY_DB,
              carried=_carried())
    tasks = [(f, piece) for f in files for piece in _pieces(f[0], f[2], lo, hi)]
    occ = []
    for (f, _piece), part in zip(tasks, olp.scan_pieces("op", [(f[2], p) for f, p in tasks], kw)):
        occ += [dict(o, file=f[0]) for o in part]
    ctx = {}
    for fid, _name, path in files:
        lines = [o["line"] for o in occ if o["file"] == fid]
        if lines:
            ctx[fid] = olp.occurrence_context(path, lines, keep - {NONE_DB}, TEXT_AROUND)
    occ = [o for o in occ if o["line"] in ctx.get(o["file"], ())]   # those of lines without a database: left out
    order = {f[0]: k for k, f in enumerate(files)}
    occ.sort(key=lambda o: (o["stamp"], order[o["file"]], o["line"]))
    w("RAW LOG LINES (%s occurrence%s, oldest first; > marks the occurrence)\n" % (format(len(occ), ","), "" if len(occ) == 1 else "s"))
    for k, o in enumerate(occ, 1):
        w("\n\n[%s] %s  %s%s\n" % (format(k, ","), o["stamp"].replace("T", " "), o["operation"],
                                     "" if o["seconds"] is None else "  (%.3f s)" % o["seconds"]))
        for i, line in ctx.get(o["file"], {}).get(o["line"], ()):
            w("%s %s\n" % (">" if i == o["line"] else " ", line))
    return "".join(out)
