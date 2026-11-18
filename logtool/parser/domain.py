"""The global filter as an Odoo-style domain (see FILTERS.md): fields, operators, validation, prefix list <-> tree.

Evaluating a domain is in domain_eval.py; mapping it to and from the app's own filters in domain_filters.py.
"""

from __future__ import annotations

import datetime
import json
import re

from .facts import F_DUR, F_IP, F_LOGGED_REC, F_LOGGED_USER, F_REC, F_SESSION, F_URL, F_USER, dur_range


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
    "ip": "the whole report narrowed to these client addresses: their requests and the warnings logged during them",
}
# what each kind of occurrence carries: field -> (kinds, meaning)
DOM_FIELDS = {
    "kind": ("all", "'request', 'job', 'issue' (a WARNING / ERROR / CRITICAL line) or 'slow_query'"),
    "operation": ("request, job", "the operation, as in the table's operation column"),
    "app": ("request, job", "the Odoo app that owns the operation"),
    "type": ("request, job", "the kind of work (ui.read, api.write, cron, ...), the table's type column"),
    "duration": ("request, job, slow_query", "seconds the request / job took (SQL + Python), or the slow query ran"),
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
    "sql": ("slow_query", "the SQL of a slow query (from a PostgreSQL slow-query log), as logged"),
    F_DUR: ("request, job", "duration ranges in ms, \"lo-hi\" (lo included, hi not; \"lo-\": no upper bound), "
                            "as the duration histogram's bars"),
}
DOM_DETAILS = (F_IP, F_SESSION, F_URL, F_USER, F_REC, "cron id", "status", "remaining", "rule id", "records processed",
               "modules", F_LOGGED_USER, F_LOGGED_REC, F_DUR)
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
                  "app, type, search (free text), issue ([level, logger, message template]) or slow_query (a shape id)",
    "chart.total": "the series of the \"total\" item: {\"bars\": [series], \"lines\": [series]}",
    "chart.bucket": "bucket size in minutes (1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 10080) or \"auto\"",
    "chart.fit": "true: squeeze every bucket into the panel width; false: keep bars readable and scroll",
    "chart.focus": "the span the chart is focused on, two bounds like time (>= and <)",
    "chart.timezone": "timezone of the histogram's labels, like \"UTC\" or \"Europe/Brussels\"",
    "table.group": "rows of the table: operation, app, type or slow_query",
    "table.search": "text typed in the table's search box",
    "table.sort": "[column, \"asc\" | \"desc\"]",
    "table.hidden_columns": "list of the table columns that are hidden",
    "errors.level": "all, errors or warnings",
    "errors.search": "text typed in the errors table's search box",
    "panels.hidden": "list of the panels that are collapsed: chart, table, selected, errors",
}
DOM_SERIES = ("requests", "avg_ms", "avg_ms.py", "avg_ms.sql", "total_ms", "total_ms.py", "total_ms.sql")
DOM_ITEM_KINDS = ("operation", "app", "type", "search", "issue", "slow_query")
DOM_ITEM_SERIES = {"issue": ("requests",), "slow_query": ("requests", "avg_ms", "total_ms")}   # what those kinds can plot
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


def _dom_series(v, where, what, kind=None):
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise DomainError("%s is a list of series names, in %s" % (what, where))
    only = DOM_ITEM_SERIES.get(kind, DOM_SERIES)
    bad = [x for x in v if x not in only]
    if bad:
        raise DomainError("unknown series %s in %s; %s" % (", ".join(json.dumps(x) for x in bad), where,
                          "an issue only has \"requests\" (its occurrences)" if kind == "issue" else
                          "a slow query only has " + ", ".join(only) if kind == "slow_query" else "use " + ", ".join(DOM_SERIES)))
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
               "bars": _dom_series(value.get("bars", ["requests"]), where, "bars", kind),
               "lines": _dom_series(value.get("lines", [] if kind in DOM_ITEM_SERIES else ["avg_ms"]), where, "lines", kind)}
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
        if value not in ("operation", "app", "type", "slow_query"):
            raise DomainError("table.group is operation, app, type or slow_query, in %s" % where)
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
    if field == F_DUR:
        vals = value if op in ("in", "not in") else [value]
        if op not in ("=", "!=", "in", "not in") or not vals or not all(isinstance(v, str) and dur_range(v) for v in vals):
            raise DomainError("%s only takes =, !=, in, not in, with ranges of ms like \"100-150\" or \"5000-\", in %s"
                              % (F_DUR, where))
    if field in ("file", "database") and op not in ("=", "!=", "in", "not in"):
        raise DomainError("%s only takes =, !=, in, not in, in %s" % (field, where))
    if field == "ip":
        if op not in ("=", "in"):
            raise DomainError("ip only takes = and in, in %s" % where)
        if not all(isinstance(v, str) and v for v in (value if op == "in" else [value])):
            raise DomainError("ip values are client addresses (text), in %s" % where)
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
