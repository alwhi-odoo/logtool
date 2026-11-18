"""PostgreSQL slow-query logs (pg_slow_queries.log): their own parser, and the shape that groups a query's runs.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import NamedTuple, Optional

from .lines import stamp_minute
from .logio import READ_ERRORS, opener


# PostgreSQL's log of the queries that ran too long, one entry each:
#
#     --------------------------------------------------------------------------------
#     2026-11-10 17:24:10.365 UTC my_db: approximate duration: 41 seconds
#
#     SELECT ...
#       FROM ...
#
# An entry starts after a line of dashes, with a header (when the query was logged, in UTC, its database, how
# long it ran); its SQL follows, up to the next line of dashes. The SQL is kept as text, never parsed: only its
# *shape* (the text with every literal replaced by ?, see sq_shape()) is worked out, so the runs of one query
# group together.

SQ_SEP = re.compile(r"^-{10,}\s*$")
SQ_HEAD = re.compile(r"^(?P<date>\d{4}-\d\d-\d\d)[ T](?P<time>(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d)(?P<frac>[.,]\d+)?\s+UTC\s+"
                     r"(?:(?P<db>[^\s:]+)?:\s+)?approximate duration:\s*(?P<dur>\d+(?:\.\d+)?)\s*seconds?\b")
SQ_NAME = re.compile(r"slow[_-]?quer", re.I)
SQ_SNIFF = 8192         # characters of a file sq_sniff() reads
SQ_SQL_MAX = 100_000    # characters of one query's SQL kept; the rest is cut (SlowQuery.cut)


class SlowQuery(NamedTuple):
    end: float          # when it was logged (it had then run `seconds`), seconds since 1970-01-01 UTC
    db: Optional[str]   # None: no database named
    seconds: float
    sql: str
    cut: bool           # the SQL was longer than SQ_SQL_MAX
    shape: str          # id of its shape (see sq_shape())


# literals and comments, matched in one pass: a quote inside a comment, or "--" inside a string, starts nothing
_SQ_TOKEN = re.compile(r"(?P<cm>--[^\n]*|/\*.*?\*/)|(?P<str>[Ee]?'(?:[^']|'')*'?)|(?P<qid>\"(?:[^\"]|\"\")*\"?)"
                       r"|(?P<num>(?<![\w$.])-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\w.]))", re.S)
_SQ_LIST = re.compile(r"\(\s*\?(?:\s*,\s*\?)+\s*\)")                 # (?, ?, ?) -> (?)
_SQ_ROWS = re.compile(r"\(\?\)(?:\s*,\s*\(\?\))+")                   # VALUES (?), (?) -> (?)
_SQ_ARRAY = re.compile(r"ARRAY\[\s*\?(?:\s*,\s*\?)*\s*\]", re.I)      # ARRAY[?, ?] -> ARRAY[?]
_SQ_SPACE = re.compile(r"\s+")


def sq_fields(q):
    """Everything a domain (see dom_compile) can test on one slow query."""
    return {"kind": "slow_query", "duration": q.seconds, "sql": q.sql}


def sq_censor(sql):
    """The SQL with every string and number literal replaced by ? and its comments dropped, its layout kept."""
    return _SQ_TOKEN.sub(lambda m: m.group() if m.lastgroup == "qid" else "" if m.lastgroup == "cm" else "?", sql)


def sq_shape(sql):
    """(id, text) of a query's shape: sq_censor()ed, lists of values folded into one, whitespace collapsed. The
    runs of one query with other values share it."""
    text = _SQ_SPACE.sub(" ", sq_censor(sql)).strip()
    text = _SQ_ROWS.sub("(?)", _SQ_LIST.sub("(?)", _SQ_ARRAY.sub("ARRAY[?]", text)))
    return hashlib.sha1(text.lower().encode("utf-8", "replace")).hexdigest()[:12], text


def sq_sniff(path, name=None):
    """Is this a slow-query log? Its name says so, or one of its first lines is an entry header."""
    if SQ_NAME.search(name or os.path.basename(path)):
        return True
    try:
        with opener(path) as fh:
            head = fh.read(SQ_SNIFF)
    except READ_ERRORS + (UnicodeError,):
        return False
    return any(SQ_HEAD.match(line) for line in head.splitlines()[:40])


def sq_parse(path, only_db=None):
    """Every entry of one slow-query log, in file order, and the text of each shape met:
    ([SlowQuery], {shape id: text}). only_db: keep that database's entries (and those without one) only.
    Raises READ_ERRORS when the file cannot be read."""
    out, shapes = [], {}
    head, sql, size = None, [], 0

    def flush():
        if head is None or (only_db is not None and head[1] not in (None, only_db)):
            return
        text = "\n".join(sql).strip("\n")
        sid, stext = sq_shape(text)
        shapes.setdefault(sid, stext[:2000])
        out.append(SlowQuery(head[0], head[1], head[2], text, size > SQ_SQL_MAX, sid))

    with opener(path) as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            m = SQ_HEAD.match(line) if line[:2].isdigit() else None
            if m or SQ_SEP.match(line):
                flush()
                head, sql, size = None, [], 0
                if m:
                    try:
                        end = stamp_minute(m["date"], m["time"][:5]) * 60 + int(m["time"][6:8]) + \
                            (float("0." + m["frac"][1:]) if m["frac"] else 0.0)
                    except ValueError:                      # an impossible date (2026-02-31): the entry is skipped
                        continue
                    head = (end, m["db"] or None, float(m["dur"]))
                continue
            if head is None:
                continue
            if size <= SQ_SQL_MAX:
                sql.append(line[:SQ_SQL_MAX - size + 1] if size + len(line) > SQ_SQL_MAX else line)
            size += len(line) + 1
        flush()
    return out, shapes
