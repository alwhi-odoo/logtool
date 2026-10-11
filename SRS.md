# logtool: Software Requirements Specification

| | |
|---|---|
| Product | logtool, an Odoo server log profiler |
| Version | 1.2.0 |
| Date | 2026-10-11 |
| Author | alwhi |
| Status | Released |

This document follows the structure of ISO/IEC/IEEE 29148 for a software requirements specification. Requirements
use **shall** for what the software must do, **should** for what it is expected to do unless there is a good
reason not to, and **may** for what is optional. Each requirement has an identifier (`FR-…` for functional,
`NFR-…` for non-functional) so tests, issues and changes can refer to it.

---

## 1. Introduction

### 1.1 Purpose

This document specifies what logtool does and the qualities it must have: the log input it accepts, the analysis
it produces, the user interface, the command-line tool, the HTTP API between the two halves, and the performance,
privacy and security constraints. It is written for contributors changing the code, reviewers judging whether a
change is correct, and users who want to know exactly what the tool promises.

### 1.2 Scope

logtool answers one question about an Odoo server: **where does its time go?** It reads the server's own log
files, groups every HTTP request and background job into an *operation*, and reports SQL time, Python time and
total time per operation, per Odoo app and per type of work. It shows when the load happened, which warnings and
errors were logged and during which requests, and the clients, users and records behind them.

logtool consists of:

* `logtool`, a web application that runs on the user's machine and is used in a browser on that machine;
* `logtool-report`, a command-line tool that prints the same analysis as a text report.

Out of scope: collecting logs from servers, live tailing, alerting, storing analyses in a database, multi-user
access, and any analysis that needs data the log does not contain (e.g. per-query SQL plans).

### 1.3 Definitions

| term | meaning |
|---|---|
| **log entry** | One header line (timestamp, process id, level, database, logger, message) and the continuation lines that follow it (message body, traceback). |
| **access line** | The log entry Odoo writes when an HTTP request ends (logger `werkzeug` up to 19.0, `odoo.http.server` from 19.4). It carries the client IP, method, path, status and the **timing triplet**. |
| **timing triplet** | `<queries> <sql seconds> <other seconds>` at the end of an access line. Total = SQL + other ("Python"). |
| **operation** | A group of requests or job runs that do the same thing, e.g. `call_kw sale.order.web_read`, `/shop/<slug>`, `cron: Mail: Email Queue Manager`. Ids, tokens and slugs are collapsed so one page is one row. |
| **app** | The Odoo module that owns an operation (`sale`, `stock`, `website_sale`, ...). |
| **type** (family) | The kind of work: `ui.read`, `ui.write`, `ui.action`, `api.method`, `report`, `assets`, `cron`, `automation`, `module-load`, ... |
| **job** | A cron run, an automation-rule run or a module loading, timed from its own log messages. |
| **issue** | A `WARNING`, `ERROR` or `CRITICAL` entry, grouped by level, logger and message *template* (numbers replaced by `#`). |
| **occurrence** | One request, one job run or one issue line. |
| **detail** (fact) | A value that grouping collapsed and that tells occurrences apart: client IP, session, raw URL, user id, record, cron id, ... |
| **claim** | Matching an untimed line (a warning, an ORM trace line) to the request or job its worker was running, which is logged when it ends. |
| **chunk** | A byte range of a plain log file parsed independently, cut at the start of a log entry. |
| **selection** | Files → databases → time interval: which log lines an analysis covers. |
| **item filter** | A filter on one row: "occurrences of X must carry one of these detail values". |
| **domain** | An Odoo-style prefix-notation condition list that expresses the whole page state ([FILTERS.md](../FILTERS.md)). |
| **only-read database** | A database chosen before loading: every other database's entries are skipped while parsing. |

### 1.4 References

* [README.md](../README.md): user guide.
* [FILTERS.md](../FILTERS.md): the filter-domain language. It is part of this specification by reference (FR-FLT).
* [CONTRIBUTING.md](../CONTRIBUTING.md): code organization and development rules.
* [SECURITY.md](../SECURITY.md): security policy.
* Odoo's logging format: `%(asctime)s %(pid)s %(levelname)s %(dbname)s %(name)s: %(message)s`.

### 1.5 Overview

Section 2 describes the product's context, users and constraints. Section 3 lists the requirements. Section 4
says how each group of requirements is verified. The appendices list the accepted log formats, the fixed limits
and the HTTP API.

---

## 2. Overall description

### 2.1 Product perspective

logtool is a standalone tool. It does not connect to Odoo, to PostgreSQL or to the network. Its only input is log
files, which the user copies from a server or downloads from a hosting dashboard.

```
 log files (plain, .gz, .bz2)
        │
        ▼
 ┌───────────────────── user's machine ──────────────────────┐
 │  browser ──HTTP (loopback)──▶ logtool server (Flask)       │
 │  app.js                        app.py: files, caches,      │
 │                                 selections, export          │
 │                                     │                        │
 │                                     ▼                        │
 │                                profiler.py: parsing,         │
 │                                classification, merging,      │
 │                                worker-process pool           │
 │                                     ▲                        │
 │  terminal ── logtool-report ───────┘                         │
 └──────────────────────────────────────────────────────────────┘
```

### 2.2 Product functions

* Load one or more logs (plain, gzip, bzip2), in parallel, with a parse cache on disk.
* Parse and classify every log entry; time requests and jobs; group warnings and errors.
* Select files, databases and a time interval; filter by details or by any domain condition.
* Show statistics, a requests-over-time histogram, operation / app / type tables, a warnings-and-errors table.
* Open any row to see its numbers, example lines, details, related operations, the lines logged during it and its
  raw lines with their surrounding context.
* Export a pseudonymized JSON summary; import one again.
* Read only one database of a multi-database log; list the databases of a log without loading it.
* Print a text report from the command line.

### 2.3 User classes

| user | needs |
|---|---|
| Odoo developer / consultant | Find the slow operations, the N+1 suspects, the failing cron, the module that owns them. Daily user. |
| Support / hosting engineer | Explain a slowdown at a given time; trace an error to a client, a user or a record; share findings without sharing the raw log. |
| Integrator / partner | Show a customer's workload; compare before and after a change. |
| AI agent assisting one of the above | Read an export summary; write filter domains (FILTERS.md §10). |

Users are technical but should not need to read the code or the log format to use the tool.

### 2.4 Operating environment

* Linux with Python 3.9 or newer and Flask 2.3 or newer.
* A current desktop browser (Chromium, Firefox, Safari) on the same machine. The in-browser database scan of
  `.gz` files needs `DecompressionStream`.
* Typical inputs: one log of 10 MB to 1 GB, or tens of daily logs adding up to several GB.

### 2.5 Design and implementation constraints

* **C-1** The parser (`logtool/profiler.py`) shall use the Python standard library only.
* **C-2** The web server's only runtime dependency shall be Flask.
* **C-3** The frontend shall be plain JavaScript, HTML and CSS served by the application, with no build step and
  no external resources (scripts, fonts, styles, images).
* **C-4** The software shall work without network access.
* **C-5** The software shall be installable with pip from a source tree, an sdist, a wheel or a Git URL, and with
  the bundled `install.sh`.

### 2.6 Assumptions and dependencies

* **A-1** Logs use Odoo's default header format, or one of the variants in Appendix A.
* **A-2** Log timestamps are in one clock (normally UTC) and lines are at most about 3 minutes out of order.
* **A-3** Access lines carry the timing triplet (Odoo's default). Lines without it are counted as untimed.
* **A-4** The ORM call trace (`odoo.api: call ...`) is only present in logs written at DEBUG level; features that
  rely on it (logged records / users) show less without it, and the export says so.

---

## 3. Requirements

### 3.1 External interfaces

#### 3.1.1 User interface

* **FR-UI-1** The page shall have a top bar (title, time interval, export, import, add logs, reset), a data
  selection area (Files, Databases), general statistics, the histogram panel, the operations table, the selected
  items table and the warnings-and-errors table.
* **FR-UI-2** While nothing is loaded, the drop zone shall be large and the page shall say how to start.
* **FR-UI-3** Long operations (upload, parse, analysis, scan) shall show a progress or busy indicator.
* **FR-UI-4** Errors shall be shown as a readable notice; the page shall stay usable after any failed request.
* **FR-UI-5** Panels shall be collapsible; view preferences (hidden columns, grouping, timezone, fit) shall persist
  per browser.

#### 3.1.2 Command-line interfaces

* **FR-CLI-1** `logtool [LOG ...] [--host H] [--port P] [--no-browser] [--db NAME] [--version]` shall start the
  web server, preload the given logs and open a browser tab when a desktop session exists.
* **FR-CLI-2** `logtool-report LOG [LOG ...]` shall print the analysis as text; `-` shall read stdin. Options shall
  cover: rows shown (`--top`, `--min-total`, `--min-calls`, `--fold`, `--by`), extra sections (`--events`,
  `--logins`, `--hours`, `--unclassified`, `--all`), selection (`--since`, `--until`, `--db`, `--only-db`,
  `--exclude-db`, `--grep`), outputs (`--csv`, `--json`, `-o`), parallelism (`--jobs`), `--selftest` and
  `--version`.
* **FR-CLI-3** `--since` / `--until` shall accept a date, a minute or a second, separated by a space or `T`. A
  bound shall cover what it names (`--until 2026-09-14` is the whole day). An invalid value shall be an error.
* **FR-CLI-4** `logtool-report` shall exit with status 0 on success and 2 when no line was recognised or no line
  matched the filters, saying which of the two happened.

#### 3.1.3 HTTP API

The browser talks to the server over a JSON API (Appendix C). It is internal: its shape may change between
versions. Requirements on it:

* **FR-API-1** Every route shall answer JSON. Invalid input (bad numbers, malformed JSON, unknown ids, invalid
  domains) shall give status 400 with an `error` message; an unexpected failure shall give 500 with a message,
  and shall not stop the server.
* **FR-API-2** Analysis routes shall accept the selection as query parameters: `exclude_file`, `exclude_db`,
  `from` / `to` (seconds), `ff` (item filters, JSON), `xd` (custom conditions, JSON).

#### 3.1.4 Files

* **FR-IO-1** Input: Odoo log files, plain text (any line ending, invalid UTF-8 replaced), gzip (`.gz`) or bzip2
  (`.bz2`), chosen by file suffix; exported summaries (`.json`).
* **FR-IO-2** Output: the export summary (JSON); `logtool-report` text, CSV and JSON.

### 3.2 Functional requirements

#### 3.2.1 Loading (FR-LOAD)

* **FR-LOAD-1** Logs shall be added by drag and drop, a file picker, or on the command line; several at once.
* **FR-LOAD-2** Each loaded file shall be shown with its name and size, and can be unloaded. Files that cannot be
  read, or contain no recognisable log lines, shall be marked with the reason; the other files shall stay usable.
* **FR-LOAD-3** Files added together shall be parsed together: compressed files decompressed concurrently, then
  every chunk of every file on one shared worker pool.
* **FR-LOAD-4** A compressed log shall be decompressed once to a temporary file when the disk has room for it
  (keeping a safety margin), so it is parsed and seeked like a plain one; otherwise it shall be read in one piece.
* **FR-LOAD-5** Plain logs larger than a threshold shall be split into chunks of at most 64 MB, each cut at the
  start of a log entry, and parsed in parallel. The result shall equal a serial parse of the whole file.
* **FR-LOAD-6** Parsed chunks shall be cached on disk, keyed by the file's size and first and last MiB, the
  parser's code and the only-read database. Loading the same file again shall read the cache. The cache shall be
  bounded (4 GB, least recently used first) and can be disabled (`LOGTOOL_PARSE_CACHE=0`).
* **FR-LOAD-7** Other requests (file list, popups) shall keep being answered while new files are parsed.
* **FR-LOAD-8** Reset shall unload every file, clear every filter and setting (including the only-read database)
  and restore the default view.
* **FR-LOAD-9** Unloading a file, including while it is being decompressed or parsed, shall free its caches and
  delete every temporary copy of it.

#### 3.2.2 Parsing and classification (FR-PARSE)

* **FR-PARSE-1** A log entry starts at a header line matching one of the formats of Appendix A with a real
  calendar timestamp; every other line (traceback, message body, blank, impossible timestamp) belongs to the
  entry above it. ANSI color codes shall be ignored.
* **FR-PARSE-2** Every access line shall be classified into (type, app, operation) from its method, path and
  status: JSON-RPC `call_kw` / `call_button` by model and method, `/xmlrpc` and `/jsonrpc` services, web client
  routes, reports, assets, website and portal pages, attachments, long polling, websocket upgrades, scanner probes,
  and a catch-all. Ids, tokens, hashes and slugs shall be collapsed.
* **FR-PARSE-3** Requests shall be timed from the timing triplet: SQL seconds, Python seconds, queries. Requests
  without it shall be counted as untimed. Held-open connections (websocket upgrades) shall not count as work.
* **FR-PARSE-4** Jobs shall be timed from their own messages: cron runs (Odoo 17 to 20 message shapes, including
  start / done pairs), automation rules (including time-based rules paired from "Starting" to "done"), and module
  loading. Their time has no SQL / Python split.
* **FR-PARSE-5** Each operation shall keep: calls, errors (5xx), SQL / Python / unsplit seconds, queries, the
  maximum, a percentile sample (at most 20,000 values, merged in proportion to the calls each part saw), HTTP
  status counts, per-minute activity and example lines (first, slowest, first failing).
* **FR-PARSE-6** Issues shall be grouped by (level, logger, message template). Each shall keep its count, first
  and last time, a sample line, the exception class of its traceback, the server action named in it, its
  per-minute counts, and its details (the numbers behind each `#`, user ids, records).
* **FR-PARSE-7** Untimed lines (issues, ORM trace lines, add-on INFO / DEBUG lines) shall be claimed by the request
  or job the same worker logs next, when it started before them. The operation shall keep the shapes of those
  lines and the users and records they name (counted once per request); an issue shall keep the operations it was
  logged during.
* **FR-PARSE-8** Logins shall be paired with the client IP of the same worker's access line.
* **FR-PARSE-9** Every database's lines shall be kept apart, so any subset of databases can be combined later
  without parsing again. Lines without a database (`?`, `None`, or a format without the column) shall form their
  own group.
* **FR-PARSE-10** The parser shall report which header formats and access-line variants it recognised, and the
  number of lines per log level.

#### 3.2.3 Only-read database (FR-DB)

* **FR-DB-1** The user shall be able to set one database to read, before or after loading, in the Databases box
  or with `logtool --db NAME`; `logtool-report --only-db NAME` shall do the same.
* **FR-DB-2** With it set, the entries of every other database (header and continuation lines) shall be skipped
  right after their header is matched, without further parsing. Entries without a database shall always be read.
* **FR-DB-3** Changing it shall parse the loaded logs again; no result computed under the previous setting shall
  be shown or cached afterwards.
* **FR-DB-4** The raw-line views shall skip the same entries, so their results agree with the counts.
* **FR-DB-5** *Find databases in a log file* shall list the databases of user-chosen plain or `.gz` files with
  their line counts, searchable, read in the browser without uploading or loading them. It shall recognise the
  same header formats as the parser (Appendix A), including colored and prefixed lines. Choosing a database from
  the list shall set it as the only-read database.

#### 3.2.4 Selection (FR-SEL)

* **FR-SEL-1** The user shall be able to include or exclude each loaded file, and each database of the included
  files, keeping at least one of each. On first load the busiest database and the lines without a database shall
  be selected.
* **FR-SEL-2** The user shall be able to select a time interval by clicking histogram bars or typing timestamps
  (`YYYY-MM-DD HH:MM[:SS]`, the log's clock). Impossible timestamps shall be rejected. An interval outside the
  logs shall be refused; one covering them all shall mean "whole selection".
* **FR-SEL-3** An interval shall reuse the cached chunks that lie inside it and re-read only the chunks it cuts.
* **FR-SEL-4** The page shall only ever show the answer to the latest selection; answers to earlier ones that
  arrive late shall be dropped.

#### 3.2.5 Filters (FR-FLT)

* **FR-FLT-1** Item filters: clicking a detail value of a row shall keep only that row's occurrences carrying it.
  Values of one detail combine with OR, different details with AND. The selected logs shall be re-parsed with the
  filter, and every view, count and export shall follow it.
* **FR-FLT-2** A warning's client filter (client IP, session) shall be checked against the request it was logged
  during. Warnings logged during a request the filters leave out shall be left out with it.
* **FR-FLT-3** The filter editor shall show the complete page state as a domain and apply an edited domain,
  implementing the language of FILTERS.md exactly: fields, operators, prefix logic, "not applicable" semantics,
  the selection fields' restrictions, view settings and error messages.
* **FR-FLT-4** Every domain the application writes (item filters on any detail, view settings) shall be accepted
  when applied back.
* **FR-FLT-5** An invalid domain shall be rejected before anything runs, with a message naming the problem.

#### 3.2.6 Statistics, histogram and tables (FR-VIEW)

* **FR-VIEW-1** General statistics shall show the time window, log entries and lines, requests (rate, unparsable),
  request time, background jobs, the SQL / Python split, SQL queries, errors and warnings (5xx, ERROR, WARNING),
  operations / apps / types, HTTP statuses, and the filter editor.
* **FR-VIEW-2** The histogram shall show requests and time per bucket for the total and for each user-chosen item
  (search term, table row, issue), with bars and lines per series (requests, average ms, total ms, each split into
  Python and SQL). Bucket sizes from 1 minute to 1 week, or automatic; fit or scroll; a display timezone that only
  relabels the chart.
* **FR-VIEW-3** *Focus interval* shall re-bin the selected interval at a finer resolution: down to 1 minute for up
  to a day.
* **FR-VIEW-4** The operations table shall group by operation, app or type, with columns type, app, total s,
  % of total, SQL s, % SQL, Python s, % Python, calls, average ms, p95 s, max s, queries per call, ms per query,
  5xx and bottleneck; sortable, searchable, columns can be hidden. It shall show the long tail (up to 5,000 rows).
* **FR-VIEW-5** The warnings-and-errors table shall list the most frequent issues (60, plus any pinned one) with
  level filter and search.
* **FR-VIEW-6** Right-clicking a row shall select it: it becomes a histogram item and a row of the selected items
  table.

#### 3.2.7 Detail pages (FR-DET)

* **FR-DET-1** Opening an operation, app or type shall show its numbers, example lines, the details behind the
  grouping (most frequent values first, with counts and average seconds), its operations (app / type), the lines
  logged while it ran and the users / records they name, and its raw lines.
* **FR-DET-2** Opening an issue shall show its numbers, details, the operations it was logged during and its
  occurrences.
* **FR-DET-3** Raw lines shall be listed in log order, or slowest first for operations, up to 200, honoring the
  selection and every filter; they shall be re-read from the files in parallel, only over the chunks that overlap
  the interval.
* **FR-DET-4** *Log around it* shall show up to 200 lines on either side of an occurrence, color coded (the
  occurrence, other occurrences of the same item, its parent request, what was logged during it), with copy.
* **FR-DET-5** A file deleted from disk after loading shall be reported as such when its lines are needed.

#### 3.2.8 Export and import (FR-EXP)

* **FR-EXP-1** *Export summary* shall download a JSON document of the current selection with the statistics, the
  tables, every distinct issue (up to 5,000), the details behind each row, timelines per cron / automation rule
  and busiest operations, per-client activity, logins, lines per level, `limits` (what this log cannot show) and
  `fields` (what each key means).
* **FR-EXP-2** The export shall contain no database names, no file names and no raw log lines. Client IPs, session
  ids and logins shall be replaced by stable pseudonyms (`ip-1`, `session-1`, `user-1`). Free text shall be
  scrubbed of the selection's database and file names (whole words, at least 4 characters, not common words),
  email addresses, and IPv4 / IPv6 addresses. User and record ids shall be kept.
* **FR-EXP-3** Importing an exported summary shall show it read-only (no file, database or interval selection).
  A file that is not a valid export shall be marked as such.

### 3.3 Non-functional requirements

#### 3.3.1 Performance (NFR-PERF)

Reference machine: 12 cores, SSD. Reference inputs: one 200 MB log of 34 days (about 1.1 million requests), and
48 daily logs of 2.9 GB in total.

| id | requirement |
|---|---|
| NFR-PERF-1 | Loading the 200 MB log shall take at most 5 s; the first analysis at most 2 s. |
| NFR-PERF-2 | A new time interval shall be answered in at most 1 s; anything already computed in at most 0.2 s. |
| NFR-PERF-3 | Raw log lines shall be answered in at most 5 s on the 2.9 GB set. |
| NFR-PERF-4 | Loading logs from the parse cache shall be at least 3 times faster than parsing them. |
| NFR-PERF-5 | With an only-read database set, a log of many databases shall load faster than without, in proportion to the share of lines skipped. |
| NFR-PERF-6 | The in-browser database scan should read at least 100 MB per second. |
| NFR-PERF-7 | Memory shall stay under 1 GB for the 200 MB log and under 4 GB for the 2.9 GB set, with a filter. |

#### 3.3.2 Reliability (NFR-REL)

* **NFR-REL-1** No input file shall crash the server or the report: empty, binary, truncated or corrupt
  compressed files, very long lines, invalid UTF-8, impossible timestamps and unknown formats shall be handled and
  reported.
* **NFR-REL-2** A file that breaks half way shall keep what was read before the break, and say so.
* **NFR-REL-3** Concurrent requests shall never deadlock, and shall never serve a result computed for another
  selection, filter or only-read database.
* **NFR-REL-4** If the worker pool dies, parsing shall continue serially.
* **NFR-REL-5** Running out of disk space shall be reported as such, with what the user can do.
* **NFR-REL-6** Temporary files and worker processes shall be cleaned up when the server stops, whether by
  Ctrl+C, SIGTERM or a closed terminal (SIGHUP); files left by a crash shall be deleted at the next start, without
  touching those of another running instance.

#### 3.3.3 Security and privacy (NFR-SEC)

* **NFR-SEC-1** The server shall listen on the loopback interface by default and warn when told to listen on
  another address.
* **NFR-SEC-2** When bound to loopback, requests whose `Host` is not a loopback name shall be refused (403).
  State-changing requests whose `Origin` is another site shall be refused (403).
* **NFR-SEC-3** The software shall make no outbound network connection and load no external resource.
* **NFR-SEC-4** Uploaded files shall be stored under random names in a per-process directory; user-supplied names
  shall never be used as paths.
* **NFR-SEC-5** Cached data that is deserialized (pickle) shall only be read from a directory owned by the current
  user and not writable by others.
* **NFR-SEC-6** Every log-derived string shown in the page shall be escaped (no HTML injection from log content,
  file names or database names).
* **NFR-SEC-7** The export shall meet FR-EXP-2.

#### 3.3.4 Usability (NFR-USE)

* **NFR-USE-1** A first-time user shall get from an Odoo log to the slowest operations by dropping the file on the
  page, without configuration.
* **NFR-USE-2** Every control whose meaning is not obvious shall have a tooltip.
* **NFR-USE-3** Times shall be shown in the log's own clock (`YYYY-MM-DD HH:MM:SS`), except the histogram's
  optional display timezone, which shall be labeled.
* **NFR-USE-4** The page shall be usable with the keyboard for its main actions (add logs, dialogs, tabs).

#### 3.3.5 Portability and installation (NFR-PORT)

* **NFR-PORT-1** The software shall run on Linux with CPython 3.9 to 3.13.
* **NFR-PORT-2** `install.sh` shall install into a private virtualenv, link both commands into `~/.local/bin`,
  add that directory to `PATH` if needed, upgrade in place when run again, and uninstall cleanly.
* **NFR-PORT-3** The wheel shall contain the templates and static files.

#### 3.3.6 Maintainability (NFR-MAINT)

* **NFR-MAINT-1** The parser shall carry a built-in self-test (`--selftest`).
* **NFR-MAINT-2** The web application shall have an automated test suite covering broken input, API edge cases,
  intervals against straight parses, filters, export scrubbing and the regressions of every fixed bug, runnable
  with and without pytest.
* **NFR-MAINT-3** Continuous integration shall run the self-test, the test suite, a frontend syntax check and a
  package build on every push and pull request.
* **NFR-MAINT-4** The package version shall be defined in one place (`logtool/__init__.py`).

### 3.4 Data requirements

* **DR-1** All analysis state shall be in memory, per server process; nothing persists between runs except the
  parse cache and per-browser view preferences (localStorage).
* **DR-2** Fixed limits (Appendix B) shall bound memory per operation, per detail and per client, collapsing the
  rest into an "(other values)" / "(other clients)" entry rather than dropping counts.

---

## 4. Verification

| requirements | how they are verified |
|---|---|
| FR-PARSE, FR-CLI | `python -m logtool.profiler --selftest`: classification of every access-line shape, jobs, issues, formats, impossible timestamps. |
| FR-LOAD, NFR-REL | `tests/test_logtool.py`: empty, binary, corrupt and truncated gzip, bz2, CRLF / latin-1, very long lines, many operations, unload during decompression, parse cache reuse and keys, workers stopped on SIGTERM. |
| FR-LOAD-5, FR-SEL-3 | Tests comparing chunked and interval results with a straight parse (`test_interval_from_chunks_matches_a_straight_parse`, `test_chunks_are_cut_at_log_entries`, ...). |
| FR-DB | `test_only_db_reads_one_database_and_the_lines_without_one`, `test_rows_are_sent_again_after_the_database_to_read_changes`, `test_reset_reads_every_database_again`; the scan in a browser. |
| FR-FLT | Fact-filter, domain round-trip, custom-condition and FILTERS.md example tests; `test_job_details_and_bare_numbers_make_valid_domains`. |
| FR-DET | Detail, raw-line, slowest-first and log-context tests; scanners agree with the parser (`test_the_scanners_count_like_the_parser`, colored logs). |
| FR-EXP | `test_export_pseudonymizes_and_explains`, `test_export_keeps_common_words_and_blanks_real_names`, import tests. |
| FR-API, NFR-SEC-2 | Bad input gives 400 tests; cross-origin and foreign-host tests, IPv6 loopback. |
| NFR-SEC-5 | `test_parse_cache_refuses_a_directory_others_can_write`. |
| NFR-PERF | Measured manually on the reference inputs before a release (README, *Performance*). |
| NFR-PORT, NFR-MAINT-3 | CI matrix on Python 3.9 to 3.13 and the package job. |
| FR-UI, NFR-USE | Manual check in a browser before a release. |

---

## Appendix A. Accepted log formats

Header lines (tried in this order; the first that matches wins):

| name | shape |
|---|---|
| `odoo` | `2026-09-13 22:00:09,711 2422355 WARNING mydb py.warnings: message` |
| `odoo-nodb` | the same without the database column |
| `prefixed` | anything (syslog, journald, docker prefix) followed by either of the above |

The timestamp may use `T` instead of a space and `.` instead of `,`; milliseconds are optional. A database name
never ends with `:`. `?` and `None` mean "no database".

Access lines:

| Odoo version | logger | shape |
|---|---|---|
| 17.0 – 19.0 | `werkzeug` | `ip - - [date] "GET /x HTTP/1.1" 200 - 91 0.412 0.663` |
| 19.4 – 20.0 | `odoo.http.server` | `ip SESSION - [date] "GET /x HTTP/1.1" 200 7454 91 0.412 0.663 rw` |

## Appendix B. Fixed limits

| limit | value |
|---|---|
| operations sent to the table | 5,000 |
| operations with their own timeline | 500 |
| issues in the table / in an export | 60 / 5,000 |
| percentile sample per operation | 20,000 values |
| distinct values kept per detail | 200 (the rest counted as "(other values)") |
| values shown per detail | 12 |
| clients with activity over time | 500 |
| untimed line shapes kept per operation | 25 |
| raw occurrences listed | 200 |
| context lines on either side | 200 |
| chunk size | 8 – 64 MB |
| parse cache | 4 GB |
| assumed out-of-order tolerance | about 3 minutes |

## Appendix C. HTTP API

| method and path | purpose |
|---|---|
| `GET /` | the page |
| `GET /api/files` | loaded files |
| `POST /api/files` | upload files (multipart, field `files`) |
| `DELETE /api/files/<id>` | unload a file |
| `POST /api/reset` | unload everything, clear every setting |
| `GET /api/only_db`, `POST /api/only_db` | read / set the only-read database (`{"db": "name"}`, `""` for every database) |
| `GET /api/analyze` | statistics, tables, histogram for the selection (`init`, `rows`, `hist`, `pins` besides the selection) |
| `GET /api/detail` | an operation / app / type's detail page (`by`, `name`) |
| `GET /api/issue_detail` | an issue's detail page (`level`, `logger`, `message`) |
| `GET /api/timeline` | the focused histogram (`issues`, `pin_op`) |
| `GET /api/op_lines` | raw lines of an operation / app / type (`order=slow` for slowest first) |
| `GET /api/issue_lines` | occurrences of an issue |
| `GET /api/issue_context` | lines around one occurrence |
| `GET /api/domain`, `POST /api/domain` | the page state as a domain; check and apply a domain (`{"domain": "..."}`) |
| `GET /api/export` | the export summary (attachment) |
