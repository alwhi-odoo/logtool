"""The shape of an Odoo log line: the header formats, the access line, timestamps and message templates.

Everything else in the parser starts from these regular expressions and helpers. This module imports nothing
from the rest of the package, so anything may import it.
"""

from __future__ import annotations

import datetime
import re


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

ISSUE_LEVELS = ("WARNING", "ERROR", "CRITICAL")
_NORM_CACHE = {}        # message head -> normalized template

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


def _tsec(minute, tm, ms):
    """Seconds since the epoch (log clock) of a header's timestamp, with milliseconds."""
    return minute * 60 + int(tm[6:8]) + (float("0." + ms) if ms else 0.0)


def normalize(msg, limit=140):
    """Collapse a message into a template so variants group together."""
    m = _HEX.sub("<hex>", msg[:limit])
    m = _DIGITS.sub("#", m)
    return " ".join(m.split())


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
