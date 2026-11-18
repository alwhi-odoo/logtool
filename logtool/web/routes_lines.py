"""Routes: raw log lines, re-read from the files (occurrences of an issue or operation, the lines around one).
"""

from __future__ import annotations

import logging
import os

from flask import jsonify, request

from . import state
from .. import profiler as olp
from .reqargs import _ff_arg, _interval_arg, _issue_key_arg
from .selection import _carried, _check_on_disk, _pieces, _prof_for, _select
from .server import app
from .state import FILES, LOCK, MAX_CONTEXT, MAX_ISSUE_LINES, NoData


@app.get("/api/issue_lines")
def api_issue_lines():
    """Every occurrence of one warning / error, re-found by re-reading the loaded files (see
    scan_issue_occurrences): the details popup lists these so the user can pick which one to see
    context around, instead of only ever seeing the first one the parser happened to keep."""
    level, logger, msg = request.args.get("level"), request.args.get("logger"), request.args.get("msg")
    if not level or not logger or msg is None:
        return jsonify({"error": "level, logger and msg are required"}), 400
    try:
        with LOCK:
            _, inc, _, exclude_db = _select(set(request.args.getlist("exclude_db")),
                                             set(request.args.getlist("exclude_file")), False)
            interval = _interval_arg()
            lo, hi = interval if interval else (None, None)
            files = [(f["id"], f["name"], f["path"]) for f in inc]
        ff = _ff_arg()[1] or {}
        _check_on_disk(files)
        kw = dict(level=level, logger=logger, template=msg, exclude_db=exclude_db, lo=lo, hi=hi, limit=MAX_ISSUE_LINES, ff=ff,
                  only_db=state.ONLY_DB)
        tasks = [(f, piece) for f in files for piece in _pieces(f[0], f[2], lo, hi)]      # every file's, in order
        out = []
        for (f, _piece), part in zip(tasks, olp.scan_pieces("issue", [(f[2], p) for f, p in tasks], kw, MAX_ISSUE_LINES)):
            out += [{"file": f[0], "file_name": f[1], "line": o["line"], "stamp": o["stamp"]} for o in part]
        return jsonify(out[:MAX_ISSUE_LINES])
    except NoData as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("issue_lines failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500


def _slowest_floor(by, name, inc, exclude_db, interval, ff):
    """(calls, seconds): how many calls an operation made in the selection, and a time the slowest
    MAX_ISSUE_LINES of them surely all took more than -- from its percentile sample, with a 2x margin -- so a
    slowest-first scan can pass over the faster lines without parsing them. (0, None) when it cannot tell; no
    time for a job (cron, automation, module loading): that look only reads access lines, so with one every line
    of a job would be passed over and the files read a second time."""
    if by != "operation":
        return 0, None
    try:
        with LOCK:
            ids, excl = tuple(f["id"] for f in inc), frozenset(exclude_db)
            prof = _prof_for(inc, ids, excl, interval, _ff_arg())
            b, job = prof.det.get(name), prof.det_fam.get(name) in olp.JOB_KINDS
    except NoData:
        return 0, None
    if b is None or job or not b.sample or b.n <= 2 * MAX_ISSUE_LINES:
        return (b.n if b else 0), None
    s = sorted(b.sample)
    return b.n, s[int(len(s) * (1 - 2.0 * MAX_ISSUE_LINES / b.n))]


@app.get("/api/op_lines")
def api_op_lines():
    """The timed lines (access lines, job messages) of one operation / app / type, re-found by re-reading the
    loaded files (see scan_op_occurrences): the first ones, or with order=slow the slowest. Like
    /api/issue_lines, the details popup lists them to pick one and see the raw lines around it."""
    by, name = request.args.get("by"), request.args.get("name")
    if by not in ("operation", "app", "family") or not name:
        return jsonify({"error": "by must be operation, app or family, and name is required"}), 400
    try:
        with LOCK:
            _, inc, _, exclude_db = _select(set(request.args.getlist("exclude_db")),
                                             set(request.args.getlist("exclude_file")), False)
            interval = _interval_arg()
            lo, hi = interval if interval else (None, None)
            files = [(f["id"], f["name"], f["path"]) for f in inc]
        slowest = request.args.get("order") == "slow"
        ff = _ff_arg()[1]
        _check_on_disk(files)
        kw = dict(by=by, name=name, exclude_db=exclude_db, lo=lo, hi=hi, limit=MAX_ISSUE_LINES, slowest=slowest, ff=ff,
                  only_db=state.ONLY_DB, carried=_carried())
        tasks = [(f, piece) for f in files for piece in _pieces(f[0], f[2], lo, hi)]      # every file's, in order
        n_calls, kw["min_seconds"] = _slowest_floor(by, name, inc, exclude_db, interval, ff) if slowest else (0, None)

        def scan():
            out = []
            for (f, _piece), part in zip(tasks, olp.scan_pieces("op", [(f[2], p) for f, p in tasks], kw,
                                                                None if slowest else MAX_ISSUE_LINES)):
                out += [dict(o, file=f[0], file_name=f[1]) for o in part]
            return out
        out = scan()
        if kw["min_seconds"] and len(out) < min(MAX_ISSUE_LINES, n_calls):    # the estimate was too high: all of them
            kw["min_seconds"] = None
            out = scan()
        if slowest:                                          # equally slow ones: in file (load) order, then line order
            order = {f[0]: k for k, f in enumerate(files)}
            out = sorted(out, key=lambda o: (-(o["seconds"] or 0.0), order[o["file"]], o["line"]))
        return jsonify(out[:MAX_ISSUE_LINES])
    except NoData as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("op_lines failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500


@app.get("/api/issue_context")
def api_issue_context():
    """The raw lines around one occurrence found via /api/issue_lines or /api/op_lines (see read_context)."""
    fid = request.args.get("file")
    try:
        line = int(request.args.get("line", "0"))
        before = max(0, min(MAX_CONTEXT, int(request.args.get("before", "3"))))
        after = max(0, min(MAX_CONTEXT, int(request.args.get("after", "3"))))
    except ValueError:
        return jsonify({"error": "line, before and after must be whole numbers"}), 400
    if line < 1:
        return jsonify({"error": "line is required"}), 400
    with LOCK:
        f = FILES.get(fid)
    if f is None:
        return jsonify({"error": "unknown file"}), 404
    if not os.path.isfile(f["path"]):
        return jsonify({"error": "%s is no longer on disk (deleted since it was loaded): unload and load it again" % f["name"]}), 410
    try:
        piece = None                                         # start at the chunk holding the first line wanted, not the top
        for p in _pieces(fid, f["path"]):
            if p is not None and p[2] <= max(1, line - before):
                piece = (p[0], None, p[2])
        out = olp.read_context(f["path"], line, before, after, piece)
        by, name = request.args.get("by"), request.args.get("name")
        issue = _issue_key_arg(request.args.get("level"), request.args.get("logger"), request.args.get("msg"))
        if out["lines"]:                                     # what each line is to this occurrence, for the colors
            out["marks"] = olp.mark_context(out["lines"], out["match_at"], by if by in ("operation", "app", "family") else None,
                                            name, issue)
        return jsonify(out)
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("issue_context failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500
