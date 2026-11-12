# Changelog

All notable changes to logtool. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Slow-query logs**: PostgreSQL's `pg_slow_queries.log` loads like any log, alone or beside the Odoo logs, with
  its own parser. Its queries follow the file, database and time selection and the filter editor (kind
  `slow_query`, fields `duration` and `sql`). The table lists them *By slow query*, one row per shape (the runs of
  one query whatever its values); a query's page shows its SQL highlighted and laid out, the runs of its shape and
  what ran at the same time. Right-click a row to plot its runs on the histogram (`chart.item` `{"slow_query": shape}`). Operations, apps and
  types list the slow queries that overlapped their calls under *Logged during*. The exports count them; only
  Export HTML carries their SQL, with every literal replaced by `?`.
- **Duration item filter**: click a bar of an operation's, app's or type's *Histogram* tab to keep only its calls in
  that span of durations; the bars keep showing every call, so more spans can be added. In the filter editor it is the `duration ms` field, spans of ms like `"100-150"` or
  `"5000-"`, usable as an item filter or a custom condition.
- **Requests during runs**: a cron job's details show the average time of the requests that ran while it ran (their
  span overlaps one of its runs), and a tab with a histogram of how long they took, beside the average of every
  request of the selection.
- **Request time histogram**: a *Histogram* button on the *Request time* statistic shows how long the requests of
  the selection took, with their average marked.
- **Only this IP**: a button in every *client IP* table narrows the whole report to that client. Only its requests
  and the warnings logged during them are kept; jobs and unclaimed warnings are dropped. While it is on, the top bar
  shows a *Client IP* button that clears it. In a domain, this is the new top-level `ip` field.
- **Export text**: downloads the current selection as a plain-text report. It has three parts: an ASCII histogram
  of the requests; every operation with its calls, average ms and the details behind its grouping (client IPs
  left out); then each occurrence's log line with the 3 lines before and after it, oldest first. It holds one
  database and the lines without one: with two databases selected, the button is disabled. Its raw lines are the
  selected database's own: other databases' lines and lines without a database never appear, not even as context.
- **Export HTML** no longer carries what the filter names. The page says only which kinds of filter are on and how
  many (the server writes this itself and ignores the browser's text), and the table and errors search boxes are
  emptied.
- **Export HTML**: downloads the page as one standalone, read-only `.html` file of the current selection. Only the
  selected databases are in it (the others are not named), as they are; what lines without a database logged is
  censored (IPs, sessions, logins, emails, user and record ids pseudonymized); the details popup is kept for the top
  100 operations, every cron / automation / module-loading operation and the top 50 apps, types and errors. With a time interval or a focus, its histogram shows just that span.
- **Histogram** tab in the details of an operation, app or type: how many calls took how long (ms, log scale), with
  the average, median and p95 marked.

### Fixed

- With a warning or error selected in the histogram, choosing a time interval in which it was not logged emptied
  the operations table: the pinned issue's rows replaced the operation rows.
- With an item filter or an IP filter, a login line whose request is filtered out no longer pairs with that worker's
  next login request. Before, the export's logins and a warning's client accounts could show an account logging in
  from the wrong IP.
- With an item filter or an IP filter, the WebSocket / long-poll connections counted as *held open* are only those
  the filter keeps.

## [1.2.0] - 2026-10-11

### Added

- **Only read database**: set a database before loading logs (Databases box, `logtool --db NAME`, or
  `logtool-report --only-db NAME`). Every other database's lines and their tracebacks are skipped right after their
  header is read, so multi-database logs load faster and use less memory. Lines without a database are always read.
- **Find databases in a log file…**: lists the databases of a plain or .gz log, with line counts and a search box,
  read in the browser without uploading the file.
- `--version` on `logtool` and `logtool-report`.
- `logtool-report --since` / `--until` accept a date, a minute or a second, with a space or a `T`.
- The filter editor accepts the automation and module-loading details `rule id`, `records processed` and
  `modules`, and the bare `#` detail of a message that starts with a number.
- The tests also run under pytest; continuous integration on Python 3.9–3.13.
- Project documents: software requirements specification (`docs/SRS.md`), CONTRIBUTING, SECURITY, this changelog.

### Fixed

- Export summary: a log file named `odoo.log` turned every `odoo` in the export into `[redacted]`. Names are now
  blanked as whole words only, and every log suffix is stripped (`acme-prod.log.gz` blanks `acme-prod`). IPv6
  addresses are blanked too.
- Percentiles (p95) were wrong after merging chunks of very different sizes: each chunk's sample now weighs as
  many calls as it stands for.
- The parse cache could return another file's results for logs between 1 and 2 MiB that share their first MiB.
- The parse cache is only read from a directory of the user's own that nobody else can write to (it is unpickled).
- Colored console logs: raw log lines, *Log around it* and the database scan found nothing, and big files could
  be cut between a message and its traceback.
- A line with an impossible timestamp (`2026-02-31`) was a log entry for the raw-line views but a continuation
  line for the parser.
- Lines of an excluded database could still claim warnings in the raw-line views.
- A late answer to an earlier selection could replace a newly picked time interval, or bring back the results of
  the database read before.
- Changing *Only read database* while a filter was being applied could cache the old database's results.
- Changing *Only read database* no longer freezes the page while a log is loading.
- Reset now also clears *Only read database*.
- Typed timestamps such as `2026-02-31 10:00` or `25:99` were silently rolled over instead of rejected.
- Exception classes were not counted in the web app, and an ERROR without a traceback could take the next
  unrelated one's.
- Unloading a .gz / .bz2 file while it was being decompressed left the decompressed copy on disk.
- JSON request bodies that are not objects, a non-numeric comparison on a number field, and an imported summary
  with a broken file count now give a clear error instead of a server error or an empty result.
- `Host: [::1]` (IPv6 loopback without a port) was refused.
- `logtool-report - -` crashed; a filter that matches nothing was reported as "no Odoo log lines recognised".
- The worker pool's `pymp-*` folders piled up in `~/.cache/logtool`: they now go to the system temp directory,
  and unused ones left by earlier versions are removed at start.
- An upload error was not shown when the file list could not be reloaded either.
- Stopping `logtool` with `kill` (SIGTERM) or by closing its terminal left its worker processes running and its
  uploaded files on disk; it now cleans up as on Ctrl+C. `logtool-report` stops on Ctrl+C or SIGTERM without a
  traceback, with exit status 130.

## [1.1.0] - 2026-09-30

First packaged release: the `logtool` web UI and the `logtool-report` command, with `install.sh`.

[1.2.0]: https://github.com/alwhi-odoo/logtool/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/alwhi-odoo/logtool/releases/tag/v1.1.0
