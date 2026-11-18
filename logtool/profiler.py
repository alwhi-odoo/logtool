#!/usr/bin/env python3
"""
logtool.profiler - where does an Odoo server's time actually go?

Parses Odoo server logs, groups every request into a meaningful *operation*
(login, RPC call, list view read, button, import, export, report, asset, ...),
and reports SQL time / Python time / total time per operation, sorted by the
biggest consumer first.

Odoo's access log lines carry three numbers Odoo does not advertise:

    ... "POST /web/dataset/call_kw/account.move/web_read HTTP/1.1" 200 - 91 0.412 0.663
                                                                         ^^ ^^^^^ ^^^^^
                                                                         |  |     |
                                                        SQL queries -----+  |     |
                                                        seconds in SQL -----+     |
                                                        seconds not in SQL -------+

so total = sql + python, and the split is free. Everything else in this script
is about grouping those lines into buckets you can act on.

Every request is described by three independent answers: its *type* (ui.read,
ui.action, api.write, report, assets, ...), the Odoo *app* that owns it (the module
prefix of the model or URL) and the *operation* itself. Cron jobs, automation rules and
module loading are timed from their own log messages and join the same tables (their
SQL/Python split is unknown, so they are shown as "job").

Lines that are not requests (logins, tracebacks, deprecation warnings,
record-rule denials, ...) have no timing, so they are counted separately as
*events*. Nothing is dropped: the coverage report at the top accounts for every
line in the file, and --unclassified shows you what fell into the catch-alls.

Usage
    logtool-report LOG [LOG ...]              # plain, .gz and .bz2, or - for stdin
    logtool-report odoo.log --top 40
    logtool-report odoo.log --by family       # roll up, no detail table
    logtool-report odoo.log --events          # also the non-request lines
    logtool-report odoo.log --logins          # logins per account + client IP
    logtool-report odoo.log --unclassified    # samples of anything that hit "other"
    logtool-report odoo.log --csv out.csv --json out.json
    logtool-report odoo.log --since 2026-09-14T13:00 --until 2026-09-14T21:00
    logtool-report odoo.log --db db-name --grep account.move

Stdlib only. Streams, so a 400 MB log costs a few seconds and constant memory.

This module is the parser's front door: the code lives in the logtool/parser/ package, one concern per
module (see ARCHITECTURE.md), and what the rest of the tool uses of it is re-exported here. The web app
only ever reaches the parser as `olp.<name>` (`from .. import profiler as olp`), so a name it needs must be
listed below; `tests/test_layout.py` checks that every such name is.
"""

from __future__ import annotations

import sys

if __name__ == "__main__" and not __package__:      # run as a plain script (python logtool/profiler.py LOG), not
    import os                                        # installed: import the package from the folder it is in
    sys.path[0] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    __package__ = "logtool"

# lines: header formats, access line, timestamps
from .parser.lines import (   # noqa: F401
    ACCESS, ACCESS_LOGGERS, ANSI, DATEISH, EXC_LINE, HEADERS, ISSUE_LEVELS, LOGIN_MSG, SERVER_ACTION,
    TRACE_CALL, TRACE_KEEP, TRACE_LEVELS, TRACE_LINE, TRACE_PREFIX, normalize, stamp_minute,
)
# logio: reading log files, whole or one chunk
from .parser.logio import READ_ERRORS, opener   # noqa: F401
# facts: what grouping collapses, fact filters
from .parser.facts import (   # noqa: F401
    CLIENT_FACTS, CLIENT_KEEP, CLIENT_OTHER, FACT_KEEP, FACT_LEN, FACT_OTHER, F_DUR, F_IP, F_LOGGED_REC,
    F_LOGGED_USER, F_LOGIN, F_REC, F_SESSION, F_URL, F_USER, ISSUE_MORE_LINES, ISSUE_NUMS, ISSUE_UNFILTERABLE,
    LOGGED_OF, NUM_LABEL, RECORDS, USER_ID, _fact, _fact_time, _facts_ok, dur_range, issue_fields,
    issue_occurrence_facts, job_fields, merge_fact_times, merge_facts, request_facts, request_fields,
)
# classify: request -> (type, app, operation); event families
from .parser.classify import (   # noqa: F401
    BUTTONISH, LANG_PREFIX, LOGGER_FAMILIES, ONCHANGE_METHODS, POSISH, PROBE, READ_METHODS, STATIC_EXT,
    URL_APP, WRITE_METHODS, _TOKEN, app_of_model, classify, event_family,
)
# jobs: cron jobs, automation rules, module loading
from .parser.jobs import (   # noqa: F401
    AUTOMATION_DONE, AUTO_END, AUTO_START, CRON_17, CRON_18, CRON_19, CRON_KILLED, CRON_KILLED_LOGGER,
    CRON_KILLED_STATUS, CRON_START, CRON_START_17, JOB_KINDS, JOB_LOGGERS, MODULES_LOADED, book_cron_kills,
    classify_job, cron_killed_job, cron_killed_pid, cron_marks, cron_start, pair_cron_kills,
)
# bucket: the numbers of one operation
from .parser.bucket import Bucket, PackedHist, _pack   # noqa: F401
# report: text report, rows
from .parser.report import CHATTY_QUERIES, SLOW_QUERY_MS, as_rows, verdict, write_report   # noqa: F401
# profile: the accumulator
from .parser.profile import Profile   # noqa: F401
# scan: raw lines, re-read on demand
from .parser.scan import (   # noqa: F401
    mark_context, occurrence_context, read_context, scan_issue_occurrences, scan_op_occurrences,
)
# domain: the domain filter: fields, validation, tree
from .parser.domain import (   # noqa: F401
    DOM_BUCKETS, DOM_BY_FIELD, DOM_COLUMNS, DOM_DETAILS, DOM_FIELDS, DOM_FIELD_OF, DOM_ITEM_KINDS,
    DOM_ITEM_SERIES, DOM_NEGATIVE, DOM_NUMBER_FIELD, DOM_NUMERIC, DOM_OPERATORS, DOM_PANELS, DOM_SELECTION,
    DOM_SERIES, DOM_TS, DOM_VIEW, DOM_VIEW_DEFAULT, DomainError, dom_and, dom_conjuncts, dom_is_field,
    dom_mentions, dom_parse_text, dom_parse_ts, dom_prefix, dom_tree,
)
# domain_eval: ... evaluated on one occurrence
from .parser.domain_eval import dom_compile, extra_pred   # noqa: F401
# domain_filters: ... to and from the app's own filters
from .parser.domain_filters import dom_render, dom_split, dom_view_terms   # noqa: F401
# slowq: PostgreSQL slow-query logs
from .parser.slowq import (   # noqa: F401
    SQ_HEAD, SQ_NAME, SQ_SEP, SQ_SNIFF, SQ_SQL_MAX, SlowQuery, sq_censor, sq_fields, sq_parse, sq_shape,
    sq_sniff,
)
# parallel: worker pool, chunks, profile_files
from .parser.parallel import (   # noqa: F401
    Chunk, PAR_MAX_CHUNK, PAR_MIN_CHUNK, PAR_MIN_TOTAL, PAR_WARM_TOTAL, _cpus, profile_files, scan_pieces,
    shutdown_pool, warm_pool,
)
# selftest: --selftest
from .parser.selftest import SELFTEST_LOG, selftest   # noqa: F401
# cli: logtool-report
from .parser.cli import main   # noqa: F401


if __name__ == "__main__":
    sys.exit(main())
