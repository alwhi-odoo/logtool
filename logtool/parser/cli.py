"""The `logtool-report` command line (also `python -m logtool.profiler`).
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import re
import signal
import sys
import threading

from .. import __version__
from .parallel import profile_files, shutdown_pool
from .selftest import selftest


_CLI_TS = re.compile(r"^(\d{4}-\d\d-\d\d)(?:[ T](\d\d):(\d\d)(?::(\d\d))?)?$")


def _cli_stamp(text, upper):
    """--since / --until -> the form log stamps are compared in ('YYYY-MM-DDTHH:MM:SS'), else None. A bound
    covers what it names: --until 2026-09-14 is the whole day, --until 2026-09-14T21:00 the whole minute."""
    m = _CLI_TS.match((text or "").strip())
    if not m:
        return None
    date, hh, mm, ss = m.groups()
    try:
        datetime.datetime.strptime("%s %s:%s:%s" % (date, hh or "00", mm or "00", ss or "00"), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if hh is None:
        return date + ("T23:59:59" if upper else "T00:00:00")
    return "%sT%s:%s:%s" % (date, hh, mm, ss or ("59" if upper else "00"))


def main(argv=None):
    """logtool-report. Ctrl+C, kill and a closed terminal stop it cleanly: status 130, no traceback, and the
    worker processes are shut down with it."""
    def stop(signum, frame):
        raise KeyboardInterrupt
    old = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGHUP):
            old[sig] = signal.signal(sig, stop)
    try:
        return _main(argv)
    except KeyboardInterrupt:
        shutdown_pool()
        return 130
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


def _main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if "--selftest" in argv:
        return selftest()
    for stream in (sys.stdout, sys.stdin, sys.stderr):      # a C / POSIX locale must not break on non-ASCII names
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(
        prog="logtool-report",
        description="Group Odoo log lines into operations and report SQL / Python / total time.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Odoo access lines end with '<queries> <sql_seconds> <other_seconds>'; "
               "total = sql + other, which is where the split comes from.")
    ap.add_argument("logs", nargs="+", help="log files (plain, .gz, .bz2) or - for stdin")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    ap.add_argument("--selftest", action="store_true",
                    help="run built-in checks against a synthetic log and exit")
    ap.add_argument("--top", type=int, default=30, help="rows in the detail table (default 30)")
    ap.add_argument("--min-total", type=float, default=0.0,
                    help="hide operations below this many seconds of total time")
    ap.add_argument("--by", choices=("family", "app", "detail", "both"), default="both",
                    help="family = by type, app = by Odoo app, detail = per operation")
    ap.add_argument("--min-calls", type=int, default=1, help="hide operations with fewer calls")
    ap.add_argument("--fold", type=int, default=25,
                    help="in the family table, merge families with fewer than this many calls "
                         "into one row (default 25; use 1 to show every family)")
    ap.add_argument("--events", action="store_true", help="also report non-request log lines")
    ap.add_argument("--event-top", type=int, default=6, help="templates per event family")
    ap.add_argument("--logins", action="store_true", help="logins per account and client IP")
    ap.add_argument("--hours", action="store_true", help="load by hour")
    ap.add_argument("--unclassified", action="store_true", help="samples that hit the catch-all")
    ap.add_argument("--all", action="store_true", help="shorthand for --events --logins --hours --unclassified")
    ap.add_argument("--since", help="lower bound, in the log's clock: 2026-09-14, 2026-09-14T13:00 or "
                                    "'2026-09-14 13:00:05'")
    ap.add_argument("--until", help="upper bound, included: a date is the whole day, a minute the whole minute")
    ap.add_argument("--db", help="only this database name")
    ap.add_argument("--only-db", metavar="DB",
                    help="read only this database's lines (and those without a database); the others are "
                         "skipped unparsed, so big multi-database logs load faster than with --db")
    ap.add_argument("--exclude-db", action="append", metavar="DB",
                    help="skip lines from this database (repeatable)")
    ap.add_argument("--grep", help="only request paths containing this substring")
    ap.add_argument("--csv", help="write the sorted table to this CSV file")
    ap.add_argument("--json", help="write the sorted table to this JSON file")
    ap.add_argument("--jobs", type=int, default=None,
                    help="worker processes for big plain-text logs (default: CPU count; 1 = serial)")
    ap.add_argument("-o", "--out", help="write the text report here instead of stdout")
    a = ap.parse_args(argv)
    if a.all:
        a.events = a.logins = a.hours = a.unclassified = True
    for opt in ("since", "until"):
        if getattr(a, opt):
            stamp = _cli_stamp(getattr(a, opt), opt == "until")
            if stamp is None:
                ap.error("--%s: not a timestamp: %r (write 2026-09-14, 2026-09-14T13:00 or "
                         "'2026-09-14 13:00:05')" % (opt, getattr(a, opt)))
            setattr(a, opt, stamp)
    if a.since and a.until and a.since > a.until:
        ap.error("--since is after --until")
    a.logs = list(dict.fromkeys(a.logs))                # the same file (or stdin) twice is read once

    prof = profile_files(a, a.logs, a.jobs)
    for path, err in prof.errors.items():
        sys.stderr.write("problem reading %s: %s\n" % (path, err))

    if prof.header_ok == 0 and prof.filtered:
        sys.stderr.write("No log line matched the filters (--since / --until / --db / --only-db / --exclude-db): "
                         "%d lines recognised, all left out.\n" % prof.filtered)
        return 2
    if prof.header_ok == 0:
        sys.stderr.write(
            "No Odoo log lines recognised in %d lines. Expected something like\n"
            "  2026-09-13 22:00:09,711 2422355 INFO dbname werkzeug: ...\n" % prof.lines)
        return 2

    out = open(a.out, "w", encoding="utf-8") if a.out else sys.stdout
    try:
        prof.report(out)
    finally:
        if a.out:
            out.close()

    rows = prof.as_rows()
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
            wr.writeheader()
            wr.writerows(rows)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"window": [prof.t_min, prof.t_max], "rows": rows}, f, indent=1)
    return 0
