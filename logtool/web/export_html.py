"""Export HTML: the page itself as one standalone file, with the current selection built in.
"""

from __future__ import annotations

import datetime

from .. import profiler as olp
from .analysis import analyze, timeline
from .details import _detail, _facts_view
from .scrub import _Alias, _scrub, _scrub_facts, _scrub_issue, _scrub_timeline_op, _secret_names
from .selection import _heavy, _prof_for, _select
from .slowq_detail import slowq_detail
from .state import LOCK, NOFILT, NONE_DB, NoData


HTML_DETAILS = 50       # rows of the apps, types and errors tables whose details popup Export HTML carries ...
HTML_OP_DETAILS = 100   # ... and of the operations table, plus every job operation (cron, automation, module loading):
                        # they rank low by total time, yet the type 'cron' lists them all, and their details are small
HTML_NOTE = ("Exported %s UTC: a read-only copy of the page, of the selected databases only; the raw log lines are "
             "not in it. What lines without a database ('None') logged is censored: IPs, sessions, logins, emails, user "
             "and record ids are replaced by pseudonyms. Details are kept for the top %d operations, every cron, "
             "automation and module-loading operation, and the top %d apps, types and errors."
             % ("%s", HTML_OP_DETAILS, HTML_DETAILS))


def _filter_desc(interval, flt):
    """What Export HTML says the page is filtered by: which kinds of filter and how many, never what they name (a
    filter can name clients, users, records...)."""
    ff = flt[1] or {}
    nf = sum(len(v) for by, rows in ff.items() if by not in ("extra", "client") for want in rows.values() for v in want.values())
    nx = sum(isinstance(x, list) for x in ff.get("extra") or ())
    ni = len(ff.get("client", {}).get(olp.F_IP, ()))
    n = lambda k, one: "%d %s%s" % (k, one, "" if k == 1 else "s")      # noqa: E731
    return " · ".join((["time interval"] if interval else []) + ([n(nf, "item filter")] if nf else []) +
                      ([n(nx, "custom condition")] if nx else []) + (["only " + n(ni, "client IP")] if ni else []))


def export_html(exclude_db, exclude_file, interval, flt=NOFILT, pins=None, view=None):
    """What Export HTML builds into the page (see the snapshot in index.html): the analyze() answer the page
    shows now, cut down to the selected databases (the others are not even named), plus the details popup of
    the top rows of each table (see HTML_OP_DETAILS). The selected databases' own data is kept as it is; only what lines
    without a database (NONE_DB) logged -- the rows and messages they show up in -- is censored like export()'s
    (the other databases' and the files' names, emails, IPs blanked), with clients, accounts, user and record ids
    pseudonymized too. view: the page's view settings (see viewState() in app.js), whose items are censored the
    same way so they still match. The filter itself stays out: only how many of each kind (_filter_desc), and the
    view's search boxes are emptied (they hold whatever was typed; every row is in the page anyway)."""
    a = analyze(exclude_db, exclude_file, interval, False, flt=flt, pins=pins)
    if a.get("imported"):
        raise NoData("an imported summary cannot be exported as HTML")
    with LOCK:
        files, inc, dbs, excl = _select(set(a["excluded_db"]), exclude_file, False)
        ids, excl = tuple(f["id"] for f in inc), frozenset(excl)
        iv = tuple(a["interval"]) if a["interval"] else None
        prof = _heavy(_prof_for(inc, ids, excl, iv, flt))
        prof0 = _heavy(_prof_for(inc, ids, excl, iv)) if flt[0] else prof
        nodb = None                                     # what lines without a database logged (to censor)
        if NONE_DB in {d for d, _ in dbs} - excl:
            try:
                nodb = _prof_for(inc, ids, frozenset(d for d, _ in dbs if d != NONE_DB), iv, flt)
            except NoData:                              # none of them in the interval
                pass
    keep = [d for d in dbs if d[0] not in excl]
    secrets = [s for s in _secret_names({d for d, _ in dbs if d in excl and d not in ("?", "None")}, files)
               if s not in {d for d, _ in keep}]
    alias = _Alias()
    none_ops = set(nodb.det) if nodb else set()
    tainted = {"operation": none_ops, "issue": set(nodb.issues) if nodb else set(),
               "app": {prof.det_app.get(o) for o in none_ops}, "family": {prof.det_fam.get(o) for o in none_ops}}

    def txt(text):
        return _scrub(text, secrets)

    def scrub(by, name):
        return txt(name) if name in tainted[by] else name

    def issue_key(k):
        return (k[0], txt(k[1]), txt(k[2])) if tuple(k) in tainted["issue"] else tuple(k)

    def facts(f):
        return _scrub_facts(f, secrets, alias, ids=True)

    def op_detail(by, name):
        d = _detail(prof, by, name, _facts_view(prof, prof0, by, name, flt), _facts_view(prof, prof0, by, name, flt, logged=True))
        if name not in tainted[by]:
            return d
        return dict(d, name=txt(name), examples=[], facts=facts(d.get("facts", [])), logged_facts=facts(d.get("logged_facts", [])),
                    top_ops=[dict(o, operation=scrub("operation", o["operation"])) for o in d.get("top_ops", [])],
                    sources=dict(d["sources"], items=[dict(e, key=txt(e["key"]), sample="", ops=[[txt(o), n] for o, n in e["ops"]])
                                                      for e in d["sources"]["items"]]) if d["found"] else None)

    def issue(i):
        key = (i["level"], i["logger"], i["message"])
        return dict(i, **_scrub_issue(i, secrets, alias, ids=True), sample="") if key in tainted["issue"] else i

    def op(o):
        return _scrub_timeline_op(o, secrets) if o["operation"] in none_ops else o

    v = a["view"]
    details = {}
    with LOCK:
        for by, rows in (("operation", v["rows"]), ("app", v["groups"]["app"]), ("family", v["groups"]["family"])):
            top = HTML_OP_DETAILS if by == "operation" else HTML_DETAILS
            names = [r[by] for r in sorted(rows, key=lambda r: -r["total_s"])[:top]]
            if by == "operation":
                names += [r["operation"] for r in rows if r["family"] in olp.JOB_KINDS] + (pins or {}).get("ops", [])
            for name in dict.fromkeys(names):
                details["%s\x1f%s" % (by, scrub(by, name))] = op_detail(by, name)
        for i in v["issues"][:HTML_DETAILS] + [i for i in v["issues"][HTML_DETAILS:] if i.get("pinned")]:
            key = (i["level"], i["logger"], i["message"])
            f = _facts_view(prof, prof0, "issue", key, flt)
            details["\x1f".join(("issue",) + issue_key(key))] = {
                "found": True, "facts": facts(f) if key in tainted["issue"] else f}

        # the forms of the longest run of the slow-query shapes with the most time, their SQL censored (literals
        # replaced by ?) like the shapes the table shows
        for r in a["slowq"]["rows"][:HTML_OP_DETAILS]:
            d = slowq_detail(r["id"], set(excl), exclude_file, iv, flt)
            details["slowq\x1f" + r["id"]] = dict(d, sql=olp.sq_censor(d["sql"]))

    # an interval or a focus: the histogram shows just that span, at its finer buckets (see timeline())
    view = dict(view or {})
    for k in ("table", "errors"):
        if isinstance(view.get(k), dict):
            view[k] = dict(view[k], search="")
    span = iv or (tuple(view["focus"]) if isinstance(view.get("focus"), list) and len(view["focus"]) == 2 else None)
    focus = None
    if span:
        keys = [tuple(it["issue"]) for it in view.get("items", []) if isinstance(it.get("issue"), list) and len(it["issue"]) == 3]
        t = timeline(set(excl), exclude_file, span, flt, keys, frozenset((pins or {}).get("ops", [])))
        focus = dict(t, ops=[op(o) for o in t["ops"]],
                     issues=[dict(i, **dict(zip(("level", "logger", "message"), issue_key((i["level"], i["logger"], i["message"])))))
                             for i in t["issues"]])
    view["focus"] = [focus["from"], focus["to"]] if focus else None
    for it in view.get("items", []):
        if isinstance(it.get("issue"), list) and len(it["issue"]) == 3:
            it["issue"] = list(issue_key(it["issue"]))
        for k, by in (("operation", "operation"), ("app", "app"), ("type", "family")):
            if isinstance(it.get(k), str):
                it[k] = scrub(by, it[k])
    return {
        "note": HTML_NOTE % datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "filter": _filter_desc(iv, flt),
        "view": view,
        "details": details,
        "focus": focus,
        "analyze": dict(a, files=[], databases=keep, excluded_db=[], hist_key="snapshot",
                        view=dict(v, files=[None] * len(v["files"]), rows_key="snapshot",
                                  rows=[dict(r, operation=scrub("operation", r["operation"])) for r in v["rows"]],
                                  issues=[issue(i) for i in v["issues"]]),
                        hist=dict(a["hist"], ops=[op(o) for o in a["hist"]["ops"]]),
                        pinned_ops=[op(o) for o in a["pinned_ops"]]),
    }
