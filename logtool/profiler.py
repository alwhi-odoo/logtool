#!/usr/bin/env python3
"""
logtool.profiler - where does an Odoo server's time actually go?

Parses Odoo server logs, groups every request into a meaningful *operation*
(login, RPC call, list view read, button, import, export, report, asset, ...),
and reports SQL time / Python time / total time per operation, sorted by the
biggest consumer first.

Odoo's access log lines carry three numbers Odoo does not advertise:

    ... "POST /web/dataset/call_kw/account.move/web_read HTTP/1.1" 200 - 91 0.412 0.663
                                                                         ^^ ^^^^^ ^^^^^
                                                                         |  |     |
                                                        SQL queries -----+  |     |
                                                        seconds in SQL -----+     |
                                                        seconds not in SQL -------+

so total = sql + python, and the split is free. Everything else in this script
is about grouping those lines into buckets you can act on.

Every request is described by three independent answers: its *type* (ui.read,
ui.action, api.write, report, assets, ...), the Odoo *app* that owns it (the module
prefix of the model or URL) and the *operation* itself. Cron jobs, automation rules and
module loading are timed from their own log messages and join the same tables (their
SQL/Python split is unknown, so they are shown as "job").

Lines that are not requests (logins, tracebacks, deprecation warnings,
record-rule denials, ...) have no timing, so they are counted separately as
*events*. Nothing is dropped: the coverage report at the top accounts for every
line in the file, and --unclassified shows you what fell into the catch-alls.

Usage
    logtool-report LOG [LOG ...]              # plain, .gz and .bz2, or - for stdin
    logtool-report odoo.log --top 40
    logtool-report odoo.log --by family       # roll up, no detail table
    logtool-report odoo.log --events          # also the non-request lines
    logtool-report odoo.log --logins          # logins per account + client IP
    logtool-report odoo.log --unclassified    # samples of anything that hit "other"
    logtool-report odoo.log --csv out.csv --json out.json
    logtool-report odoo.log --since 2026-09-14T13:00 --until 2026-09-14T21:00
    logtool-report odoo.log --db db-name --grep account.move

Stdlib only. Streams, so a 400 MB log costs a few seconds and constant memory.
"""

from __future__ import annotations

import argparse
import array
import bz2
import contextlib
import csv
import datetime
import gzip
import heapq
import json
import multiprocessing
import os
import random
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
import zlib
from collections import Counter, defaultdict, deque
from itertools import chain
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import NamedTuple, Optional

# --------------------------------------------------------------------------- #
# 1. line parsing
# --------------------------------------------------------------------------- #

# Odoo's default format is
#   %(asctime)s %(process)s %(levelname)s %(dbname)s %(name)s: %(message)s
# but deployments reorder or drop fields, use threads instead of processes, or
# front the line with a syslog/docker prefix. These are tried in order and the
# first that matches a given line wins; the parser reports how many lines each
# pattern claimed so you can tell if you are on an unexpected format.
_STAMP = r"(?P<date>\d{4}-\d\d-\d\d)[ T](?P<time>(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d)[,.]?(?P<ms>\d+)?\s+"
_DB = r"(?P<db>\S*[^\s:])"          # a database name never ends in ':' (that would be the logger)
HEADERS = [
    # 2026-09-13 22:00:09,711 2422355 WARNING db-name py.warnings: msg
    ("odoo", re.compile(
        r"^" + _STAMP + r"(?P<proc>\S+)\s+(?P<level>[A-Z]+)\s+" + _DB + r"\s+(?P<logger>[\w.\-]+):\s?(?P<msg>.*)$")),
    # same, without the dbname column
    ("odoo-nodb", re.compile(
        r"^" + _STAMP + r"(?P<proc>\S+)\s+(?P<level>[A-Z]+)\s+(?P<logger>[\w.\-]+):\s?(?P<msg>.*)$")),
    # a syslog/docker prefix in front of either of the above
    ("prefixed", re.compile(
        r"^.*?" + _STAMP + r"(?P<proc>\S+)\s+(?P<level>[A-Z]+)\s+(?:" + _DB + r"\s+)?(?P<logger>[\w.\-]+):\s?(?P<msg>.*)$")),
]

# The access line. Its shape changed between Odoo versions:
#   17.0 - 19.0   logger "werkzeug"          ip - - [14/Sep/2026 13:05:09] "GET /x HTTP/1.1" 200 - 91 0.412 0.663
#   19.4 - 20.0   logger "odoo.http.server"  ip SESSION - [14/Sep/2026 13:05:09] "GET /x HTTP/1.1" 200 7454 91 0.412 0.663 rw
# (the two tokens after the ip were always "- -"; from 19.4 the first is a short session id,
# the size is in bytes or "stream", and a cursor-mode flag - ro | rw | ro->rw | - - trails).
# The "<queries> <sql_seconds> <other_seconds>" triplet is what the whole tool is built on;
# lines without it (very old Odoo) are counted as untimed.
ACCESS = re.compile(
    r'^(?P<ip>\S+) (?P<ident>\S+) (?P<auth>\S+) \[[^\]]*\] "(?P<method>[A-Z]+) (?P<path>\S+)[^"]*"'
    r'\s+(?P<status>\d{3})\s+(?P<size>\S+)'
    r'(?:\s+(?P<nq>\d+)\s+(?P<qt>[\d.]+)\s+(?P<ot>[\d.]+))?(?:\s+(?P<mode>ro->rw|ro|rw|-))?')
ACCESS_LOGGERS = ("werkzeug", "odoo.http.server")
ANSI = re.compile(r"\x1b\[[0-9;]*m")                 # colored console logs

LOGIN_MSG = re.compile(r"^Login (?P<res>successful|failed) for login:(?P<login>\S+)")
SERVER_ACTION = re.compile(r"ir\.actions\.server\((\d+),\)|server action #(\d+)")
EXC_LINE = re.compile(r"^(?P<exc>(?:[a-zA-Z_][\w.]*\.)?[A-Z]\w*(?:Error|Exception|Denied|Warning|Fault))\b")

DATEISH = re.compile(r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d")
_CLASS_CACHE = {}       # (method, path) -> (family, detail); paths repeat a lot in real logs

ISSUE_LEVELS = ("WARNING", "ERROR", "CRITICAL")
_NORM_CACHE = {}        # message head -> normalized template
_REQ_CACHE = {}         # (method, path, 404?) -> (type, app, operation, collapsed): an access line's grouping

# Log lines that carry no timing of their own (Odoo's ORM call trace "call sale.order(5,).write({...})", INFO / DEBUG
# messages of add-ons). The worker that logged one was busy with a request or cron job, and that job's own line, logged
# when it ends, has the timing. Lines are matched to it by worker and time, like warnings and errors are.
TRACE_LEVELS = ("DEBUG", "INFO")
TRACE_PREFIX = "odoo.addons."
TRACE_CALL = re.compile(r"^call ([\w.]+)\([^)]*\)\.(\w+)")
TRACE_KEEP = 25             # distinct line shapes remembered per operation
TRACE_LINE = 400            # longest example line kept

_DIGITS = re.compile(r"\d+")
_HEX = re.compile(r"\b[0-9a-f]{8,}\b")

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
# no client and is left out.
# what the lines logged while a request / job ran name (users, records), told apart from its own details
F_LOGGED_USER, F_LOGGED_REC = "logged user id", "logged records"
LOGGED_OF = ((F_USER, F_LOGGED_USER), (F_REC, F_LOGGED_REC))
ISSUE_UNFILTERABLE = (F_LOGGED_USER, F_LOGGED_REC)    # a warning has no lines of its own
CLIENT_FACTS = (F_IP, F_SESSION)    # a warning's client: only known once the request that logged it ends


def _facts_ok(want, have):
    """Does one occurrence, carrying facts `have` ({detail: iterable of values}), satisfy filter `want`?"""
    for slot, vals in want.items():
        got = have.get(slot)
        if not got or vals.isdisjoint(got):
            return False
    return True


def _op_filter_ok(ff, fam, app, det, have):
    """Does one request / job of operation `det` pass every fact filter aimed at it, its app or its type?"""
    for by, name in (("operation", det), ("app", app), ("family", fam)):
        want = ff.get(by, {}).get(name)
        if want and not _facts_ok(want, have):
            return False
    return True


def request_facts(ip, session, path, collapsed):
    """The facts one access line carries ({detail: set of values}), for item filters. The URL is always there
    (a domain's URL is the raw path of every request); only the details shown keep it for collapsed paths."""
    have = {F_IP: {ip}, F_URL: {path[:FACT_LEN]}}
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


def extra_pred(a):
    """The compiled domain of what the app's own filters cannot hold (args.fact_filter["extra"]), or None.
    Only a key is kept on `a`: args and profiles travel between processes, a function cannot."""
    k = getattr(a, "_xkey", None)
    if k is None:
        ff = getattr(a, "fact_filter", None) or {}
        k = a._xkey = json.dumps(ff["extra"], sort_keys=True) if ff.get("extra") else ""
    if not k:
        return None
    f = _DOM_COMPILED.get(k)
    return f if f is not None else dom_compile(json.loads(k))


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


def _tsec(minute, tm, ms):
    """Seconds since the epoch (log clock) of a header's timestamp, with milliseconds."""
    return minute * 60 + int(tm[6:8]) + (float("0." + ms) if ms else 0.0)


def opener(path):
    """A text reader of a log: plain, .gz or .bz2 (by suffix), or "-" for stdin (left open when done)."""
    if path == "-":
        return contextlib.nullcontext(sys.stdin)
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    if path.endswith(".bz2"):
        return bz2.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "rt", encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# 2. classification: (method, path, status) -> (type, app, operation)
# --------------------------------------------------------------------------- #
#
# Three independent questions are answered for every request:
#
#   type       what mechanism is it?     ui.read, ui.action, api.write, report, assets, ...
#   app        which Odoo app owns it?   sale, stock, account, website_sale, ...
#   operation  the specific thing        "call_kw sale.order.action_confirm", "/shop/<slug>"
#
# Nothing here is tied to one database or one customer:
#   * the app of an RPC call is the module prefix of its model (sale.order -> sale),
#   * the app of a controller route is the module that owns the URL prefix,
#   * the operation keeps full detail; only ids, tokens and slugs are collapsed so one
#     page does not split into thousands of rows. Nothing is merged into a bigger bucket.

# call_kw / call_button methods, by what the user (or integration) is doing.
READ_METHODS = {
    "read", "search_read", "web_search_read", "web_read", "web_read_group", "read_group",
    "formatted_read_group", "formatted_read_grouping_sets", "search", "search_count",
    "name_search", "web_name_search", "name_get", "display_name", "fields_get",
    "search_panel_select_range", "search_panel_select_multi_range", "get_views", "load_views",
    "read_progress_bar", "web_resequence", "get_formview_id", "get_formview_action",
    "check_access_rights", "has_group", "read_recordset",
}
WRITE_METHODS = {
    "create", "write", "web_save", "unlink", "copy", "copy_data", "web_override_translations",
    "action_archive", "action_unarchive", "toggle_active", "resequence", "onchange_field",
}
ONCHANGE_METHODS = {"onchange", "onchange_form"}


def _split_model_method(s):
    """'account.move.web_read' -> ('account.move', 'web_read')"""
    if not s or "." not in s:
        return s or "?", ""
    model, _, method = s.rpartition(".")
    return model, method


BUTTONISH = re.compile(r"^(action_|button_|toggle_|do_)|_(confirm|validate|post|cancel|done|send|"
                       r"apply|process|reconcile|pay|invoice|print)$")
POSISH = re.compile(r"(^|_)pos(_|$)")                 # Odoo's naming for point-of-sale entry points


def app_of_model(model, method=""):
    """Odoo model names start with the name of the module that owns them."""
    if not model or model == "?":
        return "other"
    if model.startswith("x_"):
        return "studio-custom"                        # Studio-created models
    if POSISH.search(method or "") or model.startswith("pos."):
        return "pos"
    if model.startswith("res.partner"):
        return "contacts"
    head = model.split(".", 1)[0]
    return "base" if head in ("res", "ir") else head


def _rpc_type(model, method, button=False):
    if method in ONCHANGE_METHODS:
        return "onchange"
    if method in WRITE_METHODS:
        return "write"
    if model.startswith("base_import") or "import" in method:
        return "import"
    if "export" in method:
        return "export"
    is_report = model.endswith(".report") or ".report" in model or model.startswith("report.")
    if method in READ_METHODS:
        return "report" if is_report else "read"
    if "report" in method or is_report:
        return "report"
    if button or BUTTONISH.search(method):
        return "action"                               # workflow steps: confirm, validate, post, ...
    return "method"


def _rpc_kind(channel, t):
    return "%s.%s" % (channel, t) if t in ("read", "write", "onchange", "action", "method") else t


LANG_PREFIX = re.compile(r"^/[a-z]{2}(?:_[A-Z]{2})?(?=/)")
PROBE = re.compile(r"(^|/)(\.env|\.git|\.svn|\.aws|\.ssh|\.npmrc|\.htpasswd|\.pgpass|wp-|"
                   r"phpmyadmin|phpinfo|\.well-known|credentials|secrets?)|"
                   r"\.(php|zip|sql|tar|tgz|gz|bak|rar|7z|dump|tfstate|pem|jar|war|prisma|"
                   r"ya?ml|ini|conf|log)$", re.I)
STATIC_EXT = re.compile(r"\.(js|css|map|png|jpe?g|svg|gif|ico|woff2?|ttf|eot|webp|avif|mp4|webm)$", re.I)

# URL prefixes of Odoo's own controllers whose URL is not the module name.
# Anything not listed falls back to "the first path segment is the module".
URL_APP = {
    "shop": ("website_sale", "website"), "cart": ("website_sale", "website"),
    "my": ("portal", "website"), "payment": ("payment", "website"),
    "blog": ("website_blog", "website"), "forum": ("website_forum", "website"),
    "slides": ("website_slides", "website"), "event": ("website_event", "website"),
    "events": ("website_event", "website"), "jobs": ("website_hr_recruitment", "website"),
    "helpdesk": ("website_helpdesk", "website"), "appointment": ("appointment", "website"),
    "survey": ("survey", "website"), "contactus": ("website", "website"), "aboutus": ("website", "website"),
    "website": ("website", "website"), "page": ("website", "website"),
    "livechat": ("im_livechat", "messaging"), "im_livechat": ("im_livechat", "messaging"),
    "longpolling": ("bus", "messaging"), "websocket": ("bus", "messaging"), "bus": ("bus", "messaging"),
    "mail": ("mail", "messaging"), "discuss": ("mail", "messaging"),
    "pos": ("point_of_sale", "controller"), "pos-self": ("point_of_sale", "controller"),
    "pos_self_order": ("point_of_sale", "controller"), "pos-self-order": ("point_of_sale", "controller"),
    "saas_worker": ("hosting", "internal"), "_odoo": ("hosting", "internal"),
    "healthz": ("hosting", "internal"), "monitoring": ("hosting", "internal"),
}

_TOKEN = re.compile(r"^(?:[0-9a-f]{8,}|[A-Za-z0-9_\-]{24,})$")
_SLUG_ID = re.compile(r"^(.+?)-\d+$")


def _seg(s):
    if s.isdigit():
        return "<id>"
    if _TOKEN.match(s) and any(c.isdigit() for c in s):
        return "<token>"
    if _SLUG_ID.match(s):
        return "<slug>"
    return s


def _pattern(segs, depth=4):
    """['shop','my-chair-12'] -> '/shop/<slug>'. Ids, tokens and slugs collapse; deep paths are cut."""
    out = [_seg(s) for s in segs[:depth]]
    return "/" + "/".join(out) + ("/…" if len(segs) > depth else "")


def _module(seg):
    return seg.replace("-", "_") or "unknown"


def classify(method, path, status=""):
    """Return (type, app, operation). Never raises, never returns None."""
    p = path.split("?", 1)[0]
    frag = ""
    if "#" in p:
        p, _, frag = p.partition("#")

    # Odoo prefixes website URLs with the language code (/en_US/shop/...). Strip it so
    # the same page does not split into one bucket per installed language.
    lang = LANG_PREFIX.match(p)
    if lang and not p.startswith(("/web/", "/xmlrpc", "/jsonrpc", "/json/")):
        p = p[lang.end():] or "/"

    # Vulnerability scanners generate a long tail of junk paths.
    if PROBE.search(p):
        return "probe", "other", "probe %s" % (p.rsplit("/", 1)[-1] or p)[:30]

    segs = [s for s in p.split("/") if s]
    first = segs[0] if segs else ""
    tail = segs[-1] if segs else ""
    verb = "" if method in ("GET", "HEAD") else method + " "

    # ---- external API: the model and method travel after '#' in the logged path ------ #
    if first == "xmlrpc":
        svc = segs[-1] if len(segs) > 1 else ""
        if svc == "common":
            return "api.auth", "base", "xmlrpc %s" % (frag or "common")
        if svc == "db":
            return "api.method", "base", "xmlrpc db.%s" % (frag.split(".")[-1] or "?")
        if frag:
            model, meth = _split_model_method(frag)
            return _rpc_kind("api", _rpc_type(model, meth)), app_of_model(model, meth), "xmlrpc %s.%s" % (model, meth)
        return "api.method", "api", "xmlrpc %s" % (svc or "?")
    if first == "jsonrpc":
        if frag:
            model, meth = _split_model_method(frag)
            return _rpc_kind("api", _rpc_type(model, meth)), app_of_model(model, meth), "jsonrpc %s.%s" % (model, meth)
        return "api.method", "api", "jsonrpc"
    if first == "json":                                # Odoo 19 JSON-2: /json/2/<model>/<method>
        if len(segs) >= 4:
            return (_rpc_kind("api", _rpc_type(segs[2], segs[3])), app_of_model(segs[2], segs[3]),
                    "json2 %s.%s" % (segs[2], segs[3]))
        return "api.method", "api", "json2 %s" % _pattern(segs)

    # ---- the web client's RPC ----------------------------------------------------- #
    for prefix, name in (("/web/dataset/call_kw/", "call_kw"), ("/web/dataset/call_button/", "call_button")):
        if p.startswith(prefix):
            bits = p[len(prefix):].split("/")
            model = bits[0] if bits else "?"
            meth = bits[1] if len(bits) > 1 else ""
            if frag:                                   # Odoo appends #model.method
                fmodel, fmeth = _split_model_method(frag)
                model, meth = fmodel or model, fmeth or meth
            t = _rpc_type(model, meth, button=name == "call_button")
            return _rpc_kind("ui", t), app_of_model(model, meth), "%s %s.%s" % (name, model, meth)
    if p.startswith("/web/dataset/"):
        what = tail
        return ("ui.write" if what == "resequence" else "ui.read" if what == "search_read" else "ui.method",
                "web", "dataset %s" % what)

    # ---- sessions and authentication --------------------------------------------- #
    if p in ("/web/session/authenticate", "/web/session/impersonate") or p.startswith(
            ("/web/login", "/web/signup", "/web/reset_password")):
        return "auth", "web", "login %s" % p
    if p in ("/web/session/logout", "/web/session/destroy"):
        return "auth", "web", "logout"
    if first in ("auth_oauth", "auth_totp", "auth_signup", "auth_passkey"):
        return "auth", first, "login %s" % _pattern(segs, 2)

    # ---- web client shell, actions, menus ------------------------------------------ #
    if p == "/web/action/run":
        return "ui.action", "web", "run server action (/web/action/run)"
    if p.startswith("/web/action/"):
        return "ui.load", "web", "load action (/web/action/%s)" % tail
    if p.startswith(("/web/webclient/", "/web/session/", "/web/bundle", "/web/tests", "/web/domain/")):
        return "ui.load", "web", "webclient %s" % tail
    if p == "/odoo" or p.startswith("/odoo/"):
        return "ui.load", "web", "client shell /odoo/*"
    if p == "/web" or p.startswith("/web#"):
        return "ui.load", "web", "client shell /web"

    # ---- import / export -------------------------------------------------------------- #
    if first == "base_import" or "base_import" in frag:
        return "import", "base", "base_import %s" % (frag or p).rsplit(".", 1)[-1].rsplit("/", 1)[-1]
    if p.startswith("/web/export/") or p.endswith("/export_xlsx") or "/export/" in p:
        return "export", "web", "export %s" % tail

    # ---- reports: /report/<pdf|html|text>/<module.report_name>/<ids> ------------------- #
    if first == "report":
        kind = segs[1] if len(segs) > 1 else "?"
        name = segs[2] if len(segs) > 2 else "*"
        return "report", (name.split(".")[0] if "." in name else "report"), "report %s %s" % (kind, name)
    if first == "account_reports" or "account_report" in frag:
        return "report", "account_reports", "account report %s" % (frag or p)

    # ---- attachments, images, bundles, static files -------------------------------------- #
    if p.startswith("/web/assets"):
        return "assets", "web", "bundle %s" % tail
    if p.startswith(("/web/image", "/web/content", "/web/binary")):
        what = first and segs[1] if len(segs) > 1 else "content"
        if len(segs) > 3 and "." in segs[2] and segs[3].isdigit():
            model = segs[2]                                # /web/image/<model>/<id>/<field>
            field = segs[4] if len(segs) > 4 else ""
            return "assets", app_of_model(model), "%s %s%s" % (what, model, "." + field if field else "")
        return "assets", "web", "%s (attachment)" % what
    if STATIC_EXT.search(p) or p.startswith(("/favicon", "/robots.txt", "/sitemap")) or "/static/" in p:
        return "assets", (_module(first) if len(segs) > 1 else "website"), "static %s" % (first if len(segs) > 1 else tail)

    # ---- everything owned by a URL prefix ------------------------------------------------ #
    if first in URL_APP:
        app, kind = URL_APP[first]
        if kind == "website" and status == "404":
            kind = "not-found"
        return kind, app, verb + _pattern(segs)

    # ---- unknown routes stay attributable ------------------------------------------------ #
    if len(segs) >= 2:
        kind = "not-found" if status == "404" else "controller"
        return kind, _module(first), verb + _pattern(segs)
    if not segs or len(segs) == 1:                          # "/" or a single-segment CMS page slug
        if status == "404":
            return "not-found", "website", "%spage /*" % verb
        return "website", "website", "%spage %s" % (verb, "/" if not segs else "/*")
    return "other", "other", "%s %s" % (method, _pattern(segs))


# --------------------------------------------------------------------------- #
# 2b. background work that is logged as messages instead of access lines
# --------------------------------------------------------------------------- #
#
# Odoo logs no timing for server actions themselves (they run inside a request, a cron job
# or an automation rule and are already counted there), but it does log:
#   cron jobs         Job 'Mail: Email Queue Manager' (8) fully done (#loop 1; done 20; remaining 0; duration 0.62s)
#   automation rules  Automation Rule 'Notify' (12) done (on_write; 3/3 records; duration 0.532s)
#                     (at INFO only when slower than 0.1 s, WARNING above 1 s)
#   module loading    Module sale loaded in 1.20s, 2400 queries (+300 extra)
# cron, by version:
#   17.0        Starting job `Name`.            Job done: `Name` (0.007s).
#   18.0        Job 'Name' (8) starting         Job 'Name' (8) done in 0.017s   (once per loop iteration)
#   19.0 - 20.0 Job 'Name' (8) starting         Job 'Name' (8) fully done (#loop 1; done 20; remaining 0; duration 0.62s)
#               (status is "fully done", "partially done" or "failed")
CRON_17 = re.compile(r"^Job done: `(?P<name>.*)` \((?P<dur>[\d.]+)s\)")
CRON_18 = re.compile(r"^Job (?P<q>['\"])(?P<name>.*)(?P=q) \((?P<id>\d+)\) done in (?P<dur>[\d.]+)s")
CRON_19 = re.compile(r"^Job (?P<q>['\"])(?P<name>.*)(?P=q) \((?P<id>\d+)\) (?P<status>[a-z ]+?) "
                     r"\(#loop (?P<loops>\d+); done (?P<done>\d+); remaining (?P<rem>\d+); duration (?P<dur>[\d.]+)s\)")
# automation rules: 20.0 logs a duration (INFO above 0.1 s, WARNING above 1 s); 17.0 - 19.0 only log
# the start and the end of time-based rules, so those are paired by worker and rule name.
AUTOMATION_DONE = re.compile(r"^(?P<what>.+?) (?P<q>['\"])(?P<name>.*)(?P=q) \((?P<id>\d+)\) (?P<status>\w+) "
                             r"\((?P<trigger>[^;()]+); (?P<done>\d+)/(?P<total>\d+) records; duration (?P<dur>[\d.]+)s\)")
AUTO_START = re.compile(r"^Starting time-based automation rule `(?P<name>.*)`\.")
AUTO_END = re.compile(r"^Time-based automation rule `(?P<name>.*)` done\.")
# module loading: "762 modules loaded in 7.38s, 0 queries (+0 extra)" is logged by every version;
# "Module sale loaded in 1.20s, 2400 queries" only exists at DEBUG level and is not used (it would double count).
MODULES_LOADED = re.compile(r"^(?P<n>\d+) modules loaded in (?P<dur>[\d.]+)s, (?P<nq>\d+) queries")
JOB_KINDS = ("cron", "automation", "module-load")
JOB_LOGGERS = ("ir_cron", "base_automation", "modules.loading")


def _app_from_label(name):
    """'Google Calendar: synchronization' -> 'google_calendar' (Odoo names crons '<App>: <what>')."""
    head = name.split(":", 1)[0] if ":" in name else ""
    return re.sub(r"[^a-z0-9]+", "_", head.strip().lower()).strip("_") or "cron"


def classify_job(logger, msg):
    """(type, app, operation, seconds, queries, format, facts) for a timed background-work message, else None.
    facts: the (detail, value) pairs the operation name leaves out, such as the cron id and how the run ended."""
    if logger.endswith("ir_cron"):
        for rx, fmt in ((CRON_19, "cron 19+"), (CRON_18, "cron 18"), (CRON_17, "cron 17")):
            m = rx.match(msg)
            if m:
                name, g = m.group("name"), m.groupdict()
                facts = [("cron id", g["id"])] if g.get("id") else []
                if g.get("status"):
                    facts += [("status", g["status"]), ("remaining", g["rem"])]
                return "cron", _app_from_label(name), "cron: %s" % name, float(m.group("dur")), 0, fmt, facts
    elif logger.endswith("base_automation"):
        m = AUTOMATION_DONE.match(msg)
        if m:
            return ("automation", "base_automation", "automation: %s (%s)" % (m.group("name"), m.group("trigger").strip()),
                    float(m.group("dur")), 0, "automation 20 (duration)",
                    [("rule id", m.group("id")), ("status", m.group("status")),
                     ("records processed", "%s/%s" % (m.group("done"), m.group("total")))])
    elif logger.endswith("modules.loading"):
        m = MODULES_LOADED.match(msg)
        if m:
            return ("module-load", "base", "load modules", float(m.group("dur")), int(m.group("nq")), "modules loaded",
                    [("modules", m.group("n"))])
    return None


# --------------------------------------------------------------------------- #
# 3. event classification for the non-request lines
# --------------------------------------------------------------------------- #

LOGGER_FAMILIES = (
    ("odoo.addons.base.models.res_users", "auth"),
    ("odoo.addons.base.models.ir_rule", "access-rules"),
    ("odoo.addons.base.models.ir_cron", "cron"),
    ("odoo.addons.base.models.ir_model", "orm-schema"),
    ("odoo.addons.base.models.ir_attachment", "attachments"),
    ("odoo.addons.base.models.ir_actions_report", "report-engine"),
    ("odoo.addons.base_automation", "automation-rules"),
    ("odoo.addons.mail", "mail"),
    ("odoo.addons.bus", "bus"),
    ("odoo.addons.saas_worker", "hosting-internal"),
    ("odoo.addons.iap", "integration"),
    ("odoo.modules", "module-loading"),
    ("odoo.registry", "registry"),
    ("odoo.schema", "orm-schema"),
    ("odoo.api", "orm-recompute"),
    ("odoo.fields", "orm-fields"),
    ("odoo.domains", "orm-domains"),
    ("odoo.sql_db", "database"),
    ("odoo.models", "orm"),
    ("odoo.http", "http-errors"),
    ("py.warnings", "deprecations"),
    ("longpolling", "bus"),
    ("werkzeug", "requests"),
)


def event_family(logger):
    for prefix, fam in LOGGER_FAMILIES:
        if logger == prefix or logger.startswith(prefix + "."):
            return fam
    if logger.startswith("odoo.addons."):
        return "addon:" + logger.split(".")[2]
    return logger.split(".")[0] or "unknown"


def normalize(msg, limit=140):
    """Collapse a message into a template so variants group together."""
    m = _HEX.sub("<hex>", msg[:limit])
    m = _DIGITS.sub("#", m)
    return " ".join(m.split())


# --------------------------------------------------------------------------- #
# 4. accumulator
# --------------------------------------------------------------------------- #

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


_UNIX_DAY0 = 719163         # date(1970, 1, 1).toordinal()
_DAYS = {}                  # "2026-09-14" -> minutes since the unix epoch at 00:00 (log clock, no tz)


def stamp_minute(date, time):
    """Minutes since 1970-01-01 of a log timestamp (naive: the log's own clock)."""
    base = _DAYS.get(date)
    if base is None:
        y, m, d = date.split("-")
        base = _DAYS[date] = (datetime.date(int(y), int(m), int(d)).toordinal() - _UNIX_DAY0) * 1440
    return base + int(time[:2]) * 60 + int(time[3:5])


def _norm_head(msg):
    """The normalized template of a warning / error message, the third part of its issue key (memoized)."""
    head = msg[:140]
    tpl = _NORM_CACHE.get(head)
    if tpl is None:
        if len(_NORM_CACHE) > 100000:
            _NORM_CACHE.clear()
        tpl = _NORM_CACHE[head] = normalize(head)
    return tpl


def _thin(sample, k):
    """At most `k` of a percentile sample, evenly strided: it keeps the spread over time and the distribution
    the same as a random subset would, at a fraction of the cost (a merge thins every operation's sample).
    Bucket.merge weighs the two samples it joins before thinning, so the result still stands for every call."""
    step = -(-len(sample) // k)
    return array.array("d", sample[::step])


class Bucket:
    __slots__ = ("n", "sql", "py", "unsplit", "nq", "mx", "errors", "sample", "seen", "hist", "st", "pc", "ex")

    RESERVOIR = 20000
    LINE_MAX = 400          # longest example log line kept

    def hist_add(self, minute, sql, py, extra=0.0, count=1):
        """Per-minute [requests, sql s, python s, unsplit s]: when the work happened."""
        e = self.hist.get(minute)
        if e is None:
            self.hist[minute] = [count, sql, py, extra]
        else:
            e[0] += count
            e[1] += sql
            e[2] += py
            e[3] += extra

    def __init__(self):
        self.hist = {}
        self.st = {}          # HTTP status -> count
        self.pc = None        # memo of the last percentile: (seen, p, value)
        self.n = 0
        self.sql = 0.0
        self.py = 0.0
        self.unsplit = 0.0    # seconds with no SQL/Python split (jobs logged as messages)
        self.nq = 0
        self.mx = 0.0
        self.errors = 0
        self.sample = array.array("d")   # reservoir of total times, for percentiles (8 bytes each, not a float object)
        self.seen = 0
        self.ex = None        # example log lines: {"first": (stamp, line), "slow": (seconds, stamp, line), "err": (stamp, line)}

    def note(self, stamp, raw, t, is_err):
        """Keep real log lines of this operation to show as examples: the first one, the slowest and the first failing one."""
        ex = self.ex
        if ex is None:
            line = raw.rstrip()[:self.LINE_MAX]
            self.ex = {"first": (stamp, line), "slow": (t, stamp, line)}
            if is_err:
                self.ex["err"] = (stamp, line)
            return
        slow = t > ex["slow"][0]
        if slow or (is_err and "err" not in ex):
            line = raw.rstrip()[:self.LINE_MAX]
            if slow:
                ex["slow"] = (t, stamp, line)
            if is_err and "err" not in ex:
                ex["err"] = (stamp, line)

    def merge(self, o, rng, full=True):
        """Fold another bucket in. full=False skips the per-minute and per-status detail
        (used for roll-ups such as types and apps, which never show it)."""
        self.n += o.n
        self.sql += o.sql
        self.py += o.py
        self.unsplit += o.unsplit
        self.nq += o.nq
        self.errors += o.errors
        mine_seen, self.seen = self.seen, self.seen + o.seen
        self.mx = max(self.mx, o.mx)
        if full:
            for k, v in o.st.items():
                self.st[k] = self.st.get(k, 0) + v
            mine, h = self.hist, o.hist
            if isinstance(h, PackedHist):                # (the usual case: a parsed chunk's) read in place
                vs = h.vals
                for i, k in enumerate(h.keys):
                    j = 4 * i
                    e = mine.get(k)
                    if e is None:
                        mine[k] = [vs[j], vs[j + 1], vs[j + 2], vs[j + 3]]
                    else:
                        e[0] += vs[j]
                        e[1] += vs[j + 1]
                        e[2] += vs[j + 2]
                        e[3] += vs[j + 3]
            else:
                for k, v in h.items():
                    e = mine.get(k)
                    if e is None:
                        mine[k] = list(v)
                    else:
                        e[0] += v[0]
                        e[1] += v[1]
                        e[2] += v[2]
                        e[3] += v[3]
        if o.ex:
            mine = self.ex
            if mine is None:
                self.ex = dict(o.ex)
            else:
                for k, v in o.ex.items():
                    cur = mine.get(k)
                    if cur is None or (v[0] > cur[0] if k == "slow" else v[0] < cur[0]):   # slowest wins; first / err: the earliest stamp
                        mine[k] = v
        # Each sample value stands for seen / len(sample) calls. Where the two sides differ (one of them hit the
        # reservoir cap), the side whose values stand for fewer calls is thinned first, so a big chunk's calls
        # weigh in the percentiles as much as they did in the log (as combine() does for roll-ups).
        theirs = o.sample
        if self.sample and theirs and mine_seen and o.seen:
            if mine_seen * len(theirs) < o.seen * len(self.sample):
                k = max(1, mine_seen * len(theirs) // o.seen)
                if len(self.sample) > k:
                    self.sample = _thin(self.sample, k)
            elif o.seen * len(self.sample) < mine_seen * len(theirs):
                k = max(1, o.seen * len(self.sample) // mine_seen)
                if len(theirs) > k:
                    theirs = _thin(theirs, k)
        self.sample.extend(theirs)                   # in place; thin out only when it has doubled, so merges stay cheap
        if len(self.sample) > 2 * self.RESERVOIR:
            self.sample = _thin(self.sample, self.RESERVOIR)

    @staticmethod
    def combine(buckets, rng):
        """One roll-up bucket over many (e.g. every operation of a type). The percentile sample takes a
        share from each bucket in proportion to how many calls it saw."""
        out = Bucket()
        total_seen = sum(b.seen for b in buckets)
        want = Bucket.RESERVOIR
        samples = []
        for b in buckets:
            out.n += b.n
            out.sql += b.sql
            out.py += b.py
            out.unsplit += b.unsplit
            out.nq += b.nq
            out.errors += b.errors
            out.seen += b.seen
            if b.mx > out.mx:
                out.mx = b.mx
            s = b.sample
            if total_seen > want:
                k = max(1, want * b.seen // total_seen)
                if len(s) > k:
                    s = _thin(s, k)
            samples.extend(s)
        out.sample = array.array("d", samples)
        return out

    def add(self, sql, py, nq, is_error, rng, extra=0.0):
        t = sql + py + extra
        self.n += 1
        self.sql += sql
        self.py += py
        self.unsplit += extra
        self.nq += nq
        if t > self.mx:
            self.mx = t
        if is_error:
            self.errors += 1
        self.seen += 1
        if len(self.sample) < self.RESERVOIR:
            self.sample.append(t)
        else:                                    # reservoir sampling
            j = rng.randrange(self.seen)
            if j < self.RESERVOIR:
                self.sample[j] = t

    @property
    def total(self):
        return self.sql + self.py + self.unsplit

    def pct(self, p):
        if not self.sample:
            return 0.0
        c = self.pc
        if c is not None and c[0] == self.seen and c[1] == p:
            return c[2]
        s = sorted(self.sample)
        v = s[min(len(s) - 1, int(len(s) * p / 100.0))]
        self.pc = (self.seen, p, v)
        return v


class Profile:
    def __init__(self, args):
        self.a = args
        self._ff = getattr(args, "fact_filter", None) or None    # item filters / custom domain (see _facts_ok), read per request
        self.rng = random.Random(12345)
        self.fam = defaultdict(Bucket)           # by type   (ui.read, api.write, cron, ...)
        self.app = defaultdict(Bucket)           # by app    (sale, stock, website_sale, ...)
        self.det = defaultdict(Bucket)           # by operation
        self.det_fam = {}                        # operation -> type
        self.det_app = {}                        # operation -> app
        self.events = defaultdict(Counter)       # family -> Counter[template]
        self.event_level = {}
        self.exceptions = Counter()
        self.server_actions = Counter()
        self.logins = Counter()                  # (login, ip, result) -> n
        self.status = Counter()
        self.hours = defaultdict(Bucket)
        self.unclassified = Counter()
        self.header_hits = Counter()
        self.dbs = Counter()                     # database name -> log lines
        self.lines = 0
        self.header_ok = 0
        self.continuation = 0
        self.access_ok = 0
        self.access_untimed = 0
        self.access_bad = 0
        self.held_n = 0                          # connections held open (WebSocket upgrades): not work
        self.held_s = 0.0
        self.filtered = 0
        self.t_min = None
        self.t_max = None
        self._pending_login = {}                 # pid -> (result, login)
        self._expect_exc = None                  # the profile whose ERROR line waits for its traceback's exception class
        self.index = {}                          # profile_files: path -> [(byte offset, minute)]
        self.parts = {}                          # split_db: database -> child Profile
        self._cur = None                         # split_db: child that owns continuation lines
        self._skip = bool(args.since or args.until)   # the last header was outside the time window (lines before the first one are)
        self.formats = Counter()                 # which Odoo log-format variants were recognised
        self._starts = {}                        # (worker, rule) -> start time, for time-based automation pairing
        self.levels = Counter()                  # WARNING / ERROR / CRITICAL -> log lines
        self.issues = {}                         # (level, logger, template) -> [n, first, last, sample, exception,
                                                  #  src, hist]; hist is {minute: [n, 0.0, 0.0, 0.0]}, same shape
                                                  # as a Bucket's (see hist_add) so _rebin() reads either kind
        self._last_issue = None                  # ERROR record still waiting for its traceback's exception class
        self.issue_ops = {}                      # issue key -> {operation: count}: which request was running when it was logged
        self._pend = {}                          # worker -> [(time, profile, issue key)] logged since that worker's last request ended
        self._lpend = {}                         # worker -> [(time, profile, message, line)]: untimed lines, same idea
        self.src = {}                            # operation -> {line shape: [n, example line, runs, seconds]}: untimed lines
                                                  # logged while it ran; runs / seconds: the requests / jobs that logged it
        self.op_facts = {}                       # operation -> facts (see _fact): client IPs, sessions, raw URLs, cron ids, ...
        self.op_logged = {}                      # operation -> facts of the lines logged while it ran (see _logged_facts):
                                                  # how many of its requests named each user / record
        self.op_facts_s = {}                     # operation -> twin of op_facts / op_logged (see _fact_time): the seconds
        self.op_logged_s = {}                    # of the requests / jobs behind each value, for their average
        self._iocc = None                        # facts of the warning being read, still growing with its traceback
        self.issue_facts = {}                    # issue key -> facts: the numbers behind '#', users, records, clients
        self._ifact = None                       # (profile, issue key, lines left): continuation lines still searched for facts
        self._ihold = None                       # a warning / error held back by a fact filter until its continuation lines are read
        self.clients = {}                        # client -> {minute: [requests, seconds]}: who was active when
        self.login_seen = {}                     # (login, ip, result) -> [first stamp, last stamp]
        self.all_levels = Counter()              # every level (DEBUG, INFO, ...) -> header lines: what the log can show at all

    # -- helpers ---------------------------------------------------------- #
    def _part(self, db):
        p = self.parts.get(db)
        if p is None:
            p = self.parts[db] = Profile(self.a)
        return p

    def _in_window(self, stamp):
        if self.a.since and stamp < self.a.since:
            return False
        if self.a.until and stamp > self.a.until:
            return False
        return True

    def finalize(self):
        """Roll the operation buckets up into types and apps and total the HTTP statuses. Doing it once
        here, instead of on every request, is what keeps parsing fast. Call after the last feed / merge."""
        by_type, by_app = defaultdict(list), defaultdict(list)
        status = Counter()
        for op, b in self.det.items():
            by_type[self.det_fam.get(op, "other")].append(b)
            by_app[self.det_app.get(op, "other")].append(b)
            status.update(b.st)
        self.fam = defaultdict(Bucket, {k: Bucket.combine(v, self.rng) for k, v in by_type.items()})
        self.app = defaultdict(Bucket, {k: Bucket.combine(v, self.rng) for k, v in by_app.items()})
        self.status = status
        return self

    # -- main loop -------------------------------------------------------- #
    def merge(self, o, heavy=True):
        """Fold another Profile (e.g. from a parallel worker) into this one. heavy=False leaves out what only an
        item's details and the export read (op_facts, op_logged, src, clients): see merge_heavy."""
        for mine, theirs in ((self.det, o.det), (self.hours, o.hours)):
            for k, b in theirs.items():
                mine[k].merge(b, self.rng)
        self.det_fam.update(o.det_fam)
        self.det_app.update(o.det_app)
        for name in ("exceptions", "server_actions", "logins", "unclassified",
                     "header_hits", "dbs", "levels", "formats", "all_levels"):
            getattr(self, name).update(getattr(o, name))
        for fam, c in o.events.items():
            self.events[fam].update(c)
        self.event_level.update(o.event_level)
        for name in ("lines", "header_ok", "continuation", "access_ok", "access_untimed",
                     "access_bad", "held_n", "held_s", "filtered"):
            setattr(self, name, getattr(self, name) + getattr(o, name))
        if o.t_min is not None and (self.t_min is None or o.t_min < self.t_min):
            self.t_min = o.t_min
        if o.t_max is not None and (self.t_max is None or o.t_max > self.t_max):
            self.t_max = o.t_max
        for k, d in o.issue_ops.items():
            mine = self.issue_ops.setdefault(k, {})
            for op, n in d.items():
                mine[op] = mine.get(op, 0) + n
        for k, f in o.issue_facts.items():
            merge_facts(self.issue_facts.setdefault(k, {}), f)
        if heavy:
            self.merge_heavy(o)
        for k, (t0, t1) in o.login_seen.items():
            e = self.login_seen.get(k)
            if e is None:
                self.login_seen[k] = [t0, t1]
            else:
                e[0], e[1] = min(e[0], t0), max(e[1], t1)
        for k, e in o.issues.items():
            mine = self.issues.get(k)
            if mine is None:
                self.issues[k] = list(e[:6]) + [{m: list(v) for m, v in e[6].items()}]   # deep: mutated in place below
            else:
                mine[0] += e[0]
                if e[1] < mine[1]:
                    mine[1] = e[1]
                if e[2] > mine[2]:
                    mine[2] = e[2]
                mine[4] = mine[4] or e[4]
                mine[5] = mine[5] or e[5]
                for m, v in e[6].items():
                    h = mine[6].get(m)
                    if h is None:
                        mine[6][m] = list(v)
                    else:
                        h[0] += v[0]
        for db, child in o.parts.items():          # never adopt `child` itself: it may be cached and must stay as it is
            self._part(db).merge(child, heavy)

    def merge_heavy(self, o):
        """The part of merge() that only an item's details and the export read: what was collapsed behind each
        operation, what its lines named, the lines logged while it ran, and who was active when."""
        for op, d in o.src.items():
            mine = self.src.setdefault(op, {})
            for k, e in d.items():
                cur = mine.get(k)
                if cur is None:
                    mine[k] = list(e)
                else:
                    cur[0] += e[0]
                    cur[1] = cur[1] or e[1]
                    cur[2] += e[2]
                    cur[3] += e[3]
        for mine_d, theirs_d, mine_t, theirs_t in ((self.op_facts, o.op_facts, self.op_facts_s, o.op_facts_s),
                                                   (self.op_logged, o.op_logged, self.op_logged_s, o.op_logged_s)):
            for k, f in theirs_d.items():
                merge_facts(mine_d.setdefault(k, {}), f)
                merge_fact_times(mine_d[k], mine_t.setdefault(k, {}), theirs_t.get(k) or {})
        for c, h in o.clients.items():
            mine = self.clients.setdefault(c, {})
            for m, v in h.items():
                e = mine.get(m)
                if e is None:
                    mine[m] = list(v)
                else:
                    e[0] += v[0]
                    e[1] += v[1]

    def feed(self, fh):
        """Read log lines. With args.split_db every database gets its own child Profile
        in self.parts, so any subset of databases can be merged later without re-reading."""
        a = self.a
        want_events = a.events or a.logins      # event lines are only ever reported with these
        want_hours = a.hours
        exclude = getattr(a, "exclude_db", None)
        only = getattr(a, "only_db", None)
        split = getattr(a, "split_db", False)
        pend = self._pend
        lpend = self._lpend
        ff_issue = (getattr(a, "fact_filter", None) or {}).get("issue")
        # with any filter, a warning is decided with its traceback and with the request it was logged during: one
        # logged while a filtered-out request ran goes with it (see _claim / _drop_claims)
        hold_all = bool(getattr(a, "fact_filter", None))
        for raw in fh:
            self.lines += 1
            if "\x1b" in raw:
                raw = ANSI.sub("", raw)
            rec = None
            hname = None
            # Cheap reject for traceback/continuation lines (often >half of a real log):
            # every header format needs a leading digit or a timestamp somewhere in the line.
            if raw[:1].isdigit() or DATEISH.search(raw, 0, 200):
                for name, pat in HEADERS:
                    m = pat.match(raw)
                    if m:
                        rec = m
                        hname = name
                        break
                if rec is not None:
                    try:
                        minute = stamp_minute(rec.group("date"), rec.group("time"))
                    except ValueError:                 # 2026-02-31 and friends: not a real timestamp
                        rec = None
            if rec is None:
                # traceback / continuation / blank line
                if self._skip:                     # belongs to a header outside the time window
                    continue
                if split:                          # belongs to the database of the last header
                    c = self._cur or self._part("-")
                    c.lines += 1
                    c.continuation += 1
                else:
                    self.continuation += 1
                if self._ihold is not None:
                    hold = self._ihold
                    if len(hold[7]) < ISSUE_MORE_LINES:
                        hold[7].append(raw)
                    if hold[1] != "WARNING":           # what names an ERROR can come later than that
                        found = hold[8]
                        if not found["sa"] and "ir.actions.server" in raw:
                            sa = SERVER_ACTION.search(raw)
                            if sa:
                                found["sa"] = "server action #%s" % (sa.group(1) or sa.group(2))
                        if not found["exc"]:
                            em = EXC_LINE.match(raw.strip())
                            if em:
                                found["exc"] = em.group("exc")
                if self._ifact is not None:            # the body / traceback of a warning or error: users, records
                    t, k, left = self._ifact
                    _detail_facts(t.issue_facts.setdefault(k, {}), raw)
                    if self._iocc is not None:
                        _detail_facts(self._iocc, raw)
                    self._ifact = (t, k, left - 1) if left > 1 else None
                if self._last_issue is not None and not self._last_issue[5] and "ir.actions.server" in raw:
                    sa = SERVER_ACTION.search(raw)         # traceback frame: File "ir.actions.server(12,)"
                    if sa:
                        self._last_issue[5] = "server action #%s" % (sa.group(1) or sa.group(2))
                if self._expect_exc is not None or self._last_issue is not None:
                    em = EXC_LINE.match(raw.strip())
                    if em:
                        if self._expect_exc is not None:
                            self._expect_exc.exceptions[em.group("exc")] += 1
                            self._expect_exc = None
                        if self._last_issue is not None:       # name the ERROR after its exception
                            self._last_issue[4] = self._last_issue[4] or em.group("exc")
                            self._last_issue = None
                continue

            if hname == "odoo-nodb":
                date, tm, ms, proc, level, logger, msg = rec.groups()
                db = None
            else:
                date, tm, ms, proc, level, db, logger, msg = rec.groups()
            self._last_issue = self._ifact = self._iocc = self._expect_exc = None
            if self._ihold is not None:
                self._release_issue()
            db = db or "-"
            if db == "?" or db == "None":          # Odoo prints these when a line has no database
                db = "-"
            if only and db != only and db != "-":  # another database: this line and its continuation lines are not read at all
                self._skip = True
                self.filtered += 1
                continue
            tgt = self
            if split:
                tgt = self._cur = self._part(db)
            stamp = date + "T" + tm
            self._skip = False
            if a.since or a.until:
                if not self._in_window(stamp):
                    tgt.filtered += 1
                    self._skip = True                  # its continuation lines are outside the window too
                    continue
            if split:
                tgt.lines += 1
            tgt.header_ok += 1
            hh = tgt.header_hits
            hh[hname] = hh.get(hname, 0) + 1
            if db != "-":
                tgt.dbs[db] += 1
            if (a.db and db != a.db) or (exclude and db in exclude):
                tgt.filtered += 1
                continue
            if tgt.t_min is None or stamp < tgt.t_min:
                tgt.t_min = stamp
            if tgt.t_max is None or stamp > tgt.t_max:
                tgt.t_max = stamp
            pid = proc or "?"
            al = tgt.all_levels
            al[level] = al.get(level, 0) + 1
            if level in ISSUE_LEVELS and (hold_all or ff_issue and (level, logger, _norm_head(msg)) in ff_issue):
                # filtered: whether it counts depends on its traceback too, so decide at the next header
                self._ihold = (tgt, level, logger, msg, stamp, pid, _tsec(minute, tm, ms), [], {"exc": "", "sa": ""})
            elif level in ISSUE_LEVELS:
                ikey, e = tgt._issue(level, logger, msg, stamp)
                self._ifact = (tgt, ikey, ISSUE_MORE_LINES)
                if level != "WARNING":
                    self._last_issue = e
                # Remember who logged it and when: the access line of the request this worker was running
                # is logged when the request ends, so it can claim everything logged during its lifetime.
                occ = self._iocc = {}                       # what this one names: the request it ran in inherits it
                _detail_facts(occ, msg[:2000])
                pl = pend.setdefault(pid, [])
                pl.append((_tsec(minute, tm, ms), tgt, ikey, occ))
                if len(pl) > 200:
                    del pl[:100]

            if logger in ACCESS_LOGGERS or logger.endswith(".werkzeug"):
                f = tgt.formats
                fk = "access: odoo.http.server (19.4+)" if logger == "odoo.http.server" else "access: werkzeug (17.0-19.0)"
                f[fk] = f.get(fk, 0) + 1
                tgt._request(msg, tm[:2] if want_hours else None, pid, minute,
                             _tsec(minute, tm, ms) if pend.get(pid) or lpend.get(pid) else 0.0, pend, lpend, raw, stamp)
                continue

            if logger.endswith(JOB_LOGGERS):                # cron / automation / module loading
                job = classify_job(logger, msg)
                if job:
                    tgt.formats[job[5]] += 1
                    tgt._job(job[:5], minute, pid, _tsec(minute, tm, ms) if pend.get(pid) or lpend.get(pid) else 0.0,
                             pend, lpend, raw, stamp, job[6])
                    continue
                if logger.endswith("base_automation"):      # time-based rules: pair "Starting" with "done"
                    tgt._automation_pair(msg, pid, _tsec(minute, tm, ms), minute)

            if level in TRACE_LEVELS and (logger == "odoo.api" or logger.startswith(TRACE_PREFIX)):
                pl = lpend.setdefault(pid, [])                 # waits for the request / job this worker is running
                pl.append((_tsec(minute, tm, ms), tgt, msg, raw))
                if len(pl) > 100:
                    del pl[:50]

            if want_events:
                tgt._event(logger, level, msg, pid)
                if level == "ERROR":
                    self._expect_exc = tgt
            elif msg.startswith("Login ") and logger.endswith("res_users"):   # pairs the account with the access line's IP
                lm = LOGIN_MSG.match(msg)
                if lm:
                    tgt._pending_login[pid] = (lm.group("res"), lm.group("login"))
        if self._ihold is not None:
            self._release_issue()
        for lst in pend.values():                   # never claimed: logged outside any request of this piece
            for e in lst:
                self._settle(e, False)
            lst.clear()

    def _settle(self, e, during):
        """A deferred warning (pending entry with no key yet) that no request will keep or drop any more:
        `during` False -- logged outside the run that ended now, so counted with no parent unless it is filtered
        by its client (then it has none, and is left out)."""
        if e[2] is None:
            level, logger, msg, stamp, more, client, found = e[4]
            if client or during:
                e[1].filtered += 1
            else:
                e[1]._count_issue(level, logger, msg, stamp, more, found)

    def _release_issue(self):
        """A warning / error held back by a fact filter, now that its continuation lines are read: counted
        exactly as it would have been without the filter if its facts pass, else dropped."""
        tgt, level, logger, msg, stamp, pid, t, more, found = self._ihold
        self._ihold = None
        tpl = _norm_head(msg)
        want = self.a.fact_filter.get("issue", {}).get((level, logger, tpl)) or {}
        client = {k: v for k, v in want.items() if k in CLIENT_FACTS}
        own = {k: v for k, v in want.items() if k not in CLIENT_FACTS}
        xp = extra_pred(self.a)
        have = issue_occurrence_facts(msg, more)
        pl = self._pend.setdefault(pid, [])
        if (own and not _facts_ok(own, have)) or (xp and not xp(issue_fields(level, logger, tpl, msg, have))):
            tgt.filtered += 1
            pl.append((t, tgt, False, have))            # not counted, but its request still logged what it names
        else:
            pl.append((t, tgt, None, have, (level, logger, msg, stamp, more, client, found)))   # decided by its request (_claim)
        if len(pl) > 200:
            for e in pl[:100]:
                self._settle(e, False)
            del pl[:100]

    def _count_issue(self, level, logger, msg, stamp, more, found=None):
        """Count one warning / error whose continuation lines `more` are known; returns its key. found: the
        exception class / server action already read from a traceback longer than `more`."""
        key, e = self._issue(level, logger, msg, stamp)
        if found:
            e[4] = e[4] or found["exc"]
            e[5] = e[5] or found["sa"]
        f = self.issue_facts.setdefault(key, {})
        for raw in more:
            _detail_facts(f, raw)
            if level != "WARNING" and not e[5] and "ir.actions.server" in raw:
                sa = SERVER_ACTION.search(raw)
                if sa:
                    e[5] = "server action #%s" % (sa.group(1) or sa.group(2))
        if level != "WARNING" and not e[4]:
            for raw in more:
                em = EXC_LINE.match(raw.strip())
                if em:
                    e[4] = em.group("exc")
                    break
        return key

    def _logged_facts(self, pid, t_end, span, pend, lpend):
        """The users and records named in what `pid` logged during the last `span` seconds -- the warnings
        (with their tracebacks) and untimed lines (ORM calls, add-on messages) the request / job ending now
        is about to claim (see _claim) -- as {F_LOGGED_USER | F_LOGGED_REC: set}."""
        out = {}
        lo = t_end - span - 1.0
        for e in (pend.get(pid) or ()) if pend else ():
            if e[0] >= lo and e[3]:
                for mine, name in LOGGED_OF:
                    vals = e[3].get(mine)
                    if vals:
                        out.setdefault(name, set()).update(v for v in vals if v != FACT_OTHER)
        for ts, _c, msg, _raw in (lpend.get(pid) or ()) if lpend else ():
            if ts >= lo and ("id" in msg or "(" in msg or "user" in msg or "User" in msg):
                d = {}
                _detail_facts(d, msg)
                for mine, name in LOGGED_OF:
                    if d.get(mine):
                        out.setdefault(name, set()).update(d[mine])
        return out

    def _count_logged(self, det, logged, seconds):
        if logged:
            f, t = self.op_logged.setdefault(det, {}), self.op_logged_s.setdefault(det, {})
            for slot, vals in logged.items():
                for v in vals:
                    _fact_time(f, t, slot, v, seconds)

    def _drop_claims(self, pid, pend, lpend, t_end=0.0, span=0.0):
        """A request / job left out by a filter: what its worker logged while running it goes with it (and is
        not claimed by the worker's next request instead); what was logged before it is counted parentless."""
        lo = t_end - span - 1.0
        for e in (pend.get(pid) or ()) if pend else ():
            self._settle(e, e[0] >= lo)
        for d in (pend, lpend):
            lst = d.get(pid) if d else None
            if lst:
                lst.clear()

    # -- cron jobs, automation rules, module loading --------------------------- #
    def _automation_pair(self, msg, pid, t, minute):
        m = AUTO_START.match(msg)
        if m:
            self._starts[(pid, m.group("name"))] = t
            return
        m = AUTO_END.match(msg)
        if m:
            start = self._starts.pop((pid, m.group("name")), None)
            if start is not None and t >= start:
                self.formats["automation 17-19 (start/done paired)"] += 1
                self._job(("automation", "base_automation", "automation: %s (time-based)" % m.group("name"),
                           t - start, 0), minute)

    def _claim(self, pid, det, t_end, span, pend, lpend=None, ip=None, session="-", seconds=None):
        """Attribute the warnings / errors and the untimed log lines `pid` logged during the last `span`
        seconds to operation `det` (and, for a request, to its client). seconds: the time booked for that
        request / job (span when not given), counted once per line shape it logged."""
        lst = pend.get(pid) if pend else None
        if lst:
            lo = t_end - span - 1.0
            for e in lst:
                ts, child, ikey = e[0], e[1], e[2]
                if ikey is False:                   # left out by a filter: only named things (see _logged_facts)
                    continue
                if ikey is None:                    # held by a filter: this request decides it now
                    if ts < lo:
                        self._settle(e, False)
                        continue
                    level, logger, msg, stamp, more, client, found = e[4]
                    have = {F_IP: {ip}} if ip else {}
                    if ip and session != "-":
                        have[F_SESSION] = {session}
                    if client and not _facts_ok(client, have):
                        child.filtered += 1
                        continue
                    ikey = child._count_issue(level, logger, msg, stamp, more, found)
                if ts >= lo:
                    ops = child.issue_ops.setdefault(ikey, {})
                    ops[det] = ops.get(det, 0) + 1
                    if ip:
                        f = child.issue_facts.setdefault(ikey, {})
                        _fact(f, F_IP, ip)
                        if session != "-":
                            _fact(f, F_SESSION, session)
            lst.clear()
        lst = lpend.get(pid) if lpend else None
        if lst:
            lo = t_end - span - 1.0
            seen = set()
            for ts, child, msg, raw in lst:
                if ts >= lo:
                    e = child._trace(det, msg, raw)
                    if id(e) not in seen:
                        seen.add(id(e))
                        e[2] += 1
                        e[3] += span if seconds is None else seconds
            lst.clear()

    def _trace(self, det, msg, raw):
        """Count one untimed log line under operation `det`. ORM calls group by model and method
        ('stock.valuation.layer().search_read'), everything else by its normalized text."""
        m = TRACE_CALL.match(msg)
        key = "%s().%s" % (m.group(1), m.group(2)) if m else normalize(msg, 100)
        d = self.src.get(det)
        if d is None:
            d = self.src[det] = {}
        e = d.get(key)
        if e is None:
            if len(d) >= TRACE_KEEP:                       # bound memory: further shapes only add to one counter
                key = "(other lines)"
                e = d.get(key)
            if e is None:
                e = d[key] = [0, "" if key == "(other lines)" else raw.rstrip()[:TRACE_LINE], 0, 0.0]
        e[0] += 1
        return e

    def _job(self, job, minute, pid=None, t_end=0.0, pend=None, lpend=None, raw=None, stamp=None, facts=()):
        fam, app, det, seconds, nq = job
        ff = getattr(self.a, "fact_filter", None)
        logged = self._logged_facts(pid, t_end, seconds, pend, lpend) if fam == "cron" else {}
        if ff:
            have = dict(logged)
            for slot, v in facts:
                have.setdefault(slot, set()).add(v)
            xp = extra_pred(self.a)
            if not _op_filter_ok(ff, fam, app, det, have) or (xp and not xp(job_fields(fam, app, det, seconds, nq, facts, logged))):
                self.filtered += 1
                if fam == "cron":
                    self._drop_claims(pid, pend, lpend, t_end, seconds)
                return
        if fam == "cron":            # cron runs in its own worker; automation rules and module loading run *inside*
            self._count_logged(det, logged, seconds)
            self._claim(pid, det, t_end, seconds, pend, lpend)   # a request, which must keep what they log
        if facts:
            f, t = self.op_facts.setdefault(det, {}), self.op_facts_s.setdefault(det, {})
            for slot, v in facts:
                _fact_time(f, t, slot, v, seconds)
        b = self.det[det]
        b.add(0.0, 0.0, nq, False, self.rng, seconds)
        if raw is not None:
            b.note(stamp, raw, seconds, False)
        self.det[det].hist_add(minute, 0.0, 0.0, seconds)      # one run, same as any other operation's occurrence
        self.det_fam[det] = fam
        self.det_app[det] = app

    # -- warnings / errors -------------------------------------------------- #
    def _issue(self, level, logger, msg, stamp):
        """Count one WARNING / ERROR / CRITICAL line under its normalized message."""
        head = msg[:140]
        key = (level, logger, _norm_head(msg))
        e = self.issues.get(key)
        if e is None:
            if len(self.issues) >= 20000:              # bound memory on logs with unbounded variety
                key = (level, logger, "(more distinct messages)")
                e = self.issues.get(key)
            if e is None:
                e = self.issues[key] = [0, stamp, stamp, msg[:1500], "", "", {}]
        e[0] += 1
        if stamp < e[1]:
            e[1] = stamp
        if stamp > e[2]:
            e[2] = stamp
        m = stamp_minute(stamp[:10], stamp[11:16])
        h = e[6].get(m)
        if h is None:
            e[6][m] = [1, 0.0, 0.0, 0.0]
        else:
            h[0] += 1
        if not e[5] and ("server action" in msg or "ir.actions.server" in msg):
            sa = SERVER_ACTION.search(msg)
            if sa:
                e[5] = "server action #%s" % (sa.group(1) or sa.group(2))
        f = self.issue_facts.get(key)
        if f is None:
            f = self.issue_facts[key] = {}
        _num_facts(f, head)
        _detail_facts(f, msg[:2000])
        self.levels[level] += 1
        return key, e

    # -- request line ----------------------------------------------------- #
    def _request(self, msg, hour, pid, minute, t_end=0.0, pend=None, lpend=None, raw=None, stamp=None):
        m = ACCESS.match(msg)
        if not m:
            self.access_bad += 1
            self.unclassified["ACCESS-LINE-UNPARSED: " + normalize(msg, 80)] += 1
            return
        path, method, status, nq, qt, ot, mode = m.group("path", "method", "status", "nq", "qt", "ot", "mode")
        if mode == "ro->rw":
            self.formats["read-only requests retried read-write"] += 1
        if nq is None:
            self.access_untimed += 1          # older Odoo: no timing triplet
            nq, qt, ot = 0, 0.0, 0.0
        else:
            self.access_ok += 1
            nq, qt, ot = int(nq), float(qt), float(ot)
        if self.a.grep and self.a.grep not in path:
            self.filtered += 1
            return
        span = qt + ot                                      # how long the request ran, before any held time is removed
        if status == "101" or path.startswith("/longpolling/poll"):
            # A WebSocket upgrade / long poll keeps the connection open for minutes and Odoo books that
            # whole wall-clock time as "other time". It is waiting, not working: keep the SQL part
            # (the handshake's real queries) and report the held time separately.
            self.held_n += 1
            self.held_s += ot
            ot = 0.0
        key = (method, path, status == "404")
        cls = _REQ_CACHE.get(key)
        if cls is None:
            if len(_REQ_CACHE) > 200000:
                _REQ_CACHE.clear()
            fam, app, det = classify(method, path, status)
            collapsed = bool("?" in path or _DIGITS.search(path) or "<" in det or "…" in det or "/*" in det)   # ids / tokens / a query
            cls = _REQ_CACHE[key] = (fam, app, det, collapsed)
        fam, app, det, collapsed = cls
        is_err = status[0] == "5"
        ip, session = m.group("ip", "ident")
        ff = self._ff
        logged = self._logged_facts(pid, t_end, span, pend, lpend) if t_end else {}
        if ff:
            xp = extra_pred(self.a)
            if not _op_filter_ok(ff, fam, app, det, dict(request_facts(ip, session, path, collapsed), **logged)) or \
                    (xp and not xp(request_fields(fam, app, det, status, method, qt, ot, nq, ip, session, path, logged))):
                self.filtered += 1
                self._drop_claims(pid, pend, lpend, t_end, span)
                return
        f = self.op_facts.get(det)
        if f is None:
            f = self.op_facts[det] = {}
        t, secs = self.op_facts_s.setdefault(det, {}), qt + ot
        _fact_time(f, t, F_IP, ip, secs)
        if session != "-":                                  # 19.4+: a short session id per request
            _fact_time(f, t, F_SESSION, session, secs)
        if collapsed:
            _fact_time(f, t, F_URL, path[:FACT_LEN], secs)
        if fam not in ("assets", "probe"):
            self._client(ip if session == "-" else ip + " " + session, minute, qt + ot)
        d = self.det[det]
        d.add(qt, ot, nq, is_err, self.rng)
        if raw is not None:
            d.note(stamp, raw, qt + ot, is_err)
        d.hist_add(minute, qt, ot)
        d.st[status] = d.st.get(status, 0) + 1
        if det not in self.det_fam:
            self.det_fam[det] = fam
            self.det_app[det] = app
        if logged:
            self._count_logged(det, logged, qt + ot)
        if t_end:                                           # (0: this worker logged nothing since its last request)
            self._claim(pid, det, t_end, span, pend, lpend, ip, session, qt + ot)    # warnings / errors / untimed lines it logged while running it
        if hour is not None:
            self.hours[hour].add(qt, ot, nq, is_err, self.rng)
        if fam == "other":
            self.unclassified[det] += 1

        if fam in ("auth", "api.auth"):
            # Odoo logs "Login successful for login:X from n/a" with no IP (RPC has no
            # request object at that point), and the access line has the IP but not the
            # account. They are emitted back to back by the same worker, so the pid
            # joins them. GETs of /web/login are page views, not login attempts.
            got = self._pending_login.pop(pid, None)
            if got:
                lk = (got[1], ip, got[0])
                self.logins[lk] += 1
                seen = self.login_seen.get(lk)
                if stamp and seen is None:
                    self.login_seen[lk] = [stamp, stamp]
                elif stamp:
                    seen[0], seen[1] = min(seen[0], stamp), max(seen[1], stamp)
            elif method != "GET":
                self.logins[("(no login line in this file)", m.group("ip"), "?")] += 1

    def _client(self, client, minute, secs):
        """One request of `client` in `minute`: who was working when."""
        c = self.clients.get(client)
        if c is None:
            if len(self.clients) >= CLIENT_KEEP:
                client = CLIENT_OTHER
                c = self.clients.get(client)
            if c is None:
                c = self.clients[client] = {}
        e = c.get(minute)
        if e is None:
            c[minute] = [1, secs]
        else:
            e[0] += 1
            e[1] += secs

    # -- non-request line -------------------------------------------------- #
    def _event(self, logger, level, msg, pid):
        fam = event_family(logger)
        tpl = normalize(msg)
        key = "%-7s %s" % (level, tpl)
        self.events[fam][key] += 1
        self.event_level[(fam, key)] = level

        if fam == "auth":
            lm = LOGIN_MSG.match(msg)
            if lm:
                self._pending_login[pid] = (lm.group("res"), lm.group("login"))
        for sa in SERVER_ACTION.findall(msg):
            self.server_actions[sa[0] or sa[1]] += 1

    # -- report ------------------------------------------------------------ #
    def report(self, out):
        a = self.a
        w = out.write
        tot = sum(b.total for b in self.fam.values()) or 1e-9
        calls = sum(b.n for k, b in self.fam.items() if k not in JOB_KINDS)
        jobs = [(k, b) for k, b in self.fam.items() if k in JOB_KINDS]

        w("=" * 118 + "\n")
        w("ODOO LOG PROFILE\n")
        w("=" * 118 + "\n")
        w("window            : %s  ->  %s\n" % (self.t_min or "?", self.t_max or "?"))
        w("lines read        : %-12d  header matched: %d   continuation/traceback: %d   skipped by filters: %d\n"
          % (self.lines, self.header_ok, self.continuation, self.filtered))
        w("header formats    : %s\n" % (", ".join("%s=%d" % kv for kv in self.header_hits.most_common()) or "-"))
        if self.formats:
            w("log variants seen : %s\n" % ", ".join("%s=%d" % kv for kv in self.formats.most_common()))
        if self.held_n:
            w("held connections  : %d WebSocket/long-poll connections were held open for %.0f s in total "
              "(waiting, not counted as processing time)\n" % (self.held_n, self.held_s))
        w("requests          : %-12d  with timing: %d   without timing: %d   unparsable: %d\n"
          % (calls, self.access_ok, self.access_untimed, self.access_bad))
        if self.access_untimed:
            w("   note: %d access lines carry no query/time triplet (older Odoo); they count as 0 s.\n"
              % self.access_untimed)
        sql = sum(b.sql for b in self.fam.values())
        py = sum(b.py for b in self.fam.values())
        nq = sum(b.nq for b in self.fam.values())
        req_t = sum(b.total for k, b in self.fam.items() if k not in JOB_KINDS) or 1e-9
        w("total request time: %.0f s (%.2f h)   SQL %.0f s (%.1f%%)   Python %.0f s (%.1f%%)   queries %d\n"
          % (req_t, req_t / 3600, sql, 100 * sql / req_t, py, 100 * py / req_t, nq))
        if jobs:
            w("background jobs   : %s   (cron / automation / module loading: total time only, no SQL split)\n"
              % ", ".join("%s %d runs %.0f s" % (k, b.n, b.total) for k, b in sorted(jobs)))
        w("http status       : %s\n" % ", ".join("%s=%d" % kv for kv in self.status.most_common(8)))
        unk = self.fam.get("other")
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
            for k, v in self.fam.items():
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
            apps = sorted(self.app.items(), key=lambda kv: -kv[1].total)[:a.top]
            rows(apps, "BY APP  (which Odoo app owns the work: module prefix of the model / URL)")
        if a.by in ("detail", "both"):
            det = sorted(self.det.items(), key=lambda kv: -kv[1].total)[:a.top]
            rows([(("%-12s %-16s %s" % (self.det_fam.get(k, "?"), self.det_app.get(k, "?")[:16], k)), v) for k, v in det],
                 "TOP %d INDIVIDUAL OPERATIONS  (type, app, operation)" % a.top)

        if a.hours:
            w("-" * 118 + "\n")
            w("LOAD BY HOUR\n")
            w("-" * 118 + "\n")
            peak = max((b.total for b in self.hours.values()), default=1)
            for h in sorted(self.hours):
                b = self.hours[h]
                w("  %s:00  %8d req  %9.0f s  sql %4.0f%%  %s\n" % (
                    h, b.n, b.total, 100 * b.sql / (b.total or 1e-9),
                    "#" * int(48 * b.total / peak)))
            w("\n")

        if a.logins and self.logins:
            w("-" * 118 + "\n")
            w("LOGINS  (account + client IP, paired via the worker pid that logged both lines)\n")
            w("-" * 118 + "\n")
            w("%9s  %-38s %-18s %s\n" % ("count", "login", "client", "result"))
            for (login, ip, res), n in self.logins.most_common(a.top):
                w("%9d  %-38s %-18s %s\n" % (n, login[:38], ip, res))
            w("\n")

        if a.events:
            w("-" * 118 + "\n")
            w("EVENTS  (log lines that are not requests, so they have no timing)\n")
            w("-" * 118 + "\n")
            fams = sorted(self.events.items(), key=lambda kv: -sum(kv[1].values()))
            for fam, c in fams:
                w("  %-18s %8d lines\n" % (fam, sum(c.values())))
                for tpl, n in c.most_common(a.event_top):
                    w("      %8d  %s\n" % (n, tpl[:104]))
            if self.exceptions:
                w("\n  exception classes seen in tracebacks:\n")
                for exc, n in self.exceptions.most_common(15):
                    w("      %8d  %s\n" % (n, exc))
            if self.server_actions:
                w("\n  server actions that emitted log lines (ir.actions.server id -> lines):\n")
                for sa, n in self.server_actions.most_common(15):
                    w("      %8d  ir.actions.server(%s)\n" % (n, sa))
            w("\n")

        if a.unclassified and self.unclassified:
            w("-" * 118 + "\n")
            w("UNCLASSIFIED / CATCH-ALL SAMPLES  (extend classify() if any of these matter)\n")
            w("-" * 118 + "\n")
            for k, n in self.unclassified.most_common(a.top):
                w("  %8d  %s\n" % (n, k[:104]))
            w("\n")

    # -- machine readable -------------------------------------------------- #
    def as_rows(self, scopes=("family", "app", "operation"), limit=None):
        """One row per type, per app and per operation (scope says which); `limit` keeps only the
        biggest rows of each scope (by total time), before any per-row work is done."""
        tot = sum(b.total for b in self.fam.values()) or 1e-9
        out = []
        for scope, table in (("family", self.fam), ("app", self.app), ("operation", self.det)):
            if scope not in scopes:
                continue
            for name, b in sorted(table.items(), key=lambda kv: -kv[1].total)[:limit]:
                split = b.sql + b.py
                sqlpct = None if (b.unsplit and not split) else 100 * b.sql / (split or 1e-9)
                out.append({
                    "scope": scope,
                    "family": name if scope == "family" else ("" if scope == "app" else self.det_fam.get(name, "")),
                    "app": name if scope == "app" else ("" if scope == "family" else self.det_app.get(name, "")),
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


# --------------------------------------------------------------------------- #
# 4b. raw log lines around one warning/error, for the details popup
# --------------------------------------------------------------------------- #
#
# The parser keeps only aggregate counts and one first-seen sample per issue -- every other line, issue
# or not, is discarded once it has been counted, so a log of any size fits in memory. Showing the file's
# own context around one particular occurrence therefore means re-reading the file, on demand, right
# when the details popup asks for it (never during the main parse).

def _parse_header(raw):
    """(date, time, level, db, logger, msg) if `raw` is a log entry's header line as Profile.feed sees it
    (see _parse_header_g), else None. db is '-' where that format has no dbname column."""
    g = _parse_header_g(raw)
    if g is None:
        return None
    return g["date"], g["time"], g["level"], _db_of(g), g["logger"], g["msg"]


def _db_of(g):
    """The database of a parsed header, as the parser keys it: '-' for none ('?' and 'None' included)."""
    db = g.get("db") or "-"
    return "-" if db in ("?", "None") else db


def _parse_header_g(raw):
    """The named groups of a header line (date, time, ms, proc, level, db, logger, msg), else None. Decides
    exactly like Profile.feed: color codes are ignored, and an impossible timestamp (2026-02-31) makes the
    line a continuation line, not a header."""
    if "\x1b" in raw:
        raw = ANSI.sub("", raw)
    if not (raw[:1].isdigit() or DATEISH.search(raw, 0, 200)):
        return None
    for _, pat in HEADERS:
        m = pat.match(raw)
        if m:
            g = m.groupdict()
            try:
                stamp_minute(g["date"], g["time"])
            except ValueError:
                return None
            return g
    return None


def _wants_logged(ff):
    """Do these fact filters need what a request's own lines named (see Profile._logged_facts)?"""
    if not ff:
        return False
    if ff.get("extra"):
        return True
    return any(F_LOGGED_USER in want or F_LOGGED_REC in want
               for by in ("operation", "app", "family") for want in ff.get(by, {}).values())


def scan_issue_occurrences(path, level, logger, template, exclude_db, lo, hi, limit, ff=None, piece=None, only_db=None):
    """Every line in `path` (any codec opener() handles) matching one issue -- same level, logger, and
    normalized message template as _issue() computes -- as a list of {"line": <1-based line number>,
    "stamp": ...}, in file order, up to `limit`. `exclude_db` and the optional `lo`/`hi` (seconds, `hi`
    exclusive) narrow it the same way the issue's own count is already narrowed, so the two agree. So do the
    filters `ff` (args.fact_filter's shape), replaying what the parser does with them: the issue's own detail
    filter and the custom domain are checked once its traceback is read, and an occurrence then waits for the
    request / cron run its worker ends next (see Profile._claim): logged during a run the filters leave out,
    it is left out too; a client IP / session filter is checked against that request. `piece`: read only one
    byte range of a plain file (see _numbered_lines) -- the parser's own chunk, so the claims match it."""
    ff = ff or {}
    xp = dom_compile(ff["extra"]) if ff.get("extra") else None
    want = ff.get("issue", {}).get((level, logger, template)) or {}
    client = {k: v for k, v in want.items() if k in CLIENT_FACTS}
    own = {k: v for k, v in want.items() if k not in CLIENT_FACTS}
    track = bool(ff)

    def keep(msg, more):
        have = issue_occurrence_facts(msg, more)
        return (not own or _facts_ok(own, have)) and (not xp or xp(issue_fields(level, logger, template, msg, have)))
    out = []
    held = None                                  # (occurrence, message, continuation lines, worker, seconds)
    waiting = {}                                 # worker -> [(seconds, occurrence)]: waiting for the run they were logged in
    kids, cur, cur_left = {}, None, 0            # worker -> [(seconds, facts)] for what runs inherit (Profile._logged_facts)

    def settle(lst, during=False):              # nobody claims these (any more): parentless, unless a client was asked for
        if not client and not during:
            out.extend(occ for _t, occ in lst)
    try:
        with _numbered_lines(path, piece) as numbered:
            if only_db:
                numbered = _only_db_lines(numbered, only_db)
            for i, raw in numbered:
                if len(out) >= limit and not track:
                    break
                g = _parse_header_g(raw)
                if g is None:
                    if held is not None and len(held[2]) < ISSUE_MORE_LINES:
                        held[2].append(raw)
                    if cur is not None and cur_left > 0:
                        _detail_facts(cur, raw)
                        cur_left -= 1
                    continue
                cur = None
                if held is not None:
                    if keep(held[1], held[2]):
                        if track:
                            wl = waiting.setdefault(held[3], [])
                            wl.append((held[4], held[0]))
                            if len(wl) > 200:                   # the parser keeps no more pending per worker
                                settle(wl[:100])
                                del wl[:100]
                        else:
                            out.append(held[0])
                    held = None
                date, tm, lvl, db, lg, msg = g["date"], g["time"], g["level"], _db_of(g), g["logger"], g["msg"]
                if exclude_db and db in exclude_db:        # the parser leaves these out before any claim, so does this
                    continue
                pid = g.get("proc") or "?"
                t = None
                if track:
                    try:
                        t = _tsec(stamp_minute(date, tm), tm, g.get("ms"))
                    except ValueError:
                        continue
                    lst = kids.setdefault(pid, [])
                    if lvl in ISSUE_LEVELS:
                        cur, cur_left = {}, ISSUE_MORE_LINES
                        _detail_facts(cur, msg[:2000])
                        lst.append((t, cur))
                    elif lvl in TRACE_LEVELS and (lg == "odoo.api" or lg.startswith(TRACE_PREFIX)):
                        d = {}
                        _detail_facts(d, msg)
                        if d:
                            lst.append((t, d))
                    if len(lst) > 200:
                        del lst[:100]
                    op = _line_operation(lg, msg)
                    if op is not None and (op[0] == "cron" or lg in ACCESS_LOGGERS or lg.endswith(".werkzeug")):
                        start = t - (op[3] or 0.0) - 1.0
                        logged = {}
                        for ts, facts in lst:
                            if ts >= start:
                                for mine, nm in LOGGED_OF:
                                    if facts.get(mine):
                                        logged.setdefault(nm, set()).update(v for v in facts[mine] if v != FACT_OTHER)
                        lst.clear()
                        passes = _op_filter_ok(ff, op[0], op[1], op[2], dict(op[4], **logged)) and \
                            (not xp or xp(dict(op[5], **logged)))
                        mine_waiting = waiting.pop(pid, [])
                        settle([w for w in mine_waiting if w[0] < start])
                        during = [w for w in mine_waiting if w[0] >= start]
                        if not passes:
                            settle(during, during=True)
                        else:
                            m = ACCESS.match(msg) if op[0] != "cron" else None
                            have = {}
                            if m:
                                have[F_IP] = {m.group("ip")}
                                if m.group("ident") != "-":
                                    have[F_SESSION] = {m.group("ident")}
                            if not client or _facts_ok(client, have):
                                out.extend(occ for _t, occ in during)
                if lvl != level or lg != logger:
                    continue
                if normalize(msg[:140]) != template:
                    continue
                if lo is not None:
                    try:
                        secs = stamp_minute(date, tm[:5]) * 60 + int(tm[6:8])
                    except ValueError:
                        continue
                    if secs < lo or secs >= hi:
                        continue
                occ = {"line": i, "stamp": "%sT%s" % (date, tm)}
                if track:
                    held = (occ, msg, [], pid, t)
                else:
                    out.append(occ)
            if held is not None and keep(held[1], held[2]):
                if track:
                    waiting.setdefault(held[3], []).append((held[4], held[0]))
                else:
                    out.append(held[0])
            for lst in waiting.values():
                settle(lst)
    except OSError:
        pass
    return sorted(out, key=lambda o: o["line"])[:limit]


def _line_operation(logger, msg):
    """(type, app, operation, seconds, facts) of a timed line -- an access line or a job's own message --
    grouped exactly as the parser groups it, else None. facts: what the line carries, for fact filters."""
    if logger in ACCESS_LOGGERS or logger.endswith(".werkzeug"):
        m = ACCESS.match(msg)
        if not m:
            return None
        path, method, status = m.group("path", "method", "status")
        key = (method, path, status == "404")
        cls = _CLASS_CACHE.get(key)
        if cls is None:
            if len(_CLASS_CACHE) > 200000:
                _CLASS_CACHE.clear()
            cls = _CLASS_CACHE[key] = classify(method, path, status)
        fam, app, det = cls
        qt, ot = m.group("qt", "ot")
        qt, ot = float(qt or 0), float(ot or 0)
        if status == "101" or path.startswith("/longpolling/poll"):
            ot = 0.0                                    # held open, not working: as Profile._request counts it
        collapsed = "?" in path or _DIGITS.search(path) or "<" in det or "…" in det or "/*" in det
        fields = request_fields(fam, app, det, status, method, qt, ot, int(m.group("nq") or 0), m.group("ip"), m.group("ident"), path)
        return (fam, app, det, qt + ot if m.group("qt") else None,
                request_facts(m.group("ip"), m.group("ident"), path, collapsed), fields)
    if logger.endswith(JOB_LOGGERS):
        job = classify_job(logger, msg)
        if job:
            have = {}
            for slot, v in job[6]:
                have.setdefault(slot, set()).add(v)
            return job[0], job[1], job[2], job[3], have, job_fields(job[0], job[1], job[2], job[3], job[4], job[6])
        m = AUTO_END.match(msg) if logger.endswith("base_automation") else None
        if m:
            det = "automation: %s (time-based)" % m.group("name")
            return "automation", "base_automation", det, None, {}, job_fields("automation", "base_automation", det, 0.0, 0, ())
    return None


_TIMING_TAIL = re.compile(r'" \d{3} \S+ \d+ ([\d.]+) ([\d.]+)')       # an access line's status .. SQL s, other s


def scan_op_occurrences(path, by, name, exclude_db, lo, hi, limit, slowest=False, ff=None, piece=None, min_seconds=None,
                        only_db=None):
    """The timed lines in `path` of one operation, app or type (`by` is "operation", "app" or "family"), as
    [{"line", "stamp", "seconds"}]: the first `limit` in file order, or with `slowest` the `limit` slowest
    (which reads the whole file). `exclude_db` and `lo`/`hi` narrow it like scan_issue_occurrences, and
    `ff` (args.fact_filter's shape) leaves out the lines the fact filters leave out of the counts. min_seconds:
    with `slowest`, lines that took less are passed over on a cheap look at their timing, before any parsing
    (the caller knows roughly how slow the slowest `limit` are, see api_op_lines)."""
    col = {"family": 0, "app": 1, "operation": 2}[by]
    xp = dom_compile(ff["extra"]) if ff and ff.get("extra") else None
    track = _wants_logged(ff)        # replay what the parser does: each request inherits what its worker logged meanwhile
    quick = min_seconds if slowest and min_seconds and not track else None
    kids, cur, cur_left = {}, None, 0          # worker -> [(seconds, facts)]; the open warning's facts
    auto = {}                                    # (worker, rule) -> when its time-based run started
    out, heap, n = [], [], 0
    try:
        with _numbered_lines(path, piece) as numbered:
            if only_db:
                numbered = _only_db_lines(numbered, only_db)
            for i, raw in numbered:
                if not slowest and len(out) >= limit:
                    break
                logged = {}
                if quick is not None:                             # too fast to be among the slowest: not even parsed
                    tm_ = _TIMING_TAIL.search(raw)
                    if tm_ is None or float(tm_.group(1)) + float(tm_.group(2)) < quick:
                        continue
                g = _parse_header_g(raw)
                if g is None:
                    if cur is not None and cur_left > 0:          # the body / traceback of the warning above
                        _detail_facts(cur, raw)
                        cur_left -= 1
                    continue
                cur = None
                date, tm, lvl, db, lg, msg = g["date"], g["time"], g["level"], _db_of(g), g["logger"], g["msg"]
                if exclude_db and db in exclude_db:        # the parser leaves these out before any claim, so does this
                    continue
                pid = g.get("proc") or "?"
                op = _line_operation(lg, msg)
                if lg.endswith("base_automation") and (op is None or op[3] is None):
                    try:                                          # time-based rules: "Starting" .. "done", as the parser pairs them
                        t = _tsec(stamp_minute(date, tm), tm, g.get("ms"))
                    except ValueError:
                        continue
                    ms = AUTO_START.match(msg)
                    if ms:
                        auto[(pid, ms.group("name"))] = t
                    elif op is not None:
                        t0 = auto.pop((pid, AUTO_END.match(msg).group("name")), None)
                        if t0 is None or t < t0:
                            op = None                             # unpaired: the parser does not count it either
                        else:
                            op = op[:3] + (t - t0, op[4], dict(op[5], duration=t - t0))
                if track:
                    try:
                        t = _tsec(stamp_minute(date, tm), tm, g.get("ms"))
                    except ValueError:
                        continue
                    lst = kids.setdefault(pid, [])
                    if lvl in ISSUE_LEVELS:
                        cur, cur_left = {}, ISSUE_MORE_LINES
                        _detail_facts(cur, msg[:2000])
                        lst.append((t, cur))
                    elif lvl in TRACE_LEVELS and (lg == "odoo.api" or lg.startswith(TRACE_PREFIX)):
                        d = {}
                        _detail_facts(d, msg)
                        if d:
                            lst.append((t, d))
                    if len(lst) > 200:
                        del lst[:100]
                    if op is not None and (op[0] == "cron" or lg in ACCESS_LOGGERS or lg.endswith(".werkzeug")):
                        start = t - (op[3] or 0.0) - 1.0         # what a request / cron run claims, as _claim does
                        for ts, facts in lst:
                            if ts >= start:
                                for mine, nm in LOGGED_OF:
                                    if facts.get(mine):
                                        logged.setdefault(nm, set()).update(v for v in facts[mine] if v != FACT_OTHER)
                        lst.clear()
                if op is None or op[col] != name:
                    continue
                if ff and (not _op_filter_ok(ff, op[0], op[1], op[2], dict(op[4], **logged)) or
                           (xp and not xp(dict(op[5], **logged)))):
                    continue
                if lo is not None:
                    try:
                        secs = stamp_minute(date, tm[:5]) * 60 + int(tm[6:8])
                    except ValueError:
                        continue
                    if secs < lo or secs >= hi:
                        continue
                occ = {"line": i, "stamp": "%sT%s" % (date, tm), "seconds": op[3],
                       "operation": op[2] if by != "operation" else None}
                if not slowest:
                    out.append(occ)
                    continue
                n += 1
                item = (op[3] or 0.0, -i, n, occ)          # -i: of equally slow lines keep the earliest
                if len(heap) < limit:
                    heapq.heappush(heap, item)
                elif item > heap[0]:
                    heapq.heapreplace(heap, item)
    except OSError:
        pass
    return out if not slowest else [e[3] for e in sorted(heap, reverse=True)]


def mark_context(lines, match_at, by=None, name=None, issue=None):
    """What each line around an occurrence is to it, for the log viewer: "main" (the occurrence and its
    traceback), "same" (other occurrences of the same operation / app / type -- `by`, `name` -- or of the
    same warning -- `issue`: (level, logger, template)), "parent" (the request / cron run it ran inside: the
    next one its worker ended whose run covers it, as Profile._claim matches them), "during" (what the
    worker logged while the occurrence -- a request or job -- ran), else None. A continuation line has the
    mark of the header line above it."""
    col = {"family": 0, "app": 1, "operation": 2}.get(by)
    hdrs = []                                   # (index, worker, seconds, level, logger, msg, timed operation)
    for i, raw in enumerate(lines):
        g = _parse_header_g(raw)
        if g is None:
            continue
        try:
            t = _tsec(stamp_minute(g["date"], g["time"]), g["time"], g.get("ms"))
        except ValueError:
            continue
        hdrs.append((i, g.get("proc") or "?", t, g["level"], g["logger"], g["msg"], _line_operation(g["logger"], g["msg"])))
    main = next((h for h in hdrs if h[0] == match_at), None)
    marks = [None] * len(lines)
    if main is None:
        marks[match_at] = "main"
        return marks
    _, pid, t0, _lvl, _lg, _msg, op0 = main
    claims = lambda h: h[6] is not None and (h[6][0] == "cron" or h[4] in ACCESS_LOGGERS or h[4].endswith(".werkzeug"))   # noqa: E731
    start = t0 - (op0[3] or 0.0) - 1.0 if op0 is not None else None
    parent = None
    if not (op0 is not None and claims(main)):  # a request / cron run is no one's child
        parent = next((h for h in hdrs if h[0] > match_at and h[1] == pid and claims(h)), None)
        if parent is not None and t0 < parent[2] - (parent[6][3] or 0.0) - 1.0:
            parent = None                       # that run started after this line: not its parent
    kind = {}
    for h in hdrs:
        i, p, t, lvl, lg, msg, op = h
        if i == match_at:
            kind[i] = "main"
        elif parent is not None and i == parent[0]:
            kind[i] = "parent"
        elif (col is not None and op is not None and op[col] == name) or \
                (issue is not None and (lvl, lg) == tuple(issue[:2]) and normalize(msg[:140]) == issue[2]):
            kind[i] = "same"
        elif start is not None and p == pid and start <= t <= t0 and not claims(h):
            kind[i] = "during"
    cur, starts = None, {h[0] for h in hdrs}
    for i in range(len(lines)):
        if i in starts:
            cur = kind.get(i)
        elif i == match_at:
            cur = "main"
        marks[i] = cur
    return marks


def read_context(path, line_no, before, after, piece=None):
    """The raw lines around line number `line_no` (1-based) in `path`: up to `before` lines before it,
    the line itself, and up to `after` lines after. {"lines": [...], "match_at": <index of line_no in
    lines>}. A single early-exiting pass -- the file is never read past what was asked for. `piece`
    (start byte, None, its first line number) starts reading there instead of at the top of the file."""
    buf = deque(maxlen=before)
    out, match_at = [], 0
    try:
        with _numbered_lines(path, piece) as numbered:
            for i, raw in numbered:
                raw = raw.rstrip("\n")
                if i < line_no:
                    buf.append(raw)
                elif i == line_no:
                    match_at = len(buf)
                    out = list(buf) + [raw]
                else:
                    out.append(raw)
                    if i >= line_no + after:
                        break
    except OSError:
        pass
    return {"lines": out, "match_at": match_at}


# --------------------------------------------------------------------------- #
# 6. the global filter as an Odoo-style domain (see FILTERS.md)
# --------------------------------------------------------------------------- #
# A domain is a predicate over *occurrences*: one request (an access line), one job run (cron, automation
# rule, module loading) or one warning / error line; the app shows only the occurrences it keeps.
#
#     [["database", "not in", [false]], ["time", ">=", "2026-09-01 00:00:00"],
#      "|", ["operation", "!=", "call_kw sale.order.web_read"], ["client IP", "in", ["10.0.0.7"]],
#      ["http_status", ">=", 500]]
#
# Terms are [field, operator, value]; "&" and "|" take the next two domains, "!" the next one, and the
# top-level items are joined with "&". A term on a field the occurrence does not have (a client IP on a
# warning, a log level on a request) is *not applicable*: it is left out of the "&" / "|" / "!" around it,
# and a domain with nothing applicable left keeps the occurrence.
#
# The app's own filters map onto one shape each (dom_split() recognises them, dom_render() writes them), so
# every filter the UI sets can be read and edited as a domain, and a domain made of those shapes only runs on
# the fast, cached paths. Everything else is evaluated here for every occurrence (dom_compile()).

DOM_OPERATORS = ("=", "!=", "<", "<=", ">", ">=", "in", "not in",
                 "like", "not like", "ilike", "not ilike", "=like", "=ilike")
DOM_NEGATIVE = {"!=": "=", "not in": "in", "not like": "like", "not ilike": "ilike"}
# the selection: which log lines are read at all. Only as top-level conditions (joined with "&").
DOM_SELECTION = {
    "file": "name of a loaded log file",
    "database": "database name of the log line (false: lines with no database)",
    "time": "timestamp of the log line, 'YYYY-MM-DD HH:MM:SS' in the log's own clock (UTC)",
}
# what each kind of occurrence carries: field -> (kinds, meaning)
DOM_FIELDS = {
    "kind": ("all", "'request', 'job' or 'issue' (a WARNING / ERROR / CRITICAL line)"),
    "operation": ("request, job", "the operation, as in the table's operation column"),
    "app": ("request, job", "the Odoo app that owns the operation"),
    "type": ("request, job", "the kind of work (ui.read, api.write, cron, ...), the table's type column"),
    "duration": ("request, job", "seconds the request / job took (SQL + Python)"),
    "sql_s": ("request", "seconds in PostgreSQL"),
    "python_s": ("request", "seconds outside PostgreSQL"),
    "queries": ("request, job", "number of SQL queries"),
    "http_status": ("request", "HTTP status code, a number (200, 404, 500)"),
    "method": ("request", "HTTP method (GET, POST, ...)"),
    F_IP: ("request", "client address"),
    F_SESSION: ("request", "short session id (Odoo 19.4+ access lines), false when there is none"),
    F_URL: ("request", "the raw path, with its query string"),
    "cron id": ("job", "id of the cron job"),
    "status": ("job", "how a cron run ended (fully done, processed, ...)"),
    "remaining": ("job", "records a cron run left for later"),
    "rule id": ("job", "id of the automation rule"),
    "records processed": ("job", "records an automation rule run processed, written done/total (\"3/3\")"),
    "modules": ("job", "number of modules a module loading loaded"),
    "level": ("issue", "WARNING, ERROR or CRITICAL"),
    "logger": ("issue", "logger name (odoo.models, odoo.http, ...)"),
    "message": ("issue", "the message template, numbers replaced by # (as in the errors table)"),
    "issue": ("issue", "the whole key: [level, logger, message template]"),
    "text": ("issue", "the message exactly as logged (first line)"),
    F_LOGGED_USER: ("request, job", "user ids named in the lines logged while it ran (ORM calls, warnings, tracebacks)"),
    F_LOGGED_REC: ("request, job", "records named in the lines logged while it ran, like sale.order(42)"),
    F_USER: ("issue", "user ids named in the message or its traceback"),
    F_REC: ("issue", "records named in the message or its traceback, like sale.order(12)"),
}
DOM_DETAILS = (F_IP, F_SESSION, F_URL, F_USER, F_REC, "cron id", "status", "remaining", "rule id", "records processed",
               "modules", F_LOGGED_USER, F_LOGGED_REC)
# 'Order #', 'ID # (2)': the numbers behind a template's '#'; a bare '#' / '# (2)' when no word precedes the number
DOM_NUMBER_FIELD = re.compile(r"^(?:\S.* )?#(?: \(\d+\))?$")
DOM_NUMERIC = {"duration", "sql_s", "python_s", "queries", "http_status"}     # compared as numbers only
DOM_BY_FIELD = {"operation": "operation", "app": "app", "type": "family", "issue": "issue"}   # domain field -> row kind
DOM_FIELD_OF = {v: k for k, v in DOM_BY_FIELD.items()}
DOM_TS = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d)(?::(\d\d))?$")


class DomainError(ValueError):
    """A domain that cannot be used; the message says why, for the user (or the agent) to fix it."""


def _dom_lists(v):
    return [_dom_lists(x) for x in v] if isinstance(v, (list, tuple)) else v


def dom_parse_text(text):
    """Domain text -> list. JSON (true / false / null) or a Python literal (True / False / None, tuples)."""
    import ast
    text = (text or "").strip()
    if not text:
        return []
    try:
        dom = json.loads(text)
    except (ValueError, RecursionError):
        try:
            dom = ast.literal_eval(text)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError) as e:
            raise DomainError("not a valid domain: %s (write it as a JSON list, like "
                              "[[\"operation\", \"=\", \"...\"]])" % (getattr(e, "msg", None) or e))
    dom = _dom_lists(dom)
    if not isinstance(dom, list):
        raise DomainError("a domain is a list: [term, term, ...]")
    return dom


def dom_is_field(name):
    return name in DOM_FIELDS or name in DOM_SELECTION or name in DOM_VIEW or bool(DOM_NUMBER_FIELD.match(name))


# The view: what the page shows of the filtered occurrences -- the histogram's items, series, bucket size,
# focus, the table's grouping, search and sort, ... Not conditions: settings, one top-level term each
# (chart.item once per item), in the form [setting, "=", value] (chart.focus: two bounds, like time). The
# filter editor always writes all of them; a domain that leaves one out gets its default.
DOM_VIEW = {
    "chart.item": "one histogram item: {kind: name, \"bars\": [series], \"lines\": [series]}, kind being operation, "
                  "app, type, search (free text) or issue ([level, logger, message template])",
    "chart.total": "the series of the \"total\" item: {\"bars\": [series], \"lines\": [series]}",
    "chart.bucket": "bucket size in minutes (1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 10080) or \"auto\"",
    "chart.fit": "true: squeeze every bucket into the panel width; false: keep bars readable and scroll",
    "chart.focus": "the span the chart is focused on, two bounds like time (>= and <)",
    "chart.timezone": "timezone of the histogram's labels, like \"UTC\" or \"Europe/Brussels\"",
    "table.group": "rows of the table: operation, app or type",
    "table.search": "text typed in the table's search box",
    "table.sort": "[column, \"asc\" | \"desc\"]",
    "table.hidden_columns": "list of the table columns that are hidden",
    "errors.level": "all, errors or warnings",
    "errors.search": "text typed in the errors table's search box",
    "panels.hidden": "list of the panels that are collapsed: chart, table, selected, errors",
}
DOM_SERIES = ("requests", "avg_ms", "avg_ms.py", "avg_ms.sql", "total_ms", "total_ms.py", "total_ms.sql")
DOM_ITEM_KINDS = ("operation", "app", "type", "search", "issue")
DOM_BUCKETS = (1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 10080)
DOM_COLUMNS = ("type", "app", "total_s", "pct_of_total_time", "sql_s", "pct_sql_within_op", "python_s",
               "pct_python_within_op", "calls", "avg_ms", "p95_s", "max_s", "queries_per_call", "ms_sql_per_query",
               "http_5xx", "bottleneck")
DOM_PANELS = ("chart", "table", "selected", "errors")
DOM_VIEW_DEFAULT = {
    "items": [], "total": None, "bucket": "auto", "fit": True, "focus": None, "timezone": "UTC",
    "table": {"group": "operation", "search": "", "sort": ["total_s", "desc"],
              "hidden_columns": ["pct_sql_within_op", "pct_python_within_op", "http_5xx", "bottleneck"]},
    "errors": {"level": "all", "search": ""}, "panels_hidden": [],
}


def _dom_series(v, where, what, issue=False):
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise DomainError("%s is a list of series names, in %s" % (what, where))
    bad = [x for x in v if x not in DOM_SERIES or (issue and x != "requests")]
    if bad:
        raise DomainError("unknown series %s in %s; %s" % (", ".join(json.dumps(x) for x in bad), where,
                          "an issue only has \"requests\" (its occurrences)" if issue else "use " + ", ".join(DOM_SERIES)))
    return list(dict.fromkeys(v))


def _dom_view_term(field, op, value, where):
    """Validate one view setting; returns its normalized value."""
    if field == "chart.focus":
        if op not in (">=", ">", "<", "<="):
            raise DomainError("chart.focus takes two bounds, >= and <, like time, in %s" % where)
        dom_parse_ts(value)
        return value
    if op != "=":
        raise DomainError("%s is a setting: write [%s, \"=\", value], in %s" % (field, json.dumps(field), where))
    if field == "chart.item":
        if not isinstance(value, dict):
            raise DomainError("chart.item takes an object like {\"operation\": \"...\", \"bars\": [\"requests\"], "
                              "\"lines\": [\"avg_ms\"]}, in %s" % where)
        kinds = [k for k in value if k in DOM_ITEM_KINDS]
        extra = [k for k in value if k not in DOM_ITEM_KINDS + ("bars", "lines")]
        if len(kinds) != 1 or extra:
            raise DomainError("chart.item needs exactly one of %s (plus bars / lines), in %s" % (", ".join(DOM_ITEM_KINDS), where))
        kind, name = kinds[0], value[kinds[0]]
        issue = kind == "issue"
        if issue and not (isinstance(name, list) and len(name) == 3 and all(isinstance(x, str) for x in name)):
            raise DomainError("an issue item is [level, logger, message template], in %s" % where)
        if not issue and (not isinstance(name, str) or not name.strip()):
            raise DomainError("the %s of a chart.item is a non-empty text, in %s" % (kind, where))
        out = {kind: name,
               "bars": _dom_series(value.get("bars", ["requests"]), where, "bars", issue),
               "lines": _dom_series(value.get("lines", [] if issue else ["avg_ms"]), where, "lines", issue)}
        return out
    if field == "chart.total":
        if not isinstance(value, dict) or set(value) - {"bars", "lines"}:
            raise DomainError("chart.total takes {\"bars\": [series], \"lines\": [series]}, in %s" % where)
        return {"bars": _dom_series(value.get("bars", []), where, "bars"), "lines": _dom_series(value.get("lines", []), where, "lines")}
    if field == "chart.bucket":
        if value != "auto" and (isinstance(value, bool) or value not in DOM_BUCKETS):
            raise DomainError("chart.bucket is \"auto\" or one of %s minutes, in %s" % (", ".join(map(str, DOM_BUCKETS)), where))
        return value if value == "auto" else int(value)
    if field == "chart.fit":
        if not isinstance(value, bool):
            raise DomainError("chart.fit is true or false, in %s" % where)
        return value
    if field in ("chart.timezone", "table.search", "errors.search"):
        if not isinstance(value, str) or len(value) > 200 or (field == "chart.timezone" and not value.strip()):
            raise DomainError("%s takes a text value, in %s" % (field, where))
        return value
    if field == "table.group":
        if value not in ("operation", "app", "type"):
            raise DomainError("table.group is operation, app or type, in %s" % where)
        return value
    if field == "table.sort":
        if not (isinstance(value, list) and len(value) == 2 and value[0] in ("operation",) + DOM_COLUMNS and value[1] in ("asc", "desc")):
            raise DomainError("table.sort is [column, \"asc\" | \"desc\"] with a column of %s, in %s" % (", ".join(("operation",) + DOM_COLUMNS), where))
        return value
    if field == "table.hidden_columns":
        if not isinstance(value, list) or any(c not in DOM_COLUMNS for c in value):
            raise DomainError("table.hidden_columns is a list of %s, in %s" % (", ".join(DOM_COLUMNS), where))
        return list(dict.fromkeys(value))
    if field == "errors.level":
        if value not in ("all", "errors", "warnings"):
            raise DomainError("errors.level is all, errors or warnings, in %s" % where)
        return value
    if field == "panels.hidden":
        if not isinstance(value, list) or any(c not in DOM_PANELS for c in value):
            raise DomainError("panels.hidden is a list of %s, in %s" % (", ".join(DOM_PANELS), where))
        return list(dict.fromkeys(value))
    raise DomainError("unknown setting %s" % json.dumps(field))


def dom_parse_ts(v):
    """'YYYY-MM-DD HH:MM[:SS]' (log clock) -> seconds since the epoch."""
    m = DOM_TS.match(v.strip()) if isinstance(v, str) else None
    if not m:
        raise DomainError("time values are 'YYYY-MM-DD HH:MM:SS', got %s" % json.dumps(v))
    try:
        d = datetime.datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6] or 0),
                              tzinfo=datetime.timezone.utc)
    except (ValueError, OverflowError):
        raise DomainError("not a real time: %s" % json.dumps(v))
    return int(d.timestamp())


def _dom_term(t):
    where = json.dumps(t, ensure_ascii=False)
    if len(t) != 3 or not isinstance(t[0], str) or not isinstance(t[1], str):
        raise DomainError("a term is [field, operator, value], got %s" % where)
    field, op, value = t[0], t[1].strip().lower(), t[2]
    if field in DOM_VIEW:
        return ("term", field, op, _dom_view_term(field, op, value, where))
    if not dom_is_field(field):
        raise DomainError("unknown field %s in %s (FILTERS.md lists the fields)" % (json.dumps(field), where))
    if op not in DOM_OPERATORS:
        raise DomainError("unknown operator %s in %s; use one of %s" % (json.dumps(t[1]), where, ", ".join(DOM_OPERATORS)))
    scalar = (str, int, float, bool, type(None))
    if op in ("in", "not in"):
        if not isinstance(value, list):
            value = [value]
        if not all(isinstance(v, scalar) or (field == "issue" and isinstance(v, list)) for v in value):
            raise DomainError("%s takes a list of values, in %s" % (op, where))
    elif op in ("like", "not like", "ilike", "not ilike", "=like", "=ilike"):
        if not isinstance(value, str):
            raise DomainError("%s takes a text value, in %s" % (op, where))
    elif op in ("<", "<=", ">", ">="):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise DomainError("%s takes a number or a text value, in %s" % (op, where))
        if field in DOM_NUMERIC:
            try:
                _dom_num(value)
            except ValueError:
                raise DomainError("%s is a number: %s needs a number, in %s" % (field, op, where)) from None
    elif not (isinstance(value, scalar) or (field == "issue" and isinstance(value, list))):
        raise DomainError("%s takes a single value, in %s" % (op, where))
    if field == "issue":
        if op not in ("=", "!=", "in", "not in"):
            raise DomainError("issue only takes =, !=, in, not in, in %s" % where)
        for v in (value if op in ("in", "not in") else [value]):
            if not (isinstance(v, list) and len(v) == 3 and all(isinstance(x, str) for x in v)):
                raise DomainError("issue values are [level, logger, message template], in %s" % where)
    if field == "time":
        if op not in ("<", "<=", ">", ">="):
            raise DomainError("time only takes <, <=, >, >=, in %s" % where)
        dom_parse_ts(value)
    if field in ("file", "database") and op not in ("=", "!=", "in", "not in"):
        raise DomainError("%s only takes =, !=, in, not in, in %s" % (field, where))
    return ("term", field, op, value)


def _dom_node(items, i):
    if i >= len(items):
        raise DomainError("the domain ends too early: an operator is missing what it applies to")
    tok = items[i]
    if isinstance(tok, str):
        if tok in ("&", "|"):
            a, i = _dom_node(items, i + 1)
            b, i = _dom_node(items, i)
            return (tok, a, b), i
        if tok == "!":
            a, i = _dom_node(items, i + 1)
            return ("!", a), i
        raise DomainError("unknown logical operator %s; only \"&\", \"|\" and \"!\" stand alone" % json.dumps(tok))
    if isinstance(tok, list):
        return _dom_term(tok), i + 1
    raise DomainError("unexpected %s: items are terms [field, operator, value] or \"&\", \"|\", \"!\"" % json.dumps(tok))


def dom_and(nodes):
    out = None
    for n in reversed(nodes):
        out = n if out is None else ("&", n, out)
    return out


def dom_tree(dom):
    """Prefix list -> tree of ("&", a, b) | ("|", a, b) | ("!", a) | ("term", field, op, value), None when empty.
    Raises DomainError."""
    if not isinstance(dom, list):
        raise DomainError("a domain is a list: [term, term, ...]")
    nodes, i = [], 0
    try:
        while i < len(dom):
            n, i = _dom_node(dom, i)
            nodes.append(n)
    except RecursionError:
        raise DomainError("the domain nests too deeply")
    return dom_and(nodes)


def dom_prefix(node):
    """Tree -> prefix list (the transport form, JSON-able)."""
    if node is None:
        return []
    if node[0] == "term":
        return [[node[1], node[2], node[3]]]
    if node[0] == "!":
        return ["!"] + dom_prefix(node[1])
    return [node[0]] + dom_prefix(node[1]) + dom_prefix(node[2])


def dom_conjuncts(node):
    """The top-level "&"-joined parts of a tree."""
    if node is None:
        return []
    if node[0] == "&":
        return dom_conjuncts(node[1]) + dom_conjuncts(node[2])
    return [node]


def dom_mentions(node, fields):
    if node[0] == "term":
        return node[1] in fields
    return any(dom_mentions(c, fields) for c in node[1:])


def _dom_num(x):
    if isinstance(x, bool):
        raise ValueError
    return float(x)


def _dom_eq(x, v):
    if isinstance(v, list):
        return isinstance(x, (tuple, list)) and tuple(x) == tuple(v)
    if isinstance(x, bool) or isinstance(v, bool):
        return x is v
    if x == v:
        return True
    try:
        return _dom_num(x) == _dom_num(v)
    except (TypeError, ValueError):
        return False


def _dom_cmp(x, v, op):
    try:
        a, b = _dom_num(x), _dom_num(v)
    except (TypeError, ValueError):
        a, b = str(x), str(v)
    return a < b if op == "<" else a <= b if op == "<=" else a > b if op == ">" else a >= b


_DOM_LIKE = {}


def _dom_like(pattern, op):
    rx = _DOM_LIKE.get((pattern, op))
    if rx is None:
        body = "".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in pattern)
        if op in ("like", "ilike"):
            body = ".*" + body + ".*"
        if len(_DOM_LIKE) > 256:
            _DOM_LIKE.clear()
        rx = _DOM_LIKE[(pattern, op)] = re.compile(body, re.S | (re.I if "ilike" in op else 0))
    return rx


def _dom_positive(op, vals, v):
    if op == "=":
        return not vals if v is False or v is None else any(_dom_eq(x, v) for x in vals)
    if op == "in":
        return any(_dom_eq(x, y) for x in vals for y in v if y is not False and y is not None) or \
            (not vals and any(y is False or y is None for y in v))
    if op in ("<", "<=", ">", ">="):
        return any(_dom_cmp(x, v, op) for x in vals)
    rx = _dom_like(v, op)
    return any(rx.fullmatch(str(x)) for x in vals)


def _dom_test(node, fields):
    """True / False, or None when nothing in `node` applies to this occurrence."""
    kind = node[0]
    if kind == "term":
        _, field, op, v = node
        if field in fields:
            vals = fields[field]
        elif fields.get("kind") == "issue" and DOM_NUMBER_FIELD.match(field):
            vals = ()                                   # a warning without that number: applicable, but empty
        else:
            return None
        if vals is None or vals is False:
            vals = ()
        elif field == "issue" or not isinstance(vals, (set, frozenset, list, tuple)):
            vals = (vals,)
        neg = DOM_NEGATIVE.get(op)
        return not _dom_positive(neg, vals, v) if neg else _dom_positive(op, vals, v)
    if kind == "!":
        r = _dom_test(node[1], fields)
        return None if r is None else not r
    a, b = _dom_test(node[1], fields), _dom_test(node[2], fields)
    if a is None:
        return b
    if b is None:
        return a
    return (a and b) if kind == "&" else (a or b)


_DOM_COMPILED = {}


def dom_compile(prefix_list):
    """A predicate fields -> keep? for a (validated) prefix list, memoized per domain. `fields` maps each
    field the occurrence has to its value, or to a set of values for details that can have several."""
    key = json.dumps(prefix_list, sort_keys=True)
    f = _DOM_COMPILED.get(key)
    if f is None:
        node = dom_tree(prefix_list)
        f = (lambda fields: True) if node is None else (lambda fields, node=node: _dom_test(node, fields) is not False)
        if len(_DOM_COMPILED) > 64:
            _DOM_COMPILED.clear()
        _DOM_COMPILED[key] = f
    return f


def _dom_is_detail(field):
    return field in DOM_DETAILS or bool(DOM_NUMBER_FIELD.match(field))


def _dom_fact_pattern(node):
    """("|", [row field, "!=", name], <"&" of [detail, "in" | "=", values]>) -> (by, name, {detail: set}), else
    None: the detail filter of one row (its occurrences must carry one of the values of each detail)."""
    if node[0] != "|":
        return None
    for head, rest in ((node[1], node[2]), (node[2], node[1])):
        if head[0] != "term" or head[1] not in DOM_BY_FIELD or head[2] != "!=":
            continue
        by, name = DOM_BY_FIELD[head[1]], head[3]
        if by == "issue":
            if not isinstance(name, list):
                continue
            name = tuple(name)
        elif not isinstance(name, str) or not name:
            continue
        facts = {}
        for t in dom_conjuncts(rest):
            if t[0] != "term" or not _dom_is_detail(t[1]) or t[2] not in ("in", "="):
                return None
            if by == "issue" and t[1] in ISSUE_UNFILTERABLE:
                return None
            vals = t[3] if t[2] == "in" else [t[3]]
            if not vals or not all(isinstance(v, str) for v in vals):
                return None
            facts.setdefault(t[1], set()).update(vals)
        return by, name, facts
    return None


def dom_split(node):
    """A validated tree -> what the app's own filters can hold, plus the rest:
    {"file": [(op, values)], "database": [(op, values)], "time": [(op, seconds)],
     "facts": {(by, name): {detail: set}}, "extra": tree or None}. Raises DomainError."""
    out = {"file": [], "database": [], "time": [], "facts": {}, "extra": [], "view": _dom_view_default()}
    focus = []
    for n in dom_conjuncts(node):
        if n[0] == "term" and n[1] in DOM_VIEW:
            _, field, op, v = n
            view = out["view"]
            if field == "chart.item":
                view["items"].append(v)
            elif field == "chart.focus":
                focus.append((op, dom_parse_ts(v)))
            elif field in ("table.group", "table.search", "table.sort", "table.hidden_columns"):
                view["table"][field[6:]] = v
            elif field in ("errors.level", "errors.search"):
                view["errors"][field[7:]] = v
            else:
                view[{"panels.hidden": "panels_hidden"}.get(field, field[6:])] = v
            continue
        if dom_mentions(n, DOM_VIEW):
            raise DomainError("view settings (chart.*, table.*, errors.*, panels.*) are top-level terms, "
                              "not inside \"|\" or \"!\"")
        if n[0] == "term" and n[1] in DOM_SELECTION:
            _, field, op, v = n
            if field == "time":
                out["time"].append((op, dom_parse_ts(v)))
            else:
                out[field].append((op, v if isinstance(v, list) else [v]))
            continue
        if dom_mentions(n, DOM_SELECTION):
            raise DomainError("file, database and time can only be top-level conditions joined with \"&\" "
                              "(they choose which log lines are read at all), not inside \"|\" or \"!\"")
        fp = _dom_fact_pattern(n)
        if fp is not None and not set(fp[2]) & set(out["facts"].get((fp[0], fp[1]), ())):
            by, name, facts = fp                     # (a detail of that row given twice is two conditions: kept as such)
            mine = out["facts"].setdefault((by, name), {})
            for k, vals in facts.items():
                mine[k] = set(vals)
            continue
        out["extra"].append(n)
    out["extra"] = dom_and(out["extra"])
    if focus:
        lo = max([v for op, v in focus if op == ">="] + [v + 1 for op, v in focus if op == ">"], default=None)
        hi = min([v for op, v in focus if op == "<"] + [v + 1 for op, v in focus if op == "<="], default=None)
        if lo is None or hi is None or lo >= hi:
            raise DomainError("chart.focus needs a lower (>=) and a higher upper (<) bound")
        out["view"]["focus"] = [lo, hi]
    if out["view"]["total"] is None:                 # total steps aside while other items are shown
        out["view"]["total"] = {"bars": [], "lines": []} if out["view"]["items"] else {"bars": ["requests"], "lines": ["avg_ms"]}
    return out


def _dom_view_default():
    return json.loads(json.dumps(DOM_VIEW_DEFAULT))


def dom_view_terms(view):
    """A view (dom_split's "view" shape, as the client sends it) -> its terms, validated, every setting included."""
    v = _dom_view_default()
    view = view or {}
    if not isinstance(view, dict) or not all(isinstance(view.get(k, {}), dict) for k in ("table", "errors")) or \
            not isinstance(view.get("items", []), list) or not isinstance(view.get("panels_hidden", []), list) or \
            not (view.get("focus") is None or (isinstance(view["focus"], list) and len(view["focus"]) == 2
                                               and all(isinstance(x, (int, float)) and 0 <= x < 1e11 for x in view["focus"]))):
        raise DomainError("view: not the shape the filter editor sends")
    v.update({k: x for k, x in view.items() if k in v and k not in ("table", "errors")})
    v["table"].update(view.get("table") or {})
    v["errors"].update(view.get("errors") or {})
    ts = lambda s: datetime.datetime.fromtimestamp(s, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")   # noqa: E731
    raw = [["chart.item", "=", it] for it in v["items"]]
    raw.append(["chart.total", "=", v["total"] or {"bars": [], "lines": []}])
    raw += [["chart.bucket", "=", v["bucket"]], ["chart.fit", "=", v["fit"]]]
    if v["focus"]:
        raw += [["chart.focus", ">=", ts(v["focus"][0])], ["chart.focus", "<", ts(v["focus"][1])]]
    raw.append(["chart.timezone", "=", v["timezone"]])
    raw += [["table." + k, "=", v["table"][k]] for k in ("group", "search", "sort", "hidden_columns")]
    raw += [["errors." + k, "=", v["errors"][k]] for k in ("level", "search")]
    raw.append(["panels.hidden", "=", v["panels_hidden"]])
    return [_dom_term(t) for t in raw]


def dom_render(exclude_files, exclude_dbs, interval, facts, extra, view=None):
    """The app's filters as domain text, one top-level condition per line. exclude_dbs: names, None for the
    lines with no database; interval: (from, to) seconds, to exclusive; facts: [(by, name, {detail: values})];
    extra: prefix list."""
    parts = []
    if exclude_files:
        parts.append(("term", "file", "not in", sorted(exclude_files)))
    if exclude_dbs:
        parts.append(("term", "database", "not in",
                      [False if d is None else d for d in sorted(exclude_dbs, key=lambda d: (d is None, d or ""))]))
    if interval:
        ts = lambda s: datetime.datetime.fromtimestamp(s, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")   # noqa: E731
        parts.append(("term", "time", ">=", ts(interval[0])))
        parts.append(("term", "time", "<", ts(interval[1])))
    for by, name, fs in facts:
        terms = dom_and([("term", k, "in", sorted(v)) for k, v in sorted(fs.items())])
        parts.append(("|", ("term", DOM_FIELD_OF[by], "!=", list(name) if by == "issue" else name), terms))
    parts += dom_conjuncts(dom_tree(extra)) if extra else []
    if view is not None:
        parts += dom_view_terms(view)
    if not parts:
        return "[]"
    line = lambda n: ", ".join(json.dumps(x, ensure_ascii=False) for x in dom_prefix(n))   # noqa: E731
    return "[\n    " + ",\n    ".join(line(p) for p in parts) + "\n]"


# --------------------------------------------------------------------------- #
# 5. cli
# --------------------------------------------------------------------------- #

PAR_MIN_TOTAL = 24 * 1024 * 1024     # below this, one process is faster than sharing the work (pool not started yet)
PAR_WARM_TOTAL = 4 * 1024 * 1024     # ... and once warm_pool() has started the workers
PAR_MIN_CHUNK = 8 * 1024 * 1024
PAR_MAX_CHUNK = 64 * 1024 * 1024     # chunks are kept, each with its own copy of what it saw (facts, examples, ...):
                                     # with many big files, fewer bigger chunks hold less memory; but a time interval
                                     # re-reads the part of its two edge chunks inside it. 64 MB weighs the two
                                     # against each other


def _only_db_lines(numbered, only):
    """(line number, line) of `numbered` without the entries (header and continuation lines) of databases other
    than `only` -- what Profile.feed leaves out with args.only_db, so the raw lines agree with the counts."""
    skip = False
    for i, raw in numbered:
        g = _parse_header_g(raw)
        if g is not None:
            skip = _db_of(g) not in (only, "-")
        if not skip:
            yield i, raw


@contextlib.contextmanager
def _numbered_lines(path, piece=None):
    """(line number, line) of a whole file (any codec), or with `piece` = (start byte, end byte or None, the
    line number of the first line starting at or after `start`, see Chunk.lines) of that byte range of a plain
    file -- what the parser reads for one chunk."""
    if piece is None or piece[0] is None:
        with opener(path) as fh:
            yield enumerate(fh, 1)
        return
    start, end, first = piece
    gen = _lines(path, start, os.path.getsize(path) if end is None else end)
    try:
        yield enumerate(gen, first)
    finally:
        gen.close()


def _scan_task(task):
    kind, path, piece, kw = task
    return (scan_op_occurrences if kind == "op" else scan_issue_occurrences)(path, piece=piece, **kw)


def scan_pieces(kind, tasks, kw, enough=None):
    """One scan_op_occurrences / scan_issue_occurrences (`kind` "op" / "issue", keyword arguments `kw`) per task,
    in parallel on the worker pool; [result per task], in task order. tasks: [(path, piece)] -- piece (start,
    end, first line) of a plain file, or None for a whole file in one go (a compressed one); several files'
    pieces go in one call, so they share the pool. With `enough`, stops once the tasks read so far, in order,
    hold that many results (the first N lines)."""
    tasks = [(kind, path, piece, kw) for path, piece in tasks]
    if len(tasks) > 1:
        try:
            futures = [_pool().submit(_scan_task, t) for t in tasks]
            out, n = [], 0
            for fut in futures:
                out.append(fut.result())
                n += len(out[-1])
                if enough is not None and n >= enough:
                    break
            for fut in futures[len(out):]:              # not needed any more: drop those not started yet
                fut.cancel()
            return out
        except (BrokenProcessPool, RuntimeError) as e:   # a worker died (or the pool shut down): scan serially
            sys.stderr.write("worker pool unavailable (%s); scanning serially\n" % e)
            shutdown_pool()
    out, n = [], 0
    for t in tasks:
        out.append(_scan_task(t))
        n += len(out[-1])
        if enough is not None and n >= enough:
            break
    return out


def _lines(path, start, end, index=None):
    """Yield the lines of a plain file whose first byte lies in [start, end).

    If `index` is a list it receives (byte_offset, minute) every time the minute of the
    leading timestamp changes, so a later call can seek straight to a time range."""
    with open(path, "rb") as f:
        pos = start
        if start > 0:
            f.seek(start - 1)
            pos = start - 1 + len(f.readline())       # skip to the first line starting at/after start
        last = None
        while pos < end:
            line = f.readline()
            if not line:
                break
            if index is not None and line[:1].isdigit():
                key = line[:16]
                if key != last:
                    last = key
                    s = key.decode("ascii", "replace")
                    if len(s) == 16 and s[4] == "-" and s[10] in " T" and s[13] == ":":
                        try:
                            index.append((pos, stamp_minute(s[:10], s[11:16])))
                        except ValueError:
                            pass
            pos += len(line)
            if line.endswith(b"\r\n"):
                line = line[:-2] + b"\n"
            yield line.decode("utf-8", "replace")


READ_ERRORS = (OSError, EOFError, zlib.error)       # a missing, corrupt or truncated log file


def _entry_start(path, x, end, look=1 << 20):
    """The first byte, at or after `x` (and before `end`), of a line that starts a log entry (a header), so a
    chunk cut there keeps an entry's continuation lines (traceback, message body) with it. Within `look`
    bytes; else the first line start after `x`."""
    with open(path, "rb") as f:
        f.seek(max(0, x - 1))
        pos = x - 1 + len(f.readline()) if x > 0 else 0         # the first line starting at or after x
        first = pos
        while pos < min(end, x + look):
            line = f.readline()
            if not line:
                break
            if _parse_header(line.decode("utf-8", "replace")) is not None:
                return pos
            pos += len(line)
    return min(first, end)


class PackedHist:
    """A read-only {minute: [values]} (an operation's per-minute hist, a client's activity) packed into two flat
    arrays: about 40 bytes a minute instead of ~230 for a dict of lists of floats, and quick to send between
    processes. A parsed chunk keeps its profiles this way (they are only ever merged from, see _pack); merged
    profiles keep plain dicts, which they update in place. Reads like the dict: items(), values(), keys, len."""
    __slots__ = ("keys", "vals", "w")

    def __init__(self, d, w):
        ks = sorted(d)
        self.keys, self.w = array.array("q", ks), w
        self.vals = array.array("d", chain.from_iterable(map(d.__getitem__, ks)))

    def __len__(self):
        return len(self.keys)

    def __iter__(self):
        return iter(self.keys)

    def items(self):
        v, w = self.vals, self.w
        return ((k, v[i * w:i * w + w]) for i, k in enumerate(self.keys))

    def values(self):
        v, w = self.vals, self.w
        return (v[i * w:i * w + w] for i in range(len(self.keys)))


def _pack(p):
    """Pack what a parsed piece holds per minute (see PackedHist), now that nothing will be added to it."""
    for prof in [p] + list(p.parts.values()):
        for buckets in (prof.det, prof.hours):
            for b in buckets.values():
                if isinstance(b.hist, dict):
                    b.hist = PackedHist(b.hist, 4)
        prof.clients = {c: PackedHist(h, 2) if isinstance(h, dict) else h for c, h in prof.clients.items()}


def _work(a, path, start, end):
    """Profile one file, or the byte range [start, end) of a plain one.
    Returns (profile, index, error): a file that breaks half way keeps what was read before that."""
    p = Profile(a)
    idx = []
    err = None
    try:
        if start is None:
            with opener(path) as fh:
                p.feed(fh)
        else:
            p.feed(_lines(path, start, end, idx))
    except READ_ERRORS as e:
        err = "%s: %s" % (type(e).__name__, e)
    _pack(p)
    return p, idx, err


_POOL = None
_POOL_WARM = False          # every worker process is running
_POOL_LOCK = threading.Lock()


def _cpus():
    """CPUs this process may use (respects taskset and container limits)."""
    try:
        return len(os.sched_getaffinity(0)) or 1
    except AttributeError:
        return os.cpu_count() or 1


def _mp_context():
    """forkserver: workers are forked from a small clean helper, so they start in milliseconds and never
    inherit the server's threads or sockets (plain fork does, and can deadlock)."""
    methods = multiprocessing.get_all_start_methods()
    ctx = multiprocessing.get_context("forkserver" if "forkserver" in methods else "spawn")
    if ctx.get_start_method() == "forkserver":
        ctx.set_forkserver_preload([__name__])
    return ctx


def _worker_init():
    """Ctrl+C in a terminal reaches every process in it. Workers ignore it (each one would print a
    traceback); the parent decides when they stop, and they exit when their pool is shut down."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _pool():
    """One shared pool of worker processes, started once (see warm_pool) and reused for every load
    and every time-interval selection."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ProcessPoolExecutor(max_workers=_cpus(), mp_context=_mp_context(), initializer=_worker_init)
        return _POOL


def shutdown_pool():
    global _POOL, _POOL_WARM
    with _POOL_LOCK:
        pool, _POOL, _POOL_WARM = _POOL, None, False
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def _checkin(args):
    """Warm-up task: hold this worker until all of them have started, so none can be reused for another."""
    folder, n = args
    open(os.path.join(folder, str(os.getpid())), "w").close()
    deadline = time.monotonic() + 60
    while len(os.listdir(folder)) < n and time.monotonic() < deadline:
        time.sleep(0.02)


def warm_pool():
    """Start every worker process now, so the first big file does not wait for them (the pool adds
    one process per task until it has as many as it needs). Once they run,
    even a few MB are worth sharing between processes (see PAR_WARM_TOTAL)."""
    global _POOL_WARM
    n = _cpus()
    folder = tempfile.mkdtemp(prefix="olp_warm_")
    try:
        list(_pool().map(_checkin, [(folder, n)] * n))
        _POOL_WARM = True
    except Exception:                                    # warming is an optimisation, never a failure
        shutdown_pool()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


class Chunk(NamedTuple):
    """One independently parsed byte range of a plain file: the per-database profiles of its lines, the first /
    last timestamp in it, and how many lines it has (so the number of any chunk's first line is known without
    reading the file again: 1 + the lines of the chunks before it). A time interval reuses every chunk that lies
    inside it; the raw-line scanners read chunk by chunk."""
    start: Optional[int]          # None: a compressed file, parsed as one piece
    end: Optional[int]
    parts: dict
    t_min: Optional[str]
    t_max: Optional[str]
    lines: int = 0


def _chunk(start, end, part):
    stamps = [(c.t_min, c.t_max) for c in part.parts.values() if c.t_min]
    return Chunk(start, end, part.parts, min(s[0] for s in stamps) if stamps else None,
                 max(s[1] for s in stamps) if stamps else None, part.lines)


def profile_files(a, paths, jobs=None, spans=None, chunks=None, min_chunk=PAR_MIN_CHUNK):
    """Profile several logs into one Profile, splitting big plain files across processes.

    spans:  optional {path: (start_byte, end_byte)} to read only part of a plain file.
    chunks: optional dict that receives {path: [Chunk, ...]} (one per parsed piece, in file order); the
            returned Profile then holds only the index and the errors, not the merged counts.
    min_chunk: smallest piece worth a process of its own.
    The result's `.index` maps each plain path to its [(byte_offset, minute)] list and `.errors` maps
    the paths that could not be read completely to the reason."""
    jobs = jobs or _cpus()
    spans = spans or {}
    plan, total = [], 0
    prof = Profile(a)
    prof.index = {}
    prof.errors = {}
    for path in paths:
        if path == "-":
            plan.append((path, None, None))
            continue
        try:
            size = os.path.getsize(path)
        except OSError as e:
            prof.errors[path] = "%s: %s" % (type(e).__name__, e)
            continue
        if path.endswith((".gz", ".bz2")):
            plan.append((path, None, None))               # compressed: cannot seek, one task
            total += size
        else:
            lo, hi = spans.get(path, (0, size))
            plan.append((path, lo, hi))
            total += hi - lo

    def take(path, s, e, result):
        part, idx, err = result
        if err:
            prof.errors[path] = err
        if idx:
            prof.index.setdefault(path, []).extend(idx)
        if chunks is None:
            prof.merge(part)
        else:                                            # the caller merges the chunks it needs, when it needs them
            chunks.setdefault(path, []).append(_chunk(s, e, part))

    tasks = []
    if jobs > 1 and total >= (PAR_WARM_TOTAL if _POOL_WARM else PAR_MIN_TOTAL) and all(p != "-" for p, _, _ in plan):
        size = min(PAR_MAX_CHUNK, max(min_chunk, -(-total // jobs)))
        for path, s, e in plan:
            if s is None or e - s <= size:
                tasks.append((path, s, e))
            else:                                        # cut at log entries: a traceback stays with its header
                cuts = [s] + sorted({_entry_start(path, x, e) for x in range(s + size, e, size)} - {s, e}) + [e]
                tasks.extend((path, a, b) for a, b in zip(cuts, cuts[1:]))
        try:
            results = list(_pool().map(_work, [a] * len(tasks), *zip(*tasks)))
        except KeyboardInterrupt:
            shutdown_pool()                              # drop the queued chunks, then let the interrupt through
            raise
        except (BrokenProcessPool, RuntimeError) as e:  # a worker died (or the pool shut down): read serially
            sys.stderr.write("worker pool unavailable (%s); reading serially\n" % e)
            shutdown_pool()
            results = None
    else:
        tasks = plan
        results = None
    if results is None:
        results = [_work(a, path, s, e) for path, s, e in tasks]
    for (path, s, e), result in zip(tasks, results):
        take(path, s, e, result)
    for lst in prof.index.values():
        lst.sort()
    return prof.finalize()


SELFTEST_LOG = """\
2026-09-13 22:00:09,711 2422355 INFO db-name werkzeug: 10.0.0.1 - - [13/Sep/2026 22:00:09] "POST /web/dataset/call_kw/account.move/web_read HTTP/1.1" 200 - 91 0.412 0.663
2026-09-13 22:00:10,001 2422355 INFO db-name odoo.addons.base.models.res_users: Login successful for login:bot@example.com from n/a
2026-09-13 22:00:10,002 2422355 INFO db-name werkzeug: 10.0.0.9 - - [13/Sep/2026 22:00:10] "POST /xmlrpc/2/common HTTP/1.1" 200 - 7 0.002 0.338
2026-09-13 22:00:11,000 2422355 INFO db-name werkzeug: 10.0.0.9 - - [13/Sep/2026 22:00:11] "POST /xmlrpc/2/object#sale.order.create HTTP/1.1" 200 - 250 1.500 2.500
2026-09-13 22:00:12,000 2422356 INFO odoo.addons.base.models.ir_cron: Starting job `Mail: Email Queue Manager`.
2026-09-13 22:00:13,000 2422356 ERROR db-name odoo.http: Exception during request handling.
Traceback (most recent call last):
  File "x.py", line 1, in y
ValueError: bad thing
2026-09-13 22:00:14,000 2422357 INFO db-name werkzeug: 10.0.0.2 - - [13/Sep/2026 22:00:14] "GET /web/assets/1/web.assets_backend.min.js HTTP/1.1" 200 -
2026-09-13 22:00:15,000 2422357 INFO db-name werkzeug: 10.0.0.3 - - [13/Sep/2026 22:00:15] "GET /en_US/shop/some-product-123 HTTP/1.1" 500 - 12 0.100 0.400
2026-09-13 22:00:16,000 2422357 INFO db-name werkzeug: 10.0.0.4 - - [13/Sep/2026 22:00:16] "GET /.env HTTP/1.1" 404 - 1 0.001 0.002
2026-09-13 22:00:17,000 2422357 WARNING db-name py.warnings: ir.actions.server(2519,):93: DeprecationWarning: read_group is deprecated
2026-09-13 22:00:18,000 2422357 WARNING db-name odoo.api: Too many iterations for recomputing fields!
2026-09-13 22:00:19,000 2422358 INFO db-name odoo.addons.base.models.ir_cron: Job 'Mail: Email Queue Manager' (8) fully done (#loop 1; done 20; remaining 0; duration 0.62s)
2026-09-13 22:00:20,000 2422358 INFO db-name odoo.addons.base_automation.models.base_automation: Automation Rule 'Notify boss' (12) done (on_write; 3/3 records; duration 0.532s)
2026-09-13 22:00:21,000 2422358 INFO db-name odoo.modules.loading: 762 modules loaded in 7.38s, 2400 queries (+300 extra)
2026-09-13 22:01:00,100 2422360 INFO db-name odoo.addons.base.models.ir_cron: Starting job `Old Style`.
2026-09-13 22:01:00,600 2422360 INFO db-name odoo.addons.base.models.ir_cron: Job done: `Old Style` (0.500s).
2026-09-13 22:01:01,000 2422360 INFO db-name odoo.addons.base.models.ir_cron: Job 'Eighteen' (5) starting
2026-09-13 22:01:02,000 2422360 INFO db-name odoo.addons.base.models.ir_cron: Job 'Eighteen' (5) done in 1.250s
2026-09-13 22:01:02,100 2422360 INFO db-name odoo.addons.base.models.ir_cron: Job 'Eighteen' (5) completed
2026-09-13 22:01:03,000 2422361 INFO db-name odoo.addons.base_automation.models.base_automation: Starting time-based automation rule `Nightly`.
2026-09-13 22:01:05,500 2422361 INFO db-name odoo.addons.base_automation.models.base_automation: Time-based automation rule `Nightly` done.
2026-09-13 22:01:06,000 2422362 INFO db-name odoo.http.server: 10.0.0.7 BK1AC94d - [13/Sep/2026 22:01:06] "POST /web/dataset/call_kw/stock.picking/button_validate HTTP/1.1" 200 7454 19 0.032 0.483 rw
2026-09-13 22:01:07,000 2422362 INFO db-name odoo.http.server: 10.0.0.7 BK1AC94d - [13/Sep/2026 22:01:07] "GET /shop/cart HTTP/1.1" 200 stream 3 0.006 0.065 ro->rw
2026-09-13 22:00:22,000 2422359 INFO db-name werkzeug: 10.0.0.5 - - [13/Sep/2026 22:00:22] "POST /web/dataset/call_button/sale.order/action_confirm HTTP/1.1" 200 - 40 0.500 1.500
2026-09-13 22:00:23,000 2422359 INFO db-name werkzeug: 10.0.0.5 - - [13/Sep/2026 22:00:23] "GET /some-typo-page HTTP/1.1" 404 - 3 0.010 0.020
2026-09-13 22:00:24,000 2422359 ERROR db-name odoo.addons.base.models.ir_cron: Job 'Fix things' (9) server action #77 failed
Traceback (most recent call last):
  File "ir.actions.server(77,)", line 3, in <module>
KeyError: 'x'
2026-09-13 22:02:00,500 3000001 WARNING db-name odoo.addons.sale.models.sale_order: Something odd about order 42
2026-09-13 22:02:00,700 3000002 WARNING db-name odoo.addons.sale.models.sale_order: A different worker warned about order 43
2026-09-13 22:02:01,000 3000001 INFO db-name werkzeug: 10.0.0.8 - - [13/Sep/2026 22:02:01] "POST /web/dataset/call_kw/sale.order/action_confirm HTTP/1.1" 200 - 5 0.300 0.400
this line is complete garbage and must not crash the parser
"""


def selftest():
    import io
    checks, fails = [], []

    def ck(name, cond, got=None):
        checks.append(name)
        if not cond:
            fails.append("%s (got %r)" % (name, got))

    a = argparse.Namespace(top=10, by="both", min_calls=1, min_total=0.0, fold=1, events=True,
                           event_top=5, logins=True, hours=True, unclassified=True, all=True,
                           since=None, until=None, db=None, grep=None, csv=None, json=None, out=None)
    p = Profile(a)
    p.feed(io.StringIO(SELFTEST_LOG))
    p.finalize()

    ck("garbage line did not crash and was counted as continuation", p.continuation >= 2, p.continuation)
    ck("no-dbname header matched", p.header_hits.get("odoo-nodb", 0) == 1, dict(p.header_hits))
    ck("timed access lines parsed", p.access_ok == 10, p.access_ok)
    attributed = {op for d in p.issue_ops.values() for op in d}
    ck("a warning is attributed to the request its worker was running",
       attributed == {"call_kw sale.order.action_confirm"}, p.issue_ops)
    ck("a warning from another worker stays unattributed",
       sum(sum(d.values()) for d in p.issue_ops.values()) == 1 and p.levels["WARNING"] >= 4, dict(p.levels))
    ck("untimed access line tolerated", p.access_untimed == 1, p.access_untimed)
    ck("no unparsable access lines", p.access_bad == 0, p.access_bad)
    ck("total time = sql + python + unsplit job time",
       abs(sum(b.total for b in p.fam.values()) - sum(b.sql + b.py + b.unsplit for b in p.fam.values())) < 1e-9)
    ck("app totals equal type totals",
       abs(sum(b.total for b in p.app.values()) - sum(b.total for b in p.fam.values())) < 1e-6)
    ck("family totals equal detail totals",
       abs(sum(b.total for b in p.fam.values()) - sum(b.total for b in p.det.values())) < 1e-6)
    ck("call_kw read classified", "ui.read" in p.fam, sorted(p.fam))
    ck("xmlrpc common -> api.auth", p.fam.get("api.auth") and p.fam["api.auth"].n == 1, sorted(p.fam))
    ck("xmlrpc object create -> api.write", "api.write" in p.fam, sorted(p.fam))
    ck("app comes from the model prefix", "sale" in p.app and "account" in p.app, sorted(p.app))
    ck("button call is a workflow action", p.fam.get("ui.action") and p.fam["ui.action"].n >= 1, sorted(p.fam))
    ck("unknown page that 404s is not-found", "not-found" in p.fam, sorted(p.fam))
    ck("cron run counted with its duration", abs(p.det["cron: Mail: Email Queue Manager"].total - 0.62) < 1e-9)
    ck("cron app comes from the name prefix", "mail" in p.app, sorted(p.app))
    ck("automation rule counted", abs(p.det["automation: Notify boss (on_write)"].total - 0.532) < 1e-9, sorted(p.det))
    ck("module loading counted with its queries", p.det["load modules"].nq == 2400, sorted(p.det))
    ck("17.0 cron format", abs(p.det["cron: Old Style"].total - 0.5) < 1e-9, sorted(p.det))
    ck("18.0 cron format (once, not per line)", p.det["cron: Eighteen"].n == 1 and abs(p.det["cron: Eighteen"].total - 1.25) < 1e-9)
    ck("time-based automation paired start/done", abs(p.det["automation: Nightly (time-based)"].total - 2.5) < 1e-6, sorted(p.det))
    ck("19.4+ access line (odoo.http.server) parsed", p.det["call_kw stock.picking.button_validate"].n == 1, sorted(p.det))
    ck("stream body size and ro->rw flag tolerated", p.access_bad == 0 and p.formats["read-only requests retried read-write"] == 1, dict(p.formats))
    ck("both access-log variants recognised", len([k for k in p.formats if k.startswith("access:")]) == 2, dict(p.formats))
    ck("job time has no SQL/Python split", p.fam["cron"].unsplit > 0 and p.fam["cron"].sql == 0)
    ck("job time is part of the total", abs(sum(b.total for b in p.fam.values()) - sum(b.total for b in p.det.values())) < 1e-6)
    ck("server action tagged on its error",
       any(e[5] == "server action #77" for e in p.issues.values()), [e[5] for e in p.issues.values()])
    ck("language prefix stripped, shop page classified", "website" in p.fam, sorted(p.fam))
    ck("5xx counted on the right family", p.fam["website"].errors == 1, p.fam["website"].errors)
    ck("dotfile probe separated from 'other'", "probe" in p.fam and "other" not in p.fam, sorted(p.fam))
    ck("untimed asset line contributes 0 s", p.fam["assets"].total == 0.0, p.fam["assets"].total)
    ck("login paired to its client ip",
       p.logins.get(("bot@example.com", "10.0.0.9", "successful")) == 1, dict(p.logins))
    ck("exception class taken from traceback", p.exceptions.get("ValueError") == 1, dict(p.exceptions))
    ck("server action id extracted", p.server_actions.get("2519") == 1, dict(p.server_actions))
    ck("cron line grouped", "cron" in p.events, sorted(p.events))
    ck("recompute warning grouped", "orm-recompute" in p.events, sorted(p.events))
    ck("report renders", (lambda: (p.report(io.StringIO()), True)[1])())
    ck("cron id and status kept behind the operation name",
       p.op_facts["cron: Mail: Email Queue Manager"].get("cron id") == {"8": 1}
       and p.op_facts["cron: Mail: Email Queue Manager"]["status"] == {"fully done": 1}, p.op_facts.get("cron: Mail: Email Queue Manager"))
    ck("client IP kept behind the operation name", p.op_facts["xmlrpc sale.order.create"].get(F_IP) == {"10.0.0.9": 1})
    odd = p.issue_facts[("WARNING", "odoo.addons.sale.models.sale_order", "Something odd about order #")]
    ck("the number behind '#' kept under the word before it", odd.get("order #") == {"42": 1}, odd)
    ck("a warning keeps the client of the request it was logged during", odd.get(F_IP) == {"10.0.0.8": 1}, odd)
    ck("every level is counted", p.all_levels["INFO"] > 10 and p.all_levels["ERROR"] == 2, dict(p.all_levels))

    # header parsing edge cases: no-db lines whose message contains a colon, impossible dates and times
    q = Profile(argparse.Namespace(**dict(vars(a), split_db=True)))
    q.feed(io.StringIO("2026-09-14 13:00:00,100 77 INFO odoo.modules.loading: Loading: base\n"
                       "2026-02-31 13:00:00,100 77 INFO db x.y: not a real date\n"
                       "2026-09-14 25:00:00,100 77 INFO db x.y: not a real hour\n"))
    ck("a colon in the message of a no-db line does not turn the logger into a database",
       set(q.parts) == {"-"} and q.header_ok == 0 and q.parts["-"].header_ok == 1, (sorted(q.parts), q.header_ok))
    ck("impossible timestamps are continuation lines, not crashes", q.parts["-"].continuation == 2, q.parts["-"].continuation)

    out = sys.stdout
    for name in checks:
        bad = [f for f in fails if f.startswith(name)]
        out.write("  %s %s\n" % ("FAIL" if bad else "ok  ", bad[0] if bad else name))
    out.write("\n%d checks, %d failed\n" % (len(checks), len(fails)))
    return 1 if fails else 0


def _version():
    try:
        from . import __version__
        return __version__
    except ImportError:                                  # run as a plain script, outside the package
        return "(unpackaged)"


_CLI_TS = re.compile(r"^(\d{4}-\d\d-\d\d)(?:[ T](\d\d):(\d\d)(?::(\d\d))?)?$")


def _cli_stamp(text, upper):
    """--since / --until -> the form log stamps are compared in ('YYYY-MM-DDTHH:MM:SS'), else None. A bound
    covers what it names: --until 2026-09-14 is the whole day, --until 2026-09-14T21:00 the whole minute."""
    m = _CLI_TS.match((text or "").strip())
    if not m:
        return None
    date, hh, mm, ss = m.groups()
    try:
        datetime.datetime.strptime("%s %s:%s:%s" % (date, hh or "00", mm or "00", ss or "00"), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if hh is None:
        return date + ("T23:59:59" if upper else "T00:00:00")
    return "%sT%s:%s:%s" % (date, hh, mm, ss or ("59" if upper else "00"))


def main(argv=None):
    """logtool-report. Ctrl+C, kill and a closed terminal stop it cleanly: status 130, no traceback, and the
    worker processes are shut down with it."""
    def stop(signum, frame):
        raise KeyboardInterrupt
    old = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGHUP):
            old[sig] = signal.signal(sig, stop)
    try:
        return _main(argv)
    except KeyboardInterrupt:
        shutdown_pool()
        return 130
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


def _main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if "--selftest" in argv:
        return selftest()
    for stream in (sys.stdout, sys.stdin, sys.stderr):      # a C / POSIX locale must not break on non-ASCII names
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(
        prog="logtool-report",
        description="Group Odoo log lines into operations and report SQL / Python / total time.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Odoo access lines end with '<queries> <sql_seconds> <other_seconds>'; "
               "total = sql + other, which is where the split comes from.")
    ap.add_argument("logs", nargs="+", help="log files (plain, .gz, .bz2) or - for stdin")
    ap.add_argument("--version", action="version", version="%(prog)s " + _version())
    ap.add_argument("--selftest", action="store_true",
                    help="run built-in checks against a synthetic log and exit")
    ap.add_argument("--top", type=int, default=30, help="rows in the detail table (default 30)")
    ap.add_argument("--min-total", type=float, default=0.0,
                    help="hide operations below this many seconds of total time")
    ap.add_argument("--by", choices=("family", "app", "detail", "both"), default="both",
                    help="family = by type, app = by Odoo app, detail = per operation")
    ap.add_argument("--min-calls", type=int, default=1, help="hide operations with fewer calls")
    ap.add_argument("--fold", type=int, default=25,
                    help="in the family table, merge families with fewer than this many calls "
                         "into one row (default 25; use 1 to show every family)")
    ap.add_argument("--events", action="store_true", help="also report non-request log lines")
    ap.add_argument("--event-top", type=int, default=6, help="templates per event family")
    ap.add_argument("--logins", action="store_true", help="logins per account and client IP")
    ap.add_argument("--hours", action="store_true", help="load by hour")
    ap.add_argument("--unclassified", action="store_true", help="samples that hit the catch-all")
    ap.add_argument("--all", action="store_true", help="shorthand for --events --logins --hours --unclassified")
    ap.add_argument("--since", help="lower bound, in the log's clock: 2026-09-14, 2026-09-14T13:00 or "
                                    "'2026-09-14 13:00:05'")
    ap.add_argument("--until", help="upper bound, included: a date is the whole day, a minute the whole minute")
    ap.add_argument("--db", help="only this database name")
    ap.add_argument("--only-db", metavar="DB",
                    help="read only this database's lines (and those without a database); the others are "
                         "skipped unparsed, so big multi-database logs load faster than with --db")
    ap.add_argument("--exclude-db", action="append", metavar="DB",
                    help="skip lines from this database (repeatable)")
    ap.add_argument("--grep", help="only request paths containing this substring")
    ap.add_argument("--csv", help="write the sorted table to this CSV file")
    ap.add_argument("--json", help="write the sorted table to this JSON file")
    ap.add_argument("--jobs", type=int, default=None,
                    help="worker processes for big plain-text logs (default: CPU count; 1 = serial)")
    ap.add_argument("-o", "--out", help="write the text report here instead of stdout")
    a = ap.parse_args(argv)
    if a.all:
        a.events = a.logins = a.hours = a.unclassified = True
    for opt in ("since", "until"):
        if getattr(a, opt):
            stamp = _cli_stamp(getattr(a, opt), opt == "until")
            if stamp is None:
                ap.error("--%s: not a timestamp: %r (write 2026-09-14, 2026-09-14T13:00 or "
                         "'2026-09-14 13:00:05')" % (opt, getattr(a, opt)))
            setattr(a, opt, stamp)
    if a.since and a.until and a.since > a.until:
        ap.error("--since is after --until")
    a.logs = list(dict.fromkeys(a.logs))                # the same file (or stdin) twice is read once

    prof = profile_files(a, a.logs, a.jobs)
    for path, err in prof.errors.items():
        sys.stderr.write("problem reading %s: %s\n" % (path, err))

    if prof.header_ok == 0 and prof.filtered:
        sys.stderr.write("No log line matched the filters (--since / --until / --db / --only-db / --exclude-db): "
                         "%d lines recognised, all left out.\n" % prof.filtered)
        return 2
    if prof.header_ok == 0:
        sys.stderr.write(
            "No Odoo log lines recognised in %d lines. Expected something like\n"
            "  2026-09-13 22:00:09,711 2422355 INFO dbname werkzeug: ...\n" % prof.lines)
        return 2

    out = open(a.out, "w", encoding="utf-8") if a.out else sys.stdout
    try:
        prof.report(out)
    finally:
        if a.out:
            out.close()

    rows = prof.as_rows()
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
            wr.writeheader()
            wr.writerows(rows)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"window": [prof.t_min, prof.t_max], "rows": rows}, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
