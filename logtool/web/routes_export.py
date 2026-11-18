"""Routes: the three exports (summary JSON, text, standalone HTML).
"""

from __future__ import annotations

import datetime
import json
import logging
import os

from flask import Response, jsonify, render_template, request

from .export_html import export_html
from .export_json import export
from .export_text import export_text
from .loading import _load_pending
from .reqargs import _ff_arg, _interval_arg, _pins_arg, _prepare_filter
from .server import app
from .state import BOOT_ID, NoData


@app.get("/api/export")
def api_export():
    try:
        _prepare_filter(_ff_arg())
        data = export(set(request.args.getlist("exclude_db")), set(request.args.getlist("exclude_file")),
                      _interval_arg(), _ff_arg())
    except NoData as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("export failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500
    return _attachment(json.dumps(data, indent=2, ensure_ascii=False), "application/json", "json")


@app.get("/api/export_text")
def api_export_text():
    """The current selection as a plain-text report (see export_text())."""
    try:
        _load_pending()
        flt = _ff_arg()
        _prepare_filter(flt)
        body = export_text(set(request.args.getlist("exclude_db")), set(request.args.getlist("exclude_file")),
                           _interval_arg(), flt)
    except NoData as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("text export failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500
    return _attachment(body, "text/plain", "txt")


@app.get("/api/export_html")
def api_export_html():
    """The page itself, standalone, with the current selection built in (see export_html())."""
    try:
        _load_pending()
        _prepare_filter(_ff_arg())
        try:
            view = json.loads(request.args.get("view") or "null")
        except ValueError:
            raise NoData("view must be JSON")
        if not isinstance(view, dict) or not isinstance(view.get("items"), list):
            raise NoData("view must be the page's view settings")
        snapshot = export_html(set(request.args.getlist("exclude_db")), set(request.args.getlist("exclude_file")),
                               _interval_arg(), _ff_arg(), _pins_arg(), view)
        static = {}                                          # the stylesheet and every script: index.html inlines them,
        for name in sorted(os.listdir(app.static_folder)):   # the scripts in its own load order
            if name.endswith((".css", ".js")):
                with open(os.path.join(app.static_folder, name), encoding="utf-8") as fh:
                    static[name] = fh.read()
    except NoData as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                   # a bad log must not kill the server
        logging.getLogger("logtool").exception("HTML export failed")
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500
    page = render_template("index.html", boot=BOOT_ID, snapshot=snapshot, css=static["style.css"], js=static)
    return _attachment(page, "text/html", "html")


def _attachment(body, mimetype, ext):
    resp = Response(body, mimetype=mimetype)
    resp.headers["Content-Disposition"] = 'attachment; filename="logtool-export-%s.%s"' % (
        datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"), ext)
    return resp
