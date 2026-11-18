"""Routes: the analysis, the details popups, the timeline, the domain editor.
"""

from __future__ import annotations

import json
import logging

from flask import jsonify, request

from .. import profiler as olp
from .analysis import analyze, timeline
from .details import cron_requests, detail, issue_detail, request_durations
from .domain_state import domain_state
from .loading import _load_pending
from .reqargs import _ff_arg, _interval_arg, _issue_key_arg, _pins_arg, _prepare_filter
from .server import app
from .slowq_detail import op_slowq, slowq_detail, slowq_running
from .state import FILES, LOCK, NONE_DB, NoData


@app.get("/api/detail")
def api_detail():
    by, name = request.args.get("by"), request.args.get("name")
    if by not in ("operation", "app", "family") or not name:
        return jsonify({"error": "by must be operation, app or family, and name is required"}), 400
    try:
        _prepare_filter(_ff_arg())
        return jsonify(detail(by, name, set(request.args.getlist("exclude_db")), set(request.args.getlist("exclude_file")),
                              _interval_arg(), _ff_arg()))
    except NoData as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("detail failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500


@app.get("/api/issue_detail")
def api_issue_detail():
    key = _issue_key_arg(request.args.get("level"), request.args.get("logger"), request.args.get("msg"))
    if key is None:
        return jsonify({"error": "level, logger and msg are required"}), 400
    try:
        _prepare_filter(_ff_arg())
        return jsonify(issue_detail(key, set(request.args.getlist("exclude_db")), set(request.args.getlist("exclude_file")),
                                    _interval_arg(), _ff_arg()))
    except NoData as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("issue_detail failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500


@app.get("/api/timeline")
def api_timeline():
    try:
        _prepare_filter(_ff_arg())
        try:
            keys = json.loads(request.args.get("issues") or "[]")
        except ValueError:
            raise NoData("issues must be JSON")
        if not isinstance(keys, list) or not all(isinstance(k, list) and len(k) == 3 and all(isinstance(x, str) for x in k)
                                                 for k in keys):
            raise NoData("issues must be a list of [level, logger, message]")
        return jsonify(timeline(set(request.args.getlist("exclude_db")), set(request.args.getlist("exclude_file")),
                                _interval_arg(), _ff_arg(),
                                [tuple(k) for k in keys[:50]], frozenset(request.args.getlist("pin_op")[:200])))
    except NoData as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("timeline failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500


@app.get("/api/request_durations")
def api_request_durations():
    try:
        _prepare_filter(_ff_arg())
        return jsonify(request_durations(set(request.args.getlist("exclude_db")),
                                         set(request.args.getlist("exclude_file")), _interval_arg(), _ff_arg()))
    except NoData as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("request_durations failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500


@app.get("/api/cron_requests")
def api_cron_requests():
    """The requests that ran while one cron operation ran (see cron_requests)."""
    name = request.args.get("name") or ""
    if not name.startswith("cron: "):
        return jsonify({"error": "name must be a cron operation"}), 400
    try:
        return jsonify(cron_requests(name, set(request.args.getlist("exclude_db")),
                                     set(request.args.getlist("exclude_file")), _interval_arg(), _ff_arg()))
    except NoData as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("cron_requests failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500


@app.get("/api/slowq")
def api_slowq():
    """One slow query's form (see slowq_detail); with running=1, what ran meanwhile (see slowq_running)."""
    fn = slowq_running if request.args.get("running") == "1" else slowq_detail
    try:
        _prepare_filter(_ff_arg())
        return jsonify(fn(request.args.get("id"), set(request.args.getlist("exclude_db")),
                          set(request.args.getlist("exclude_file")), _interval_arg(), _ff_arg()))
    except NoData as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("slowq failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500


@app.get("/api/op_slowq")
def api_op_slowq():
    """The slow queries that ran while one operation / app / type did (see op_slowq)."""
    by, name = request.args.get("by"), request.args.get("name") or ""
    if by not in ("operation", "app", "family") or not name:
        return jsonify({"error": "by must be operation, app or family, with a name"}), 400
    try:
        _prepare_filter(_ff_arg())
        return jsonify(op_slowq(by, name, set(request.args.getlist("exclude_db")), set(request.args.getlist("exclude_file")),
                                _interval_arg(), _ff_arg()))
    except NoData as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("op_slowq failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500


@app.get("/api/domain")
def api_domain_get():
    """The current global filter (the same arguments /api/analyze takes) and view (`view`: JSON, the shape
    domain_state() returns under "view") written as a domain (see FILTERS.md)."""
    try:
        try:
            view = json.loads(request.args.get("view") or "null")
        except ValueError:
            raise NoData("view must be JSON")
        flt, interval = _ff_arg(), _interval_arg()
        with LOCK:
            files = [FILES[i]["name"] for i in request.args.getlist("exclude_file") if i in FILES]
        dbs = [None if d == NONE_DB else d for d in request.args.getlist("exclude_db")]
        facts = [(by, name, {k: sorted(v) for k, v in want.items()})
                 for by, rows in (flt[1] or {}).items() if by not in ("extra", "client") for name, want in rows.items()]
        return jsonify({"domain": olp.dom_render(files, dbs, interval, facts, (flt[1] or {}).get("extra"),
                                                 view if isinstance(view, dict) else None,
                                                 (flt[1] or {}).get("client", {}).get(olp.F_IP, ()))})
    except (NoData, olp.DomainError) as e:
        return jsonify({"error": str(e)}), 400
    except (TypeError, KeyError, AttributeError, RecursionError) as e:       # a view of the wrong shape
        return jsonify({"error": "view: not the shape the filter editor sends (%s)" % e}), 400


@app.post("/api/domain")
def api_domain_post():
    """Check a domain and turn it into the app's filter state (the client then analyzes with it)."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("domain"), str):
        return jsonify({"error": "send {\"domain\": \"...\"}"}), 400
    try:
        return jsonify({"state": domain_state(body["domain"])})
    except (olp.DomainError, NoData) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("domain failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500


@app.get("/api/analyze")
def api_analyze():
    try:
        _load_pending()
        _prepare_filter(_ff_arg())
        interval = _interval_arg()
        return jsonify(analyze(set(request.args.getlist("exclude_db")), set(request.args.getlist("exclude_file")),
                               interval, request.args.get("init") == "1", request.args.get("hist"),
                               request.args.get("rows"), _ff_arg(), _pins_arg()))
    except NoData as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("analysis failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500
