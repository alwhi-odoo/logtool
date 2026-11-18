"""Export summary: a scrubbed JSON copy of the selection (it can be imported again, see loading.py).
"""

from __future__ import annotations

import datetime

from .. import profiler as olp
from .results import _facts_json, _ip_logins, _op_rows, _rebin, _span, _summary, _timeline_for
from .scrub import _Alias, _scrub, _scrub_facts, _scrub_issue, _scrub_row, _scrub_timeline_op, _secret_names
from .selection import _heavy, _prof_for, _select
from .slowq import _slow_queries, _sq_export
from .state import LOCK, MAX_EXPORT_ISSUES, NOFILT, NONE_DB
from .util import _iso


EXPORT_NOTE = ("Database names, uploaded file names, and raw log-line samples are left out entirely. Known "
               "database and file names are blanked out wherever they appeared in free text as whole words "
               "(except names shorter than 4 characters and common words such as 'odoo' or 'test', which would "
               "wreck every logger and message), along with email and IP (v4 and v6) addresses. Client IPs, session ids and login names are replaced by pseudonyms (ip-1, "
               "session-3, user-2) that stay the same throughout this file, so one client can be followed across "
               "operations, messages and time without saying who it is. User ids and record ids are kept as "
               "numbers. Operation, cron and automation-rule names come straight from this database's own "
               "configuration (for example a custom cron job's title) and are not further redacted -- skim "
               "them before sharing this file widely.")
EXPORT_FIELDS = {
    "timestamps": "The log's own clock, no time zone. 'first' / 'last' / '*_seen' are YYYY-MM-DDTHH:MM[:SS].",
    "hist": "Timelines. hist.base is the bucket size in minutes; hist.all and each hist.ops[].hist are "
            "[[bucket start (minutes since 1970-01-01), runs, sql s, python s, job s], ...], buckets with nothing "
            "left out. Every cron / automation / module-load operation has one, plus the busiest operations.",
    "rows": "One per operation (request pattern, cron, automation rule), whole selection. first_seen / last_seen "
            "and active_minutes say when it ran; facts lists what grouping collapsed.",
    "facts": "Per row / message: [{detail, values: [[value, count] or [value, count, [logins from that ip]]], "
             "distinct, other_n, total}], most frequent first. details: 'client IP', 'session' (Odoo 19.4+), 'URL' "
             "(the raw path, when ids or a query were collapsed), 'cron id', 'status', 'remaining', 'rule id', "
             "'records processed', 'user id' and 'records' (named in a message or its traceback), and '<word> #' "
             "(the number that followed <word> where the message shows '#'). A message's 'client IP' / 'session' is "
             "the request its worker was running when it was logged. Only the first 50 distinct values of a "
             "detail are told apart; other_n counts the rest.",
    "issues": "Every distinct warning / error message (numbers replaced by #), with its own timeline.",
    "clients": "Who sent requests when (assets and scanner probes left out): a client is an ip, or ip + session "
               "from Odoo 19.4. logins are the accounts that logged in from that ip; ops are its busiest operations.",
    "logins": "Login lines paired with the access line of the same worker: account, ip, result, count, first / last.",
    "log_levels": "Log lines per level. What is missing here (e.g. no DEBUG) is missing from everything else.",
    "limits": "What this log cannot show, worked out from what it contains. Read before drawing conclusions.",
    "slow_queries": "The PostgreSQL slow-query logs of the selection, without their SQL: how many queries, how long "
                    "they ran (s), by_shape (runs of one query whatever its values, its shape id being a hash of its "
                    "SQL with every literal replaced by ?) and the longest runs. t is when it was logged (it ended).",
}


def _export_clients(prof, secrets, alias, base):
    """Per client: when it was active (a timeline at the histogram's resolution), what it ran and who logged in from it."""
    ipl = _ip_logins(prof)
    by_ip = {}
    for op, f in prof.op_facts.items():
        if op in prof.det:
            for ip, n in (f.get(olp.F_IP) or {}).items():
                by_ip.setdefault(ip, []).append((op, n))
    out = []
    for c, h in sorted(prof.clients.items(), key=lambda kv: -sum(v[0] for v in kv[1].values())):
        other = c == olp.CLIENT_OTHER
        ip, _, sess = ("", "", "") if other else c.partition(" ")
        bins = {}
        for m, (n, secs) in h.items():
            e = bins.setdefault(m // base * base, [0, 0.0])
            e[0] += n
            e[1] += secs
        out.append({
            "client": c if other else alias("ip", ip) + (" " + alias("session", sess) if sess else ""),
            "logins": [] if other else [alias("user", x) for x in ipl.get(ip, [])],
            "requests": int(sum(v[0] for v in h.values())),
            "seconds": round(sum(v[1] for v in h.values()), 3),
            "first": _iso(min(h) * 60)[:16], "last": _iso(max(h) * 60)[:16],
            "hist": [[k, int(e[0]), round(e[1], 3)] for k, e in sorted(bins.items())],
            "ops": [] if other else [[_scrub(op, secrets), n] for op, n in
                                     sorted(by_ip.get(ip, []), key=lambda kv: -kv[1])[:10]],
        })
    return out


def _limits(prof, issues_n, clients):
    """What this log cannot tell, worked out from what it contains (see EXPORT_FIELDS["limits"])."""
    lv, fm = prof.all_levels, prof.formats
    out = []
    if not lv.get("DEBUG"):
        out.append("No DEBUG lines. Automation rules triggered on save (on create, on write, on change, ...) log "
                   "only at DEBUG%s, so the rows show time-based rules and crons only; the absence of lines at a "
                   "given time says nothing about rules that run on save."
                   % (" (Odoo 20 does log them, with a duration, when slower than 0.1 s)" if any(
                       k.startswith("automation 20") for k in fm) else ""))
    if not any(k.startswith("access: odoo.http.server") for k in fm):
        out.append("No session per request: before Odoo 19.4 the access line carries only the client IP, so a "
                   "client is an IP address, and everyone behind one proxy or office network shares it.")
    out.append("No user per request: Odoo does not log the user on the access line. 'logins' ties an account to "
               "the IP it logged in from, at login time only; a user id is known only where a message names it "
               "(facts 'user id', e.g. access errors).")
    out.append("Crons, automation rules and module loading are timed from their own log messages: one duration per "
               "run, no SQL / Python split. Server actions have no timing of their own; their time is part of the "
               "request, cron or rule that ran them.")
    ips = {c["client"].split(" ")[0] for c in clients if c["client"] != olp.CLIENT_OTHER}
    reqs = sum(c["requests"] for c in clients)
    if 0 < len(ips) <= 2 and reqs >= 1000:
        out.append("All %d requests come from %d IP address%s: probably a reverse proxy in front of Odoo without "
                   "--proxy-mode, so the IP does not identify the client." % (reqs, len(ips), "" if len(ips) == 1 else "es"))
    if any(c["client"] == olp.CLIENT_OTHER for c in clients):
        out.append("Only the first %d clients are told apart; later ones are grouped as '%s'."
                   % (olp.CLIENT_KEEP, olp.CLIENT_OTHER))
    if issues_n > MAX_EXPORT_ISSUES:
        out.append("Only the %d most frequent of %d distinct warning / error messages are included."
                   % (MAX_EXPORT_ISSUES, issues_n))
    return out


def export(exclude_db, exclude_file, interval, flt=NOFILT):
    """A scrubbed copy of the current selection's summary, safe to hand to someone outside the team
    (or an AI): no database or file names, no raw log-line samples, clients and accounts pseudonymized.
    See EXPORT_NOTE for what this cannot catch."""
    with LOCK:
        files, inc, dbs, exclude_db = _select(exclude_db, exclude_file, False)
        ids, excl = tuple(f["id"] for f in inc), frozenset(exclude_db)
        prof = _heavy(_prof_for(inc, ids, excl, interval, flt))
        view = _summary(prof, inc, MAX_EXPORT_ISSUES)
        secrets = _secret_names({d for d, _ in dbs if d and d not in (NONE_DB, "?", "None")}, inc)
        alias = _Alias()
        ipl = _ip_logins(prof)
        groups = {scope: [_scrub_row(r, secrets) for r in rows] for scope, rows in view["groups"].items()}
        timeline = _timeline_for(prof)
        have = {o["operation"] for o in timeline["ops"]}
        jobs = [{"family": prof.det_fam.get(k, ""), "app": prof.det_app.get(k, ""), "operation": k,
                 "hist": _rebin(b.hist, timeline["base"])}
                for k, b in prof.det.items() if prof.det_fam.get(k) in olp.JOB_KINDS and k not in have]
        timeline = dict(timeline, ops=[_scrub_timeline_op(o, secrets) for o in timeline["ops"] + jobs])
        rows = []
        for r in _op_rows(prof):
            first, last, active = _span(prof.det[r["operation"]])
            rows.append(dict(_scrub_row(r, secrets), first_seen=first, last_seen=last, active_minutes=active,
                             facts=_scrub_facts(_facts_json(prof.op_facts.get(r["operation"]), ipl,
                                                            times=prof.op_facts_s.get(r["operation"]) or {}), secrets, alias)))
        clients = _export_clients(prof, secrets, alias, timeline["base"])
        return {
            "note": EXPORT_NOTE,
            "fields": EXPORT_FIELDS,
            "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "files_n": len(inc),
            "databases_n": len(dbs),
            "limits": _limits(prof, view["issues_distinct"], clients),
            "log_levels": dict(prof.all_levels.most_common()),
            "totals": view["totals"],
            "levels": view["levels"],
            "stats": view["stats"],
            "groups": groups,
            "rows": rows,
            "issues": [_scrub_issue(i, secrets, alias) for i in view["issues"]],
            "issues_distinct": view["issues_distinct"],
            "clients": clients,
            "logins": [{"login": alias("user", login), "ip": alias("ip", ip), "result": res, "n": n,
                        "first": (prof.login_seen.get((login, ip, res)) or [None])[0],
                        "last": (prof.login_seen.get((login, ip, res)) or [None, None])[1]}
                       for (login, ip, res), n in prof.logins.most_common()],
            # the "Requests over time" histogram, so an imported summary (see _summary_view()) can still
            # show it; older exports simply lack this key and import with no histogram, as before.
            "hist": timeline,
            "slow_queries": _sq_export(_slow_queries(excl, exclude_file, interval, flt)),
        }
