#!/usr/bin/env python3
"""
logtool.app - localhost web UI for the Odoo log profiler.

    logtool                             # start on http://127.0.0.1:5000
    logtool odoo.log other.log.gz       # preload one or more logs
    logtool --port 8080 --no-browser

Logs are loaded through the browser (drag & drop or file picker) and can be
unloaded again. The analysis is filtered in three steps:

    file  ->  database  ->  time interval

Each file is parsed once, in chunks (in parallel for big files). Every chunk is kept as one
profile per database and remembers where each minute of the file starts (byte offsets).
Selecting files / databases only merges those cached pieces. A time interval merges the
chunks that lie inside it and re-reads just the two edges of the interval.

This module is only the front door (`logtool.app:main` is the `logtool` command, `logtool.app.app` the Flask
app): the code lives in the logtool/web/ package, one concern per module (see ARCHITECTURE.md).
"""

from __future__ import annotations

import sys

from .web import app, main   # noqa: F401


if __name__ == "__main__":
    sys.exit(main())
