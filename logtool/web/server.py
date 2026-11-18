"""The Flask app object, the request guard and the page itself. Routes are added by the routes_* modules.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from flask import Flask, abort, render_template, request

from .state import BOOT_ID, LOOPBACK


app = Flask("logtool")      # the package that holds templates/ and static/ (logtool/, not logtool/web/)


@app.before_request
def _guard():
    """The server only listens on this machine, but any web page open in the same browser can still send
    it requests. Refuse those: a foreign Origin on anything that changes state, and (when bound to
    loopback) a Host that is not a loopback name, which is what DNS rebinding looks like."""
    host = request.host
    host = host[1:host.find("]")] if host.startswith("[") else host.rsplit(":", 1)[0]    # [::1]:5000, [::1], name:5000
    if app.config.get("LOOPBACK_ONLY") and host not in LOOPBACK:
        abort(403)
    origin = request.headers.get("Origin")
    if request.method not in ("GET", "HEAD") and origin and urlsplit(origin).netloc != request.host:
        abort(403)


@app.get("/")
def index():
    return render_template("index.html", boot=BOOT_ID)


@app.get("/favicon.ico")
def favicon():
    return "", 204
