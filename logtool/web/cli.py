"""The `logtool` command line: starts the web server.
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing.util
import os
import shutil
import signal
import sys
import tempfile
import threading
import webbrowser

from werkzeug.serving import make_server

from . import storage
from .. import __version__
from .. import profiler as olp
from .loading import _add_file
from .server import app
from .state import LOOPBACK, set_only_db
from .storage import _pick_tmp_dir


def main(argv=None):
    # Before anything else: point every temp file this process ever creates -- ours (UPLOAD_DIR) and
    # Werkzeug's own internal buffering of an incoming upload alike -- away from the OS default temp
    # directory (see _pick_tmp_dir for why).
    multiprocessing.util.get_temp_dir()    # the worker pool's sockets: keep them in the OS temp dir, not below
    tmp = _pick_tmp_dir()
    if tmp:
        tempfile.tempdir = tmp

    ap = argparse.ArgumentParser(prog="logtool", description="Web UI for the Odoo log profiler.")
    ap.add_argument("logs", nargs="*", help="log files to preload")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to listen on (default 127.0.0.1; anything else exposes the loaded logs)")
    ap.add_argument("--port", type=int, default=5000, help="port to listen on (default 5000)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    ap.add_argument("--db", metavar="NAME", help="read only this database's log lines (and those without one); "
                                                 "the others are skipped unparsed (can be changed on the page)")
    a = ap.parse_args(argv)
    set_only_db(a.db)

    for path in a.logs:
        if not os.path.isfile(path):
            ap.error("no such file: %s" % path)
        _add_file(os.path.abspath(path), os.path.basename(path), False)

    app.config["LOOPBACK_ONLY"] = a.host in LOOPBACK
    url = "http://%s:%d" % (a.host if ":" not in a.host else "[%s]" % a.host, a.port)
    print("Odoo log profiler UI on", url)
    if a.host not in LOOPBACK:
        print("WARNING: listening on %s: anyone who can reach this machine can read the loaded logs." % a.host)
    threading.Thread(target=olp.warm_pool, daemon=True).start()      # worker processes take a while to start
    if not a.no_browser and (sys.platform != "linux" or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()      # no desktop session (e.g. ssh): just print the URL
    def stop(signum, frame):        # kill / a closed terminal: clean up like Ctrl+C (workers, uploaded files)
        raise KeyboardInterrupt
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)
    try:
        # make_server (not app.run) so werkzeug prints no dev-server warning or access log
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        make_server(a.host, a.port, app, threaded=True).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        olp.shutdown_pool()
        if storage.UPLOAD_DIR:
            shutil.rmtree(storage.UPLOAD_DIR, ignore_errors=True)
    return 0
