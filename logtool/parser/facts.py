"""Facts: the values grouping collapses (client IP, session, URL, user id, records, cron id, ...) and fact filters.

An operation or a message is one row because ids, clients and numbers are collapsed. Each row keeps a bounded
count of those values per kind of detail, {detail: {value: n}} (`_fact`), and a filter on them keeps only the
occurrences that carry a picked value (`_facts_ok`, `_op_filter_ok`). Also here: the fields a domain can test
on one occurrence (`request_fields`, `job_fields`, `issue_fields`).
"""

from __future__ import annotations

import functools
import math
import re

from .lines import _DIGITS, _HEX


# What grouping throws away. An operation or a message is one row because ids, clients and numbers are collapsed,
# but those values are what tells one occurrence from another (which user, which record, which client). Each row
# keeps a bounded count of them per kind of detail: {detail: {value: n}}.
FACT_KEEP = 200             # distinct values remembered per detail; later ones only add to FACT_OTHER
FACT_OTHER = "(other values)"
FACT_LEN = 200              # longest value kept
F_IP, F_SESSION, F_URL = "client IP", "session", "URL"
F_USER, F_REC, F_LOGIN = "user id", "records", "login"
ISSUE_MORE_LINES = 60       # continuation lines (message body, traceback) searched for users and records
ISSUE_NUMS = 6              # numbers of one message head remembered, each under the word that precedes it
CLIENT_KEEP = 500           # clients (ip, or ip + session from 19.4) whose activity over time is kept
CLIENT_OTHER = "(other clients)"
# "Sorry, Jane (id=69) doesn't have ...", "uid 69", "user_id: 69"; "sale.order(12, 13)", "(sale.order: 12)"
USER_ID = re.compile(r"\(id=(\d+)\)|\buid\b\W{0,3}(\d+)|\buser(?:_id)?\b\W{1,3}(\d+)", re.I)
RECORDS = re.compile(r"\b([a-z]\w*(?:\.\w+)+)\((\d+(?:, ?\d+)*),?\)|\(([a-z]\w*(?:\.\w+)+): (\d+)")
NUM_LABEL = re.compile(r"([A-Za-z_][\w.]*)\W{0,3}$")


def _fact(d, slot, value, n=1):
    """Count `value` under `slot` in a facts dict {slot: {value: n}}, keeping at most FACT_KEEP distinct values."""
    s = d.get(slot)
    if s is None:
        s = d[slot] = {}
    if value in s:
        s[value] += n
    elif len(s) < FACT_KEEP:
        s[value] = n
    else:
        s[FACT_OTHER] = s.get(FACT_OTHER, 0) + n


def _fact_time(d, t, slot, value, seconds):
    """_fact(d, slot, value), plus the seconds of that occurrence in t, a {slot: {value: seconds}} twin of d kept
    under the same keys (a value d has no room for adds to FACT_OTHER in both)."""
    _fact(d, slot, value)
    k = value if value in d[slot] else FACT_OTHER
    s = t.get(slot)
    if s is None:
        s = t[slot] = {}
    s[k] = s.get(k, 0.0) + seconds


def merge_fact_times(into, into_t, t):
    """Add the twin t (see _fact_time) into into_t, once its facts have been merged into `into` (merge_facts):
    a value `into` kept no room for goes to FACT_OTHER there too."""
    for slot, vals in t.items():
        kept = into.get(slot) or {}
        s = into_t.setdefault(slot, {})
        for v, secs in vals.items():
            k = v if v in kept else FACT_OTHER
            s[k] = s.get(k, 0.0) + secs


def merge_facts(into, facts):
    """Add one facts dict into another (which owns its dicts)."""
    for slot, vals in facts.items():
        s = into.get(slot)
        if s is None:
            into[slot] = dict(vals)
        elif len(s) + len(vals) <= FACT_KEEP:            # room for all of them: no per-value bookkeeping
            for v, n in vals.items():
                s[v] = s.get(v, 0) + n
        else:
            for v, n in vals.items():
                _fact(into, slot, v, n)


# Fact filters: the user picked values in the details behind a row (a client IP, a URL, a user id...) to keep
# only the occurrences that carry them. args.fact_filter is {"operation" | "app" | "family": {name: want},
# "issue": {(level, logger, template): want}} with want = {detail: frozenset(values)}: an occurrence is kept
# when, for every detail in `want`, it carries one of the values (OR within a detail, AND across details).
# A warning's client IP and session come from the request that later claims it, so a warning filtered by them
# waits in the worker's pending list until that request ends (see Profile._claim); one no request claims has
# no client and is left out. args.fact_filter["client"] = {"client IP": frozenset(ips)} narrows the whole report
# to those clients (the domain's "ip"): their requests and every warning logged during them, nothing else.
# what the lines logged while a request / job ran name (users, records), told apart from its own details
F_LOGGED_USER, F_LOGGED_REC = "logged user id", "logged records"
LOGGED_OF = ((F_USER, F_LOGGED_USER), (F_REC, F_LOGGED_REC))
# how long a request / job took, picked as ranges of milliseconds "lo-hi" (lo included, hi not; "lo-": no upper
# bound), the duration histogram's bins; `have` holds its seconds rather than a set of values
F_DUR = "duration ms"
ISSUE_UNFILTERABLE = (F_LOGGED_USER, F_LOGGED_REC, F_DUR)    # a warning has no lines of its own, nor a duration
CLIENT_FACTS = (F_IP, F_SESSION)    # a warning's client: only known once the request that logged it ends


@functools.lru_cache(maxsize=256)
def dur_range(v):
    """A F_DUR value "lo-hi" (ms) -> (lo, hi) in seconds, hi inf for "lo-"; None when it is not one."""
    lo, sep, hi = v.partition("-")
    try:
        lo, hi = float(lo) / 1000, float(hi) / 1000 if hi.strip() else math.inf
    except ValueError:
        return None
    return (lo, hi) if sep and 0 <= lo < hi else None


def _facts_ok(want, have):
    """Does one occurrence, carrying facts `have` ({detail: iterable of values}), satisfy filter `want`?"""
    for slot, vals in want.items():
        got = have.get(slot)
        if slot == F_DUR:
            if got is None or not any(lo <= got < hi for lo, hi in map(dur_range, vals)):
                return False
        elif not got or vals.isdisjoint(got):
            return False
    return True


def _op_filter_ok(ff, fam, app, det, have):
    """Does one request / job of operation `det` pass every fact filter aimed at it, its app or its type, and
    the report's client (a job has none)?"""
    if ff.get("client") and not _facts_ok(ff["client"], have):
        return False
    for by, name in (("operation", det), ("app", app), ("family", fam)):
        want = ff.get(by, {}).get(name)
        if want and not _facts_ok(want, have):
            return False
    return True


def _client_want(ff, want):
    """What the client of a warning with item filter `want` must be: that filter's client IP / session, and
    the report's client (ff["client"])."""
    client = dict(ff.get("client") or {})
    for k, v in want.items():
        if k in CLIENT_FACTS:
            client[k] = client[k] & v if k in client else v
    return client


def request_facts(ip, session, path, collapsed, seconds):
    """The facts one access line carries ({detail: set of values}), for item filters. The URL is always there
    (a domain's URL is the raw path of every request); only the details shown keep it for collapsed paths."""
    have = {F_IP: {ip}, F_URL: {path[:FACT_LEN]}, F_DUR: seconds}
    if session != "-":
        have[F_SESSION] = {session}
    return have


def request_fields(fam, app, det, status, method, qt, ot, nq, ip, session, path, logged=None):
    """Everything a domain (see dom_compile) can test on one request."""
    return {"kind": "request", "operation": det, "app": app, "type": fam, "duration": qt + ot, "sql_s": qt,
            "python_s": ot, "queries": nq, "http_status": int(status) if status.isdigit() else status,
            "method": method, F_IP: ip, F_SESSION: None if session == "-" else session, F_URL: path,
            F_LOGGED_USER: (logged or {}).get(F_LOGGED_USER, set()), F_LOGGED_REC: (logged or {}).get(F_LOGGED_REC, set())}


def job_fields(fam, app, det, seconds, nq, facts, logged=None):
    f = {"kind": "job", "operation": det, "app": app, "type": fam, "duration": seconds, "queries": nq,
         "cron id": set(), "status": set(), "remaining": set(), "rule id": set(), "records processed": set(),
         "modules": set(),
         F_LOGGED_USER: set((logged or {}).get(F_LOGGED_USER, ())), F_LOGGED_REC: set((logged or {}).get(F_LOGGED_REC, ()))}
    for slot, v in facts:
        f.setdefault(slot, set()).add(v)
    return f


def issue_fields(level, logger, tpl, msg, facts):
    return dict({F_USER: set(), F_REC: set()}, **facts, kind="issue", level=level, logger=logger, message=tpl,
                issue=(level, logger, tpl), text=msg)


def issue_occurrence_facts(msg, more=()):
    """The facts one warning / error line carries ({detail: set of values}), exactly as _issue() and the
    continuation-line handling count them: the numbers behind '#', users and records in the message and in
    `more`, the first continuation lines (body, traceback)."""
    d = {}
    _num_facts(d, msg[:140])
    _detail_facts(d, msg[:2000])
    for line in more:
        _detail_facts(d, line)
    return {k: set(v) for k, v in d.items()}


def _num_facts(d, head):
    """The numbers normalize() replaces by '#' in a message head, each counted under the word before it
    ('scheduler #', 'ID #', 'ID # (2)'), so '#' can be read back as the ids that were there."""
    text = _HEX.sub("<hex>", head)
    seen = {}
    for i, m in enumerate(_DIGITS.finditer(text)):
        if i >= ISSUE_NUMS:
            break
        lm = NUM_LABEL.search(text, max(0, m.start() - 40), m.start())
        label = (lm.group(1) if lm else "") + " #"
        k = seen[label] = seen.get(label, 0) + 1
        _fact(d, label.strip() if k == 1 else "%s (%d)" % (label.strip(), k), m.group())


def _detail_facts(d, text):
    """Users and records named anywhere in a message or its traceback."""
    if "id" not in text and "(" not in text and "user" not in text and "User" not in text:   # cheap reject: names none
        return
    for m in USER_ID.finditer(text):
        _fact(d, F_USER, m.group(1) or m.group(2) or m.group(3))
    for m in RECORDS.finditer(text):
        if m.group(1):
            ids = m.group(2).replace(" ", "").split(",")
            _fact(d, F_REC, "%s(%s%s)" % (m.group(1), ",".join(ids[:5]), ",…" if len(ids) > 5 else "")[:FACT_LEN])
        else:
            _fact(d, F_REC, "%s(%s)" % (m.group(3), m.group(4)))
