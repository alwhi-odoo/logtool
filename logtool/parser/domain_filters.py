"""A domain <-> the app's own filters: `dom_split()` recognises the shapes the UI sets, `dom_render()` writes them.
"""

from __future__ import annotations

import datetime
import json

from .domain import (
    DOM_BY_FIELD, DOM_DETAILS, DOM_FIELD_OF, DOM_NUMBER_FIELD, DOM_SELECTION, DOM_VIEW, DOM_VIEW_DEFAULT,
    DomainError, _dom_term, dom_and, dom_conjuncts, dom_mentions, dom_parse_ts, dom_prefix, dom_tree,
)
from .facts import ISSUE_UNFILTERABLE


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
    {"file": [(op, values)], "database": [(op, values)], "time": [(op, seconds)], "ip": [(op, values)],
     "facts": {(by, name): {detail: set}}, "extra": tree or None}. Raises DomainError."""
    out = {"file": [], "database": [], "time": [], "ip": [], "facts": {}, "extra": [], "view": _dom_view_default()}
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
            raise DomainError("file, database, time and ip can only be top-level conditions joined with \"&\" "
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


def dom_render(exclude_files, exclude_dbs, interval, facts, extra, view=None, ips=()):
    """The app's filters as domain text, one top-level condition per line. exclude_dbs: names, None for the
    lines with no database; interval: (from, to) seconds, to exclusive; facts: [(by, name, {detail: values})];
    extra: prefix list; ips: the client addresses the report is narrowed to."""
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
    if ips:
        parts.append(("term", "ip", "in", sorted(ips)))
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
