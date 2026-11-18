"""The web app: a localhost Flask server over the parser, which it reaches only as `olp.<name>` (logtool/profiler.py).

Importing this package registers every route on `app`. One concern per module; each imports only from the ones
above it in this list:

    state            everything kept in memory: loaded files, parsed chunks, caches, locks, limits, the database to read
    util             time conversions (log clock) and the LRU cache helpers
    storage          the upload folder and temp directory on disk
    parse_cache      parsed chunks kept on disk between runs
    loading          recognising, decompressing and parsing a file once
    slowq            the slow queries of the selection (table, export)
    selection        files -> databases -> time interval -> one merged Profile; the pieces raw-line scans read
    results          a merged Profile as JSON: totals, tables, facts, issues, timelines
    durations        the call-duration histogram
    analysis         analyze() (the answer to /api/analyze) and the focused timeline()
    details          the details popup of one row
    slowq_detail     one slow query's form, what ran meanwhile
    scrub            blanking and pseudonymizing for the exports
    export_json      Export summary
    export_html      Export HTML
    export_text      Export text
    reqargs          the arguments of an analysis request (interval, fact filters, domain, pins)
    domain_state     a domain (text) -> the app's filter state
    server           the Flask app object, the request guard, the page
    routes_files     routes: files, reset, the database to read
    routes_analysis  routes: analysis, details, timeline, domain
    routes_lines     routes: raw log lines
    routes_export    routes: the exports
    cli              the logtool command line
"""

from . import (   # noqa: F401  (the routes_* modules add their routes to app)
    state, util, storage, parse_cache, loading, slowq, selection, results, durations, analysis, details, slowq_detail,
    scrub, export_json, export_html, export_text, reqargs, domain_state, server, routes_files, routes_analysis,
    routes_lines, routes_export, cli,
)
from .cli import main
from .server import app

__all__ = ["app", "main"]
