"""`analyze()`: the answer to /api/analyze (files -> databases -> time interval), and the focused `timeline()`.
"""

from __future__ import annotations

from . import state
from .loading import _file_json, _load
from .results import (
    MAX_BINS, NICE_MIN, _add_hist, _issue_json, _op_rows, _rebin, _summary, _timeline_base, _timeline_for,
)
from .selection import _base, _cut, _select
from .slowq import _slow_queries, _sq_view
from .state import CUTS, FILES, FULL, LOCK, MAX_HIST_OPS, NOFILT, NoData, ORDER, SUMMARIES, TIMELINES
from .util import _cache_get, _iso, _remember, _secs


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

        sel = "%s|%s|%s|%s" % (",".join(ids), "\x1f".join(sorted(excl)), flt[0] or "", state.ONLY_DB or "")
        hist_key = sel                                                          # the operation timelines depend on this only
        view, rows, rows_key = full["summary"], full["rows"], sel + "|full"
        slow = _slow_queries(excl, exclude_file, None, flt)
        window = full["window"]
        if not window and slow:                                                 # slow-query logs alone: their span
            window = [int(slow[0][1].end), int(slow[-1][1].end) + 1]
            view = dict(view, stats=dict(view["stats"], window=[_iso(window[0]), _iso(window[1] - 1)],
                                         duration_s=window[1] - 1 - window[0]))
        if interval:
            lo, hi = interval
            if window:                                                          # nothing outside the logs can be selected
                lo, hi = max(lo, window[0]), min(hi, window[1])
            if not window or lo >= hi:
                raise NoData("the interval lies outside the loaded logs")
            if (lo, hi) == tuple(window):
                interval = None
            else:
                interval = (lo, hi)
                key = (ids, excl, lo, hi, flt[0])
                cut = _cache_get(CUTS, key)
                if cut is None:
                    prof = _cut(inc, ids, excl, lo, hi, flt)
                    cut = _remember(CUTS, key, {"summary": _summary(prof, inc), "rows": _op_rows(prof)})
                view, rows, rows_key = cut["summary"], cut["rows"], "%s|%d-%d" % (sel, lo, hi)
                slow = [x for x in slow if lo <= x[1].end < hi]

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
            pinned = [dict(_issue_json(prof, k, prof.issues[k], base), pinned=True) for k in pin_issues if k in prof.issues]
            if pinned:
                view = dict(view, issues=view["issues"] + pinned)
            pinned_ops = [{"family": whole.det_fam.get(o, ""), "app": whole.det_app.get(o, ""), "operation": o,
                           "hist": _rebin(whole.det[o].hist, timeline["base"])} for o in pin_ops if o in whole.det]
        return {
            "files": [_file_json(f) for f in files],                             # parsing may just have found problems
            "databases": dbs,
            "excluded_db": sorted(excl),
            "interval": list(interval) if interval else None,
            "window": window,
            "slowq": _sq_view(slow),
            "view": dict(view, rows=None if have_rows == rows_key else rows, rows_key=rows_key),
            "full_totals": full["summary"]["totals"],
            # the histogram is always the whole file + database selection, so it only travels when that
            # selection changed (the client says which one it already has)
            "hist_key": hist_key,
            "hist": dict(timeline, ops=None) if have_hist == hist_key else timeline,
            "pinned_ops": pinned_ops,
        }


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
