"""A merged Profile as JSON for the page: totals, the type / app tables, facts, issues, timelines.
"""

from __future__ import annotations

from .. import profiler as olp
from .state import FACT_TOP, MAX_HIST_OPS, MAX_ISSUES, MAX_ROWS
from .util import _duration, _iso


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
