"""The parser: Odoo logs in, per-operation numbers out. Python standard library only (no Flask, nothing from logtool.web).

Code outside this package reaches it through logtool/profiler.py (`from logtool import profiler as olp`), which
re-exports what is used. One concern per module; each imports only from the ones above it in this list:

    lines           the shape of a log line: header formats, access line, timestamps, message templates
    logio           reading log files: plain / .gz / .bz2, whole or one byte range (a chunk), with line numbers
    facts           what grouping collapses (client IP, user id, records, ...), fact filters, a domain's fields
    classify        a request -> (type, app, operation); a non-request line -> event family
    jobs            cron jobs, automation rules, module loading; cron runs whose worker was killed
    bucket          Bucket (the numbers of one operation) and PackedHist (its per-minute data, packed)
    report          the text report and the rows of the tables (called by Profile.report / Profile.as_rows)
    domain          the domain filter: fields, operators, validation, prefix list <-> tree
    domain_eval     ... evaluated on one occurrence (dom_compile)
    domain_filters  ... to and from the app's own filters (dom_split, dom_render)
    profile         Profile, the accumulator: feed() is the hot loop that reads every line
    scan            raw log lines re-read on demand: the occurrences of one issue / operation, their context
    slowq           PostgreSQL slow-query logs
    parallel        the worker pool, chunks and profile_files()
    selftest        logtool-report --selftest
    cli             the logtool-report command line
"""

from . import (   # noqa: F401
    lines, logio, facts, classify, jobs, bucket, report, domain, domain_eval, domain_filters, profile, scan, slowq,
    parallel, selftest, cli,
)
