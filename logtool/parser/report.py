"""The text report of `logtool-report` and the machine-readable rows (CSV / JSON, the web app's tables).

`Profile.report()` and `Profile.as_rows()` call `write_report()` and `as_rows()`; `verdict()` names a row's bottleneck.
"""

from __future__ import annotations

from .bucket import Bucket
from .jobs import JOB_KINDS


SLOW_QUERY_MS = 20.0        # above this, an individual query is doing real work
CHATTY_QUERIES = 60.0       # above this many queries per call, batching is the issue


def verdict(sqlpct, queries_per_call, ms_per_query):
    """Where the time is, and therefore which kind of fix can possibly help.

    The deciding number is milliseconds of SQL per query, not the SQL percentage.
    A call doing 200 fast queries and a call doing 3 slow ones both look "mostly
    SQL", but only the second can be fixed with an index.
    """
    if sqlpct is None:
        return "job"                         # logged as a message: total time only, no SQL/Python split
    if sqlpct < 35:
        return "python"                      # computes, automations, rendering, hashing
    if ms_per_query >= SLOW_QUERY_MS:
        return "slow-queries"                # index / plan / domain candidate
    if queries_per_call >= CHATTY_QUERIES:
        return "too-many-qry"                # N+1: batch it, an index will not save you
    return "mixed"


def write_report(prof, out):
    """The text report of one (finalized) Profile, written to `out` (Profile.report)."""
    a = prof.a
    w = out.write
    tot = sum(b.total for b in prof.fam.values()) or 1e-9
    calls = sum(b.n for k, b in prof.fam.items() if k not in JOB_KINDS)
    jobs = [(k, b) for k, b in prof.fam.items() if k in JOB_KINDS]

    w("=" * 118 + "\n")
    w("ODOO LOG PROFILE\n")
    w("=" * 118 + "\n")
    w("window            : %s  ->  %s\n" % (prof.t_min or "?", prof.t_max or "?"))
    w("lines read        : %-12d  header matched: %d   continuation/traceback: %d   skipped by filters: %d\n"
      % (prof.lines, prof.header_ok, prof.continuation, prof.filtered))
    w("header formats    : %s\n" % (", ".join("%s=%d" % kv for kv in prof.header_hits.most_common()) or "-"))
    if prof.formats:
        w("log variants seen : %s\n" % ", ".join("%s=%d" % kv for kv in prof.formats.most_common()))
    if prof.held_n:
        w("held connections  : %d WebSocket/long-poll connections were held open for %.0f s in total "
          "(waiting, not counted as processing time)\n" % (prof.held_n, prof.held_s))
    w("requests          : %-12d  with timing: %d   without timing: %d   unparsable: %d\n"
      % (calls, prof.access_ok, prof.access_untimed, prof.access_bad))
    if prof.access_untimed:
        w("   note: %d access lines carry no query/time triplet (older Odoo); they count as 0 s.\n"
          % prof.access_untimed)
    sql = sum(b.sql for b in prof.fam.values())
    py = sum(b.py for b in prof.fam.values())
    nq = sum(b.nq for b in prof.fam.values())
    req_t = sum(b.total for k, b in prof.fam.items() if k not in JOB_KINDS) or 1e-9
    w("total request time: %.0f s (%.2f h)   SQL %.0f s (%.1f%%)   Python %.0f s (%.1f%%)   queries %d\n"
      % (req_t, req_t / 3600, sql, 100 * sql / req_t, py, 100 * py / req_t, nq))
    if jobs:
        w("background jobs   : %s   (cron / automation / module loading: total time only, no SQL split)\n"
          % ", ".join("%s %d runs %.0f s" % (k, b.n, b.total) for k, b in sorted(jobs)))
    w("http status       : %s\n" % ", ".join("%s=%d" % kv for kv in prof.status.most_common(8)))
    unk = prof.fam.get("other")
    if unk:
        w("unclassified      : %d requests, %.0f s (%.1f%% of time) - see --unclassified\n"
          % (unk.n, unk.total, 100 * unk.total / tot))
    w("\n")

    hdr = ("%9s %6s %6s %6s %8s %9s %7s %7s %8s %-11s %s\n" % (
        "total_s", "%tot", "sql%", "py%", "calls",
        "avg_ms", "sql/req", "ms/qry", "max_s", "bottleneck", "operation"))

    def rows(items, label):
        w("-" * 118 + "\n")
        w("%s  (sorted by total time)\n" % label)
        w("-" * 118 + "\n")
        w(hdr)
        for name, b in items:
            if b.n < a.min_calls or b.total < a.min_total:
                continue
            split = b.sql + b.py
            sqlpct = None if (b.unsplit and not split) else 100 * b.sql / (split or 1e-9)
            qpc = b.nq / b.n if b.n else 0
            msq = 1000 * b.sql / b.nq if b.nq else 0
            w("%9.0f %5.1f%% %6s %6s %8d %9.1f %7.1f %7.1f %8.1f %-11s %s\n" % (
                b.total, 100 * b.total / tot,
                "-" if sqlpct is None else "%.0f%%" % sqlpct,
                "-" if sqlpct is None else "%.0f%%" % (100 * b.py / (b.total or 1e-9)),
                b.n, 1000 * b.total / b.n, qpc, msq, b.mx,
                verdict(sqlpct, qpc, msq), name[:56]))
        w("\n")

    if a.by in ("family", "both"):
        fams, minor = [], []
        for k, v in prof.fam.items():
            (minor if v.n < a.fold else fams).append((k, v))
        fams.sort(key=lambda kv: -kv[1].total)
        if minor:
            merged = Bucket()
            for _k, v in minor:
                merged.n += v.n
                merged.sql += v.sql
                merged.py += v.py
                merged.unsplit += v.unsplit
                merged.nq += v.nq
                merged.errors += v.errors
                merged.mx = max(merged.mx, v.mx)
                merged.sample.extend(v.sample[:50])
            fams.append(("(%d types with fewer than %d calls each - mostly scanner "
                         "noise; raise --fold 1 to see them)" % (len(minor), a.fold), merged))
        rows(fams, "BY TYPE  (what mechanism: ui.read, ui.action, api.write, report, assets, cron, ...)")
    if a.by in ("app", "both"):
        apps = sorted(prof.app.items(), key=lambda kv: -kv[1].total)[:a.top]
        rows(apps, "BY APP  (which Odoo app owns the work: module prefix of the model / URL)")
    if a.by in ("detail", "both"):
        det = sorted(prof.det.items(), key=lambda kv: -kv[1].total)[:a.top]
        rows([(("%-12s %-16s %s" % (prof.det_fam.get(k, "?"), prof.det_app.get(k, "?")[:16], k)), v) for k, v in det],
             "TOP %d INDIVIDUAL OPERATIONS  (type, app, operation)" % a.top)

    if a.hours:
        w("-" * 118 + "\n")
        w("LOAD BY HOUR\n")
        w("-" * 118 + "\n")
        peak = max((b.total for b in prof.hours.values()), default=1)
        for h in sorted(prof.hours):
            b = prof.hours[h]
            w("  %s:00  %8d req  %9.0f s  sql %4.0f%%  %s\n" % (
                h, b.n, b.total, 100 * b.sql / (b.total or 1e-9),
                "#" * int(48 * b.total / peak)))
        w("\n")

    if a.logins and prof.logins:
        w("-" * 118 + "\n")
        w("LOGINS  (account + client IP, paired via the worker pid that logged both lines)\n")
        w("-" * 118 + "\n")
        w("%9s  %-38s %-18s %s\n" % ("count", "login", "client", "result"))
        for (login, ip, res), n in prof.logins.most_common(a.top):
            w("%9d  %-38s %-18s %s\n" % (n, login[:38], ip, res))
        w("\n")

    if a.events:
        w("-" * 118 + "\n")
        w("EVENTS  (log lines that are not requests, so they have no timing)\n")
        w("-" * 118 + "\n")
        fams = sorted(prof.events.items(), key=lambda kv: -sum(kv[1].values()))
        for fam, c in fams:
            w("  %-18s %8d lines\n" % (fam, sum(c.values())))
            for tpl, n in c.most_common(a.event_top):
                w("      %8d  %s\n" % (n, tpl[:104]))
        if prof.exceptions:
            w("\n  exception classes seen in tracebacks:\n")
            for exc, n in prof.exceptions.most_common(15):
                w("      %8d  %s\n" % (n, exc))
        if prof.server_actions:
            w("\n  server actions that emitted log lines (ir.actions.server id -> lines):\n")
            for sa, n in prof.server_actions.most_common(15):
                w("      %8d  ir.actions.server(%s)\n" % (n, sa))
        w("\n")

    if a.unclassified and prof.unclassified:
        w("-" * 118 + "\n")
        w("UNCLASSIFIED / CATCH-ALL SAMPLES  (extend classify() if any of these matter)\n")
        w("-" * 118 + "\n")
        for k, n in prof.unclassified.most_common(a.top):
            w("  %8d  %s\n" % (n, k[:104]))
        w("\n")


def as_rows(prof, scopes=("family", "app", "operation"), limit=None):
    """One row per type, per app and per operation (scope says which); `limit` keeps only the
    biggest rows of each scope (by total time), before any per-row work is done."""
    tot = sum(b.total for b in prof.fam.values()) or 1e-9
    out = []
    for scope, table in (("family", prof.fam), ("app", prof.app), ("operation", prof.det)):
        if scope not in scopes:
            continue
        for name, b in sorted(table.items(), key=lambda kv: -kv[1].total)[:limit]:
            split = b.sql + b.py
            sqlpct = None if (b.unsplit and not split) else 100 * b.sql / (split or 1e-9)
            out.append({
                "scope": scope,
                "family": name if scope == "family" else ("" if scope == "app" else prof.det_fam.get(name, "")),
                "app": name if scope == "app" else ("" if scope == "family" else prof.det_app.get(name, "")),
                "operation": name,
                "calls": b.n,
                "total_s": round(b.total, 3),
                "pct_of_total_time": round(100 * b.total / tot, 4),
                "sql_s": round(b.sql, 3),
                "pct_sql_within_op": round(100 * b.sql / (b.total or 1e-9), 2),
                "python_s": round(b.py, 3),
                "pct_python_within_op": round(100 * b.py / (b.total or 1e-9), 2),
                "unsplit_s": round(b.unsplit, 3),
                "avg_ms": round(1000 * b.total / b.n, 2) if b.n else 0,
                "p50_s": round(b.pct(50), 3),
                "p95_s": round(b.pct(95), 3),
                "max_s": round(b.mx, 3),
                "queries": b.nq,
                "queries_per_call": round(b.nq / b.n, 2) if b.n else 0,
                "ms_sql_per_query": round(1000 * b.sql / b.nq, 3) if b.nq else 0,
                "bottleneck": verdict(sqlpct,
                                      b.nq / b.n if b.n else 0,
                                      1000 * b.sql / b.nq if b.nq else 0),
                "http_5xx": b.errors,
            })
    return out
