"""A domain (text) -> the app's filter state, checked against what is loaded (POST /api/domain).
"""

from __future__ import annotations

import json

from .. import profiler as olp
from .loading import _load
from .selection import _select
from .state import FILES, LOCK, NONE_DB, NoData, ORDER


def domain_state(text):
    """Domain text -> the app's filter state: {"exclude_file": [ids], "exclude_db": [names], "interval":
    [from | None, to | None] | None, "ip": [client addresses], "ff": [...], "xd": prefix list | None, "view": {items,
    total, bucket, fit, focus, timezone, table: {group, search, sort, hidden_columns}, errors: {level, search},
    panels_hidden}} (every view setting filled in: what the domain leaves out gets its default).
    Raises DomainError / NoData with a message for the user (or agent) to fix the domain."""
    parts = olp.dom_split(olp.dom_tree(olp.dom_parse_text(text)))
    with LOCK:
        files = [FILES[i] for i in ORDER]
        for f in files:
            _load(f)
        logs = [f for f in files if f.get("kind") != "summary" and f["usable"]]
        if not logs:
            raise NoData("no log file is loaded (an imported summary cannot be filtered)")

        def choose(conds, universe, what):
            """[(op, values)] over names -> the excluded names."""
            out = set()
            for op, vals in conds:
                unknown = [v for v in vals if v not in universe]
                if unknown and op in ("!=", "not in"):          # leaving out what is not there: nothing to do
                    vals = [v for v in vals if v in universe]
                elif unknown:
                    shown = ", ".join(json.dumps(u) for u in sorted(universe, key=lambda u: (u is None, str(u))))
                    raise NoData("no %s %s; the loaded ones are: %s" % (
                        what, ", ".join("false" if u is None else json.dumps(u) for u in unknown), shown.replace("null", "false")))
                out |= (set(universe) - set(vals)) if op in ("=", "in") else set(vals)
            return out

        names = {}
        for f in logs:
            names.setdefault(f["name"], []).append(f["id"])
        ex_names = choose(parts["file"], list(names), "file is named")
        ex_files = {i for n in ex_names for i in names[n]}
        if len(ex_files) >= len(logs):
            raise NoData("the domain leaves out every loaded file")
        _, _, dbs, _ = _select(set(), ex_files, False)
        universe = [None if d == NONE_DB else d for d, _ in dbs]
        dbvals = [(op, [None if v is False else v for v in vals]) for op, vals in parts["database"]]
        ex_dbs = choose(dbvals, universe, "database is named")
        if universe and len(ex_dbs) >= len(universe):
            raise NoData("the domain leaves out every database of the selected files")
    lo = max([v for op, v in parts["time"] if op == ">="] + [v + 1 for op, v in parts["time"] if op == ">"], default=None)
    hi = min([v for op, v in parts["time"] if op == "<"] + [v + 1 for op, v in parts["time"] if op == "<="], default=None)
    if lo is not None and hi is not None and lo >= hi:
        raise NoData("the time conditions leave no time at all")
    ips = None
    for _, vals in parts["ip"]:                     # several ip terms: the addresses they all name
        ips = set(vals) if ips is None else ips & set(vals)
    if ips is not None and not ips:
        raise NoData("the ip conditions leave no client address at all")
    return {
        "exclude_file": sorted(ex_files),
        "exclude_db": sorted(NONE_DB if d is None else d for d in ex_dbs),
        "interval": None if lo is None and hi is None else [lo, hi],
        "ip": sorted(ips or ()),
        "ff": [{"by": by, "name": list(name) if by == "issue" else name, "facts": {k: sorted(v) for k, v in fs.items()}}
               for (by, name), fs in parts["facts"].items()],
        "xd": olp.dom_prefix(parts["extra"]) or None,
        "view": parts["view"],
    }
