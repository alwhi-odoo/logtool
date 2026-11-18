# logtool: architecture

Where the code is, how the pieces depend on each other, and the few rules that keep it that way. Written for
people and for AI agents changing the code: read this first, then open only the module you need.

What logtool does is in [README.md](README.md); what it must do, in [SRS.md](SRS.md); the filter language, in
[FILTERS.md](FILTERS.md).

## The shape

```
 browser (static/*.js)   ──HTTP──▶ logtool/web/      Flask app: files, caches, selections, details, exports
                                        │  only as olp.<name>
                                        ▼
 terminal: logtool-report ──────▶ logtool/profiler.py   the parser's front door (re-exports)
                                        │
                                        ▼
                                  logtool/parser/      standard library only: parsing, classification,
                                                       merging, filters, the worker-process pool
```

Two front doors keep their old names, so the commands and `python -m` keep working:

| module | what it is |
|---|---|
| `logtool/app.py` | `logtool.app:main` is the `logtool` command, `logtool.app.app` the Flask app. Code: `logtool/web/`. |
| `logtool/profiler.py` | `logtool.profiler:main` is `logtool-report`; also everything the web app uses of the parser, re-exported. Code: `logtool/parser/`. |

## `logtool/parser/`: logs in, numbers out

Standard library only (SRS C-1). Each module imports only from the ones above it.

| module | what is in it |
|---|---|
| `lines.py` | The shape of a log line: header formats (`HEADERS`), the access line (`ACCESS`), timestamps (`stamp_minute`), message templates (`normalize`), `_parse_header`. |
| `logio.py` | Reading log files: plain / .gz / .bz2 (`opener`), one byte range of a plain file (`_lines`), line numbers (`_numbered_lines`). |
| `facts.py` | What grouping collapses (client IP, session, URL, user id, records, cron id ...): `_fact`; fact filters: `_facts_ok`; the fields a domain can test: `request_fields`, `job_fields`, `issue_fields`. |
| `classify.py` | A request → (type, app, operation): `classify`. A non-request line → event family. |
| `jobs.py` | Cron jobs, automation rules, module loading (`classify_job`); cron runs whose worker was killed (`pair_cron_kills`). |
| `bucket.py` | `Bucket`: the numbers of one operation. `PackedHist`: its per-minute data, packed. |
| `report.py` | The text report (`write_report`), the table rows (`as_rows`), the bottleneck `verdict`. |
| `domain.py` | The filter domain: fields, operators, validation, prefix list ↔ tree (`dom_tree`). |
| `domain_eval.py` | A domain evaluated on one occurrence (`dom_compile`). |
| `domain_filters.py` | A domain ↔ the app's own filters (`dom_split`, `dom_render`). |
| `profile.py` | `Profile`, the accumulator. `feed()` reads every line: the hot loop of the whole tool. |
| `scan.py` | Raw lines re-read on demand: the occurrences of one issue / operation, the lines around one. |
| `slowq.py` | PostgreSQL slow-query logs. |
| `parallel.py` | The worker pool, chunks (`Chunk`), `profile_files`, `scan_pieces`. |
| `selftest.py` | `logtool-report --selftest`. |
| `cli.py` | The `logtool-report` command line. |

## `logtool/web/`: the app over the parser

Each module imports only from the ones above it. Importing the package registers every route.

| module | what is in it |
|---|---|
| `state.py` | Everything kept in memory: `FILES`, `CHUNKS`, the caches, the locks, the limits (`MAX_*`), the database to read (`ONLY_DB`, `set_only_db`). |
| `util.py` | Log-clock time conversions (`_iso`, `_secs`), the LRU cache helpers. |
| `storage.py` | The upload folder (`UPLOAD_DIR`), the temp directory, cleaning up after old runs. |
| `parse_cache.py` | Parsed chunks kept on disk between runs. |
| `loading.py` | Recognising a file (Odoo log, slow-query log, exported summary), decompressing, parsing it once. |
| `slowq.py` | The slow queries of the selection: table rows, export. |
| `selection.py` | Files → databases → time interval → one merged `Profile` (`_select`, `_base`, `_cut`, `_prof_for`); the pieces a raw-line scan reads (`_pieces`). |
| `results.py` | A merged `Profile` as JSON: totals, tables, facts, issues, timelines. |
| `durations.py` | The call-duration histogram. |
| `analysis.py` | `analyze()` (the answer to `/api/analyze`), the focused `timeline()`. |
| `details.py` | The details popup of one row. |
| `slowq_detail.py` | One slow query's form, what ran meanwhile. |
| `scrub.py` | Blanking and pseudonymizing for the exports. |
| `export_json.py`, `export_html.py`, `export_text.py` | The three exports. |
| `reqargs.py` | The arguments of an analysis request: interval, fact filters, domain, pins. |
| `domain_state.py` | A domain (text) → the app's filter state. |
| `server.py` | The Flask `app` object, the request guard, the page. |
| `routes_files.py`, `routes_analysis.py`, `routes_lines.py`, `routes_export.py` | The routes, by subject. |
| `cli.py` | The `logtool` command line. |

## `logtool/static/`: the page

Plain JavaScript, one stylesheet (`style.css`) and one template (`logtool/templates/index.html`): no build step, no
modules, nothing fetched from elsewhere (SRS C-3). The scripts are classic scripts sharing one global scope, loaded
in the order `index.html` lists them (its `scripts` list), which is the order of this table:

| script | what is in it |
|---|---|
| `core.js` | `state` (everything the page remembers), `$` / `esc` / `store`, the number and time formats, the table columns (`COLS`), the tooltip, the busy indicator. |
| `api.js` | `api()` (in an Export HTML page, `snapApi`: the answers built into it), the file list (`refresh`, `upload`, `unload`, `resetAll`), `analyzeNow`, the export downloads (`exportAs`, `exportHtml`). |
| `selection.js` | The filter row: files, databases, the time interval, the histogram's timezone, the notice line, the database to read, the scan of a file for its database names. |
| `stats.js` | `renderAll` (every panel redrawn from `state.data`), the statistics cards, the *Request time* popup. |
| `filter_editor.js` | The *Filter* card, the view settings as the server reads and writes them (`viewState`, `applyView`), the domain popup. |
| `histogram.js` | The requests-over-time chart: series, bucket sizes, drawing (`renderHist`), focus, the clicks that set the time interval. |
| `tables.js` | The operations / apps / types table (`renderTable`), its totals, the selected items, the errors and warnings (`renderIssues`). |
| `chart_items.js` | Chart items: search tags, pinned errors and slow queries, the *Chart items & series* menu; a click on a table row. |
| `slowq.js` | Slow queries: their table, SQL laid out and highlighted (`sqlHtml`), the form of one (`openSlowq`). |
| `rawlines.js` | The raw log lines of the open form: occurrences, the lines around one. |
| `form.js` | The full-screen form every details popup opens in: tabs, Back, Escape (`showForm`, `closeModal`). |
| `facts.js` | Item filters (`toggleFact`, their popup, one client IP) and the tables of what grouping collapsed (`factsSection`). |
| `details.js` | The details popups: one warning / error (`openIssue`), one operation / app / type (`openDetail`), duration histograms. |
| `events.js` | Loaded last: every click / input / change / drag-and-drop handler, then the start of the page. |

A function body may call anything in any script (it runs after all of them loaded). Code that runs while a script
loads (a top-level `const x = f()`, `addEventListener("change", handler)`) can only use what the same script or an
earlier one defines: a function is not hoisted across scripts. Listeners on `document` run in load order.

*Export HTML* (`web/routes_export.py`) writes the same page with `style.css` and every script inline, in the same
order, so it opens with no server: a new script only has to be added to the `scripts` list of `index.html`.

## How a request is answered

1. **Load** (`web/loading.py`): each file is parsed once by `olp.profile_files`, in chunks, in worker processes.
   Every chunk keeps one `Profile` per database (`Chunk.parts`).
2. **Select** (`web/selection.py`): the chunks of the selected files and databases are merged into one `Profile`.
   A time interval reuses the chunks inside it and re-reads its two edges. A fact filter parses the files again.
3. **Show** (`web/results.py`, `analysis.py`, `details.py`): the merged `Profile` becomes JSON.
4. **Raw lines** (`parser/scan.py` through `web/routes_lines.py`): never kept; re-read from the files when asked.

## Rules

1. **The parser does not know the web app.** `logtool/parser/` imports the standard library and itself, nothing
   else. The web app reaches the parser only as `olp.<name>` (`from .. import profiler as olp`). A name it needs
   must be re-exported in `logtool/profiler.py`.
2. **No import cycles.** Inside a package a module imports only from the modules listed above it in the tables.
   Put a new function in the lowest module that has what it needs.
3. **Rebound state is read through its module.** `ONLY_DB` and `ONLY_GEN` (in `web/state.py`) and `UPLOAD_DIR`
   (in `web/storage.py`) are reassigned at run time: read them as `state.ONLY_DB`, `storage.UPLOAD_DIR`. The
   containers and locks (`FILES`, `CHUNKS`, `LOCK` ...) only change in place and are imported by name.
4. **`Profile.feed` is the hot loop.** Every name it uses is a local or a global of `parser/profile.py`, imported
   by name. Do not look names up through a module there (`lines.ACCESS`), and time a change of it before and
   after on a large log.
5. **The scanners replay the parser.** `scan_issue_occurrences` and `scan_op_occurrences` decide like
   `Profile.feed` which lines count (claims, filters, killed crons). Change them together;
   `test_the_scanners_count_like_the_parser` compares them.
6. **A test changes a setting where it is read.** Chunking: `logtool.parser.parallel.PAR_*` and `_cpus`. The
   parse cache: `web.parse_cache.PARSE_CACHE`. A parser function the web app calls: on `olp` (the app looks it up
   there on every call).
7. **Modules stay small.** One concern per module or script, at most 1,000 lines. Past that, split it.

`tests/test_layout.py` checks rules 1, 3 and 7, that every `olp.<name>` the web app uses exists, and that the page and
its HTML export load every script of `logtool/static/`, in one order.

The parse cache key holds a hash of every file of `logtool/parser/`, so a parser change never reads old chunks
back. Parsed chunks are pickled (to worker processes and to the cache): classes they hold must stay importable
from their module.

## Where to change what

| to change | look in |
|---|---|
| a log format Odoo changed (header, access line, cron message) | `parser/lines.py`, `parser/jobs.py`; add the line to `parser/selftest.py` |
| how a request is named or grouped | `parser/classify.py` |
| what is counted while reading | `parser/profile.py` (and `parser/scan.py`, rule 5) |
| a field or operator of the filter domain | `parser/domain.py`, `parser/domain_eval.py`; document it in `FILTERS.md` |
| a number or table the page shows | `web/results.py`, `web/analysis.py`; then `static/stats.js`, `static/tables.js` |
| how a part of the page looks or behaves | the script of that part (the table of `logtool/static/`), `static/style.css`, `templates/index.html` |
| the details popup | `web/details.py` |
| an export | `web/export_*.py`, `web/scrub.py` |
| a new HTTP endpoint | the matching `web/routes_*.py`; its arguments in `web/reqargs.py`; list it in SRS Appendix C |
| a limit (`MAX_*`) | `web/state.py`, or the module that owns it; list it in SRS Appendix B |
| the `logtool` / `logtool-report` options | `web/cli.py`, `parser/cli.py` |

## Tests

```bash
python -m logtool.profiler --selftest   # the parser's built-in checks
python -m pytest                        # every case
python tests/run.py                     # the same cases without pytest
python tests/run.py cron domain         # only the cases whose name contains one of these words
```

A case is a function decorated with `@case` that takes `c`, a Flask test client of a freshly reset app.
`tests/support.py` has the shared log lines and helpers. Add a case to the module of its subject:

| module | subject |
|---|---|
| `test_loading.py` | broken files, header parsing, the parse cache, the database to read |
| `test_api.py` | bad input, foreign origins, caches, pins |
| `test_details.py` | details popup, facts, raw lines, the log viewer |
| `test_cron.py` | cron runs, killed workers |
| `test_filters.py` | fact filters, one client IP |
| `test_domain.py` | the filter domain |
| `test_chunks.py` | chunks and intervals against a straight parse, scanners against the parser |
| `test_exports.py` | the three exports |
| `test_slowq.py` | slow-query logs |
| `test_cli.py` | the command lines |
| `test_layout.py` | the rules above |
