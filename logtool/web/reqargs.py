"""The arguments every analysis request takes (interval, fact filters, domain, pins), read from flask.request.
"""

from __future__ import annotations

import json

from flask import request

from .. import profiler as olp
from .selection import _filter_parse
from .state import FILES, FILTER_LOCK, LOCK, NOFILT, NoData, ORDER


def _prepare_filter(flt):
    """Re-parse the selected files with a new filter before taking LOCK (see _filter_parse)."""
    if flt[0] is None:
        return
    with LOCK:
        files = [FILES[i] for i in ORDER if FILES[i].get("kind") == "log" and FILES[i].get("usable")
                 and i not in request.args.getlist("exclude_file")]
    with FILTER_LOCK:                               # one filter re-parse at a time (concurrent ones would just repeat it)
        _filter_parse(files, flt)


def _interval_arg():
    """The requested time interval (from, to in seconds) or None. Raises NoData with a message for the user."""
    if request.args.get("from") is None or request.args.get("to") is None:
        return None
    try:
        interval = (int(request.args["from"]), int(request.args["to"]))
    except ValueError:
        raise NoData("the interval must be two whole numbers of seconds")
    interval = tuple(max(0, min(x, 10 ** 11)) for x in interval)    # far outside any log: clamped (dates overflow past that)
    if interval[0] >= interval[1]:
        raise NoData("the interval must end after it starts")
    return interval


def _ff_arg():
    """The fact filters (see olp._facts_ok) from the `ff` argument, as (cache key, args.fact_filter), or NOFILT.
    ff is a JSON list of {"by": operation | app | family | issue, "name": row name, or [level, logger, message]
    for an issue, "facts": {detail: [values]}}; each `ip` argument is a client address the whole report is
    narrowed to. Raises NoData with a message for the user."""
    raw, xd, ips = request.args.get("ff"), request.args.get("xd"), sorted(set(request.args.getlist("ip")) - {""})
    if not raw and not xd and not ips:
        return NOFILT
    bad = NoData("ff must be a list of {by, name, facts: {detail: [values]}}")
    try:
        items = json.loads(raw or "[]")
    except (ValueError, RecursionError):
        raise bad
    if not isinstance(items, list):
        raise bad
    ff = {}
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("facts"), dict):
            raise bad
        by, name = it.get("by"), it.get("name")
        if by == "issue" and isinstance(name, list) and len(name) == 3 and all(isinstance(x, str) for x in name):
            name = tuple(name)
        elif by not in ("operation", "app", "family") or not isinstance(name, str) or not name:
            raise bad
        want = ff.setdefault(by, {}).setdefault(name, {})
        for slot, vals in it["facts"].items():
            if not isinstance(vals, list) or not all(isinstance(v, str) for v in vals):
                raise bad
            if by == "issue" and slot in olp.ISSUE_UNFILTERABLE:
                continue
            if slot == olp.F_DUR and not all(olp.dur_range(v) for v in vals):
                raise NoData("%s values are ranges of ms like \"100-150\" or \"5000-\"" % olp.F_DUR)
            vals = {v for v in vals if v != olp.FACT_OTHER}
            if vals:
                want[slot] = frozenset(want.get(slot, frozenset()) | vals)
        if not want:
            del ff[by][name]
    ff = {by: rows for by, rows in ff.items() if rows}
    extra = _xd_arg(xd)
    if extra:
        ff["extra"] = extra
    if ips:
        ff["client"] = {olp.F_IP: frozenset(ips)}
    if not ff:
        return NOFILT
    canon = sorted([by, list(name) if by == "issue" else [name], sorted([k, sorted(v)] for k, v in want.items())]
                   for by, rows in ff.items() if by not in ("extra", "client") for name, want in rows.items())
    return json.dumps([canon, extra, ips], ensure_ascii=False, sort_keys=True), ff


def _xd_arg(xd):
    """The part of the global domain the app's own filters cannot hold (the `xd` argument, a JSON prefix list),
    validated; None when there is none. Raises NoData with a message for the user."""
    if not xd:
        return None
    try:
        node = olp.dom_tree(json.loads(xd))
    except (ValueError, RecursionError) as e:           # DomainError is a ValueError too
        raise NoData("xd: %s" % e)
    if node is not None and olp.dom_mentions(node, olp.DOM_SELECTION):
        raise NoData("xd: file, database and time are chosen with their own arguments")
    if node is not None and olp.dom_mentions(node, olp.DOM_VIEW):
        raise NoData("xd: view settings are not conditions")
    return olp.dom_prefix(node) or None


def _pins_arg():
    """{"issues": [[level, logger, message]], "ops": [operation]} from the `pins` argument (see analyze()), or None."""
    raw = request.args.get("pins")
    if not raw:
        return None
    try:
        pins = json.loads(raw)
    except (ValueError, RecursionError):
        raise NoData("pins must be JSON")
    ok = isinstance(pins, dict) and isinstance(pins.get("issues", []), list) and isinstance(pins.get("ops", []), list) and \
        all(isinstance(k, list) and len(k) == 3 and all(isinstance(x, str) for x in k) for k in pins.get("issues", [])) and \
        all(isinstance(o, str) for o in pins.get("ops", []))
    if not ok:
        raise NoData("pins must be {issues: [[level, logger, message]], ops: [operation]}")
    return {"issues": pins.get("issues", [])[:100], "ops": pins.get("ops", [])[:200]}


def _issue_key_arg(level, logger, msg):
    return (level, logger, msg) if level and logger and msg is not None else None
