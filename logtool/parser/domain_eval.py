"""Evaluating a domain on one occurrence: `dom_compile()` turns a prefix list into a predicate over its fields.
"""

from __future__ import annotations

import json
import math
import re

from .domain import DOM_NEGATIVE, DOM_NUMBER_FIELD, _dom_num, dom_tree
from .facts import F_DUR, dur_range


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
        node = _dom_dur(dom_tree(prefix_list))
        f = (lambda fields: True) if node is None else (lambda fields, node=node: _dom_test(node, fields) is not False)
        if len(_DOM_COMPILED) > 64:
            _DOM_COMPILED.clear()
        _DOM_COMPILED[key] = f
    return f


def _dom_dur(node):
    """A tree with its F_DUR terms written as the duration comparisons they stand for."""
    if node is None or node[0] != "term":
        return node if node is None else (node[0],) + tuple(_dom_dur(n) for n in node[1:])
    _, field, op, v = node
    if field != F_DUR:
        return node
    out = None
    for lo, hi in map(dur_range, v if op in ("in", "not in") else [v]):
        r = ("term", "duration", ">=", lo)
        if hi != math.inf:
            r = ("&", r, ("term", "duration", "<", hi))
        out = r if out is None else ("|", out, r)
    return ("!", out) if op in DOM_NEGATIVE else out
