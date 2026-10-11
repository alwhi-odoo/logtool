# logtool filter domains

logtool's whole page state can be written as one **Odoo-style domain**. That covers everything the UI can filter:
which files and databases are read, the time interval, the *item filters* picked in an item's *details behind
the grouping* or *logged during* tables, and conditions on any field of a request, job or warning. It also covers
everything the page **shows**: the histogram's items and their bars and lines, the total's series, focus mode,
fit / scroll, the bucket size and the timezone, plus the table's grouping, search, sort and hidden columns, the
errors table's level and search, and which panels are collapsed (section 7).

Open it with **General Info → Filter editor → Edit filter**. The editor shows the filter that is applied right
now, written as a domain. Edit it, or paste a new one, then press **Apply** (or Ctrl+Enter). The domain is checked
before anything runs, and the check updates as you type. The buttons:

* **Copy** puts the domain on the clipboard.
* **Reset** brings back the filter that is applied now, dropping your edits.
* **Apply** checks the domain, then runs it.

The editor always writes the complete state: every view setting is listed, so the text is never empty. When
you apply a domain, **anything it leaves out takes its default**. `[]` means no filter at all (every file,
database, time and operation) and the default view.

This document is complete: a domain written from it is accepted by the editor.

---

## 1. What a domain filters

A domain is a condition checked against every **occurrence** in the loaded logs. There are three kinds:

| kind      | one occurrence is…                                                           |
|-----------|------------------------------------------------------------------------------|
| `request` | one HTTP access line (werkzeug / `odoo.http.server`)                         |
| `job`     | one run of a cron job, an automation rule, or a module loading               |
| `issue`   | one `WARNING`, `ERROR` or `CRITICAL` log line, with its traceback            |

**Warnings follow the request they were logged during.** When a request is filtered out, the warnings and errors
its worker logged while it ran go with it, the same way its other lines do. A warning that no request claims
(start-up, another process) is kept, unless it is filtered by its client (section 6).

Only the occurrences the domain keeps count anywhere in the app: the statistics, the histogram, the operations,
apps and types tables, the errors table, the raw log lines, and **Export summary**.

## 2. Syntax

A domain is a list, written as JSON or as a Python literal (Odoo's own notation). Both are accepted:

```json
[["http_status", ">=", 500], ["duration", ">", 2]]
```
```python
[('http_status', '>=', 500), ('duration', '>', 2)]
```

* **Term**: `[field, operator, value]`.
* **Logical operators** are written in *prefix* (Polish) notation, exactly like Odoo:
  * `"&"` means AND of the next two items,
  * `"|"` means OR of the next two items,
  * `"!"` means NOT of the next item.
* **Top-level items with no operator between them are joined with AND.** So `[A, B, C]` is the same as
  `["&", "&", A, B, C]`.
* An "item" is a term, or an operator together with its own items: `["|", A, "&", B, C]` means A OR (B AND C).
* JSON uses `true` / `false` / `null`; Python uses `True` / `False` / `None`. Tuples count as lists.

Three operators combining four terms:

```json
["&", "&", "&", ["kind", "=", "request"], ["app", "=", "sale"], ["http_status", "=", 200], ["duration", ">", 1]]
```

The same domain, relying on the implicit AND:

```json
[["kind", "=", "request"], ["app", "=", "sale"], ["http_status", "=", 200], ["duration", ">", 1]]
```

## 3. Fields

### 3.1 Selection fields: `file`, `database`, `time`

These choose which log lines are read at all. **They may only appear as top-level terms joined with AND**,
never inside `"|"` or `"!"`; the editor rejects that.

| field      | value                                                              | operators                                   |
|------------|--------------------------------------------------------------------|---------------------------------------------|
| `file`     | the name of a loaded log file, as shown in the Files box           | `=`, `!=`, `in`, `not in`                   |
| `database` | a database name as shown in the Databases box; `false` = the lines with no database | `=`, `!=`, `in`, `not in`      |
| `time`     | `"YYYY-MM-DD HH:MM:SS"` (seconds optional), in the log's own clock (UTC as logged) | `>=`, `>`, `<`, `<=`         |

* `in` / `=` keep only the listed files or databases. `not in` / `!=` leave out the listed ones.
* Naming a file or database that is not loaded is an error with `in` / `=`. With `not in` / `!=` it is ignored.
* A domain may not leave out every file, or every database of the selected files.
* The time interval is from the latest lower bound (`>=` / `>`) to the earliest upper bound (`<` / `<=`).
  Giving only one bound is fine. `["time", "<", "2026-09-02 00:00:00"]` ends just before midnight.

### 3.2 Occurrence fields

A term on a field that the occurrence's kind does not have is **not applicable**; see section 5.

| field         | kinds          | type   | meaning                                                                               |
|---------------|----------------|--------|---------------------------------------------------------------------------------------|
| `kind`        | all            | text   | `"request"`, `"job"` or `"issue"`                                                     |
| `operation`   | request, job   | text   | the operation, exactly as in the operations table (`call_kw sale.order.web_read`, `/shop/<slug>`, `cron: Mail: Email Queue Manager`) |
| `app`         | request, job   | text   | the app column (`sale`, `stock`, `web`, `base`, …)                                    |
| `type`        | request, job   | text   | the type column (`ui.read`, `ui.write`, `ui.action`, `api.method`, `assets`, `report`, `cron`, `automation`, `module-load`, …) |
| `duration`    | request, job   | number | seconds: SQL + Python for a request, the logged duration for a job                    |
| `sql_s`       | request        | number | seconds inside PostgreSQL                                                             |
| `python_s`    | request        | number | seconds outside PostgreSQL                                                            |
| `queries`     | request, job   | number | number of SQL queries                                                                 |
| `http_status` | request        | number | HTTP status code: `200`, `404`, `500`, …                                              |
| `method`      | request        | text   | `GET`, `POST`, …                                                                      |
| `client IP`   | request        | text   | the client address (a warning's client: see section 6, item filters)                  |
| `session`     | request        | text   | the short session id of Odoo 19.4+ access lines; `false` when the line has none       |
| `URL`         | request        | text   | the raw path of the request, with its query string (`/shop/chair-12?page=2`)          |
| `logged user id` | request, job | text  | user ids named in the lines **logged while it ran** (several possible). See section 3.3 |
| `logged records` | request, job | text  | records named in the lines logged while it ran, written `model(id)` (several possible). See section 3.3 |
| `cron id`     | job            | text   | the cron job's id (`"8"`)                                                             |
| `status`      | job            | text   | how a cron run ended (`fully done`, `processed`, …)                                   |
| `remaining`   | job            | text   | how many records a cron run left for later (`"0"`)                                    |
| `rule id`     | job            | text   | an automation rule's id (`"12"`)                                                      |
| `records processed` | job      | text   | the records an automation rule run processed, written `done/total` (`"3/3"`)          |
| `modules`     | job            | text   | how many modules a module loading loaded (`"762"`)                                    |
| `level`       | issue          | text   | `WARNING`, `ERROR` or `CRITICAL`                                                      |
| `logger`      | issue          | text   | the logger (`odoo.models`, `odoo.http`, `odoo.addons.sale.models.sale_order`)         |
| `message`     | issue          | text   | the message *template*: numbers replaced by `#`, exactly as in the errors table (`Order # could not be confirmed by user #`) |
| `issue`       | issue          | list   | the whole identity of an errors-table row: `[level, logger, message template]`        |
| `text`        | issue          | text   | the message exactly as logged (its first line, numbers included)                      |
| `user id`     | issue          | text   | user ids named in the message or its traceback (several possible)                     |
| `records`     | issue          | text   | records named in the message or its traceback, written `model(id)` or `model(id1,id2)` (several possible) |
| `<word> #`    | issue          | text   | the number behind each `#` of the template, named after the word before it: `Order #`, `user #`, `ID #`. A second `#` after the same word is `ID # (2)`. A number with no word before it (a message that starts with one) is plain `#` |

Detail fields such as `client IP`, `URL` and `user id` use exactly the names shown as table titles in a row's
*Details behind the grouping* tab. Look there to find the names and values that exist in your log.

Numbers written as text are compared as numbers when both sides are numbers. So `["cron id", "=", 8]` and
`["cron id", "=", "8"]` both work. The number fields (`duration`, `sql_s`, `python_s`, `queries`,
`http_status`) only compare (`<`, `<=`, `>`, `>=`) with numbers: `["duration", ">", "2"]` works,
`["duration", ">", "slow"]` is an error.

### 3.3 What a request's own lines name: `logged user id`, `logged records`

A request's access line is written when the request ends. Everything its worker logged meanwhile was logged
*during* that request, and is matched to it by worker and time (the app's *Logged during* tab). That includes
ORM calls such as `odoo.api: call sale.order(42,).action_confirm()`, add-on messages, and warnings and errors
with their tracebacks. Those lines often name what the request worked on, which its own access line never
does. The request inherits those names:

* `logged records`: every `model(id)` named, e.g. `sale.order(42)`. A call on several records gives
  `sale.order(42,43)`, written exactly as the *Logged during* tab shows it.
* `logged user id`: every user id named (`(id=7)`, `uid 7`, `user 7`, `user_id: 7`).

A cron run inherits them too. Automation rules and module loading run inside a request, so their lines go to
that request.

Each value counts **once per request**, so the tables show how many requests named it. Filtering by one keeps
exactly those requests. This is how to trace who touched a record:

```json
["|", ["operation", "!=", "call_button sale.order.action_confirm"], ["logged records", "in", ["sale.order(42)"]]]
```

Then open the operation: its *client IP*, *session* and *URL* details now describe only the requests that
confirmed order 42. To trace the record across every operation instead of just one, use
`[["kind", "=", "request"], ["logged records", "=", "sale.order(42)"]]`.

ORM call lines are only in the log at `--log-level=debug`; warnings and errors name records at any log level.
Issues have no lines of their own, so these two fields do not apply to them.

## 4. Operators

| operator                  | meaning                                                                     | value            |
|---------------------------|-----------------------------------------------------------------------------|------------------|
| `=`, `!=`                 | equal / not equal. `= false` means "has no value", `!= false` means "has a value" | one value  |
| `in`, `not in`            | equal to one of / to none of the values                                     | a list (a single value counts as a list of one) |
| `<`, `<=`, `>`, `>=`      | numeric comparison (text compares alphabetically)                           | number or text   |
| `like`, `not like`        | contains, case-sensitive. `%` matches any run of characters, `_` one character | text          |
| `ilike`, `not ilike`      | contains, case-insensitive                                                  | text             |
| `=like`, `=ilike`         | the whole value matches the pattern (`%` and `_` as wildcards)              | text             |

`issue` only takes `=`, `!=`, `in` and `not in`. Its value is `["LEVEL", "logger", "template"]`, or a list of
such triples. `time` only takes `<`, `<=`, `>` and `>=`. `file` and `database` only take `=`, `!=`, `in` and
`not in`.

**Fields with several values** (`records`, `user id`, `<word> #` on a warning; `logged records`, `logged user id`): positive operators (`=`, `in`,
`like`, comparisons) match when **any** value matches. Negative operators (`!=`, `not in`, `not like`,
`not ilike`) match when **no** value matches.

## 5. Not applicable: how kinds mix

Every occurrence is tested against the same domain, but kinds have different fields. A request has no `level`;
a warning has no `http_status` and no `client IP`. **A term on a field the occurrence does not have is left
out.** It is not true and not false; it is simply dropped from the `&`, `|` or `!` around it. If nothing
applicable is left, the occurrence is kept.

Consequences:

* `[["http_status", ">=", 500]]` keeps only the failing requests. Jobs and warnings are untouched, because they
  have no `http_status`.
* `[["level", "=", "ERROR"]]` keeps only the `ERROR` lines among the issues; `WARNING` and `CRITICAL` lines are
  dropped. Requests and jobs are untouched.
* To drop a whole kind, test `kind`, which every occurrence has: `[["kind", "!=", "issue"]]` removes every
  warning and error.
* To keep *only* some requests and nothing else, combine them with `kind`:
  `[["kind", "=", "request"], ["app", "=", "sale"]]`.
* On a warning, `user id`, `records` and every `<word> #` field are applicable even when empty.
  `[["Order #", "=", "17"]]` drops the warnings that name no order 17, including those with no `Order #` at all.
* On a job, `cron id`, `status`, `remaining`, `rule id`, `records processed` and `modules` are applicable even
  when empty.

## 6. The app's own filters, as domains

The editor shows the current filter in these shapes, and it recognises them when you apply a domain. They then
run on the app's fast, cached paths. Anything else is a *custom condition*, checked on every occurrence. That
works just as well, but the logs are re-read when it changes (about a second per 15 MB of log).

| UI action                                         | domain shape |
|---------------------------------------------------|--------------|
| click a file off in the Files box                 | `["file", "not in", ["other.log"]]` |
| click a database off                              | `["database", "not in", ["other_db", false]]` |
| select a time interval (histogram / top bar)      | `["time", ">=", "2026-09-01 08:00:00"], ["time", "<", "2026-09-01 09:00:00"]` |
| item filter on an operation (click values in its details) | `"\|", ["operation", "!=", "call_kw sale.order.web_read"], ["client IP", "in", ["10.0.0.7", "10.0.0.1"]]` |
| item filter on an app / type                      | `"\|", ["app", "!=", "sale"], ["session", "in", ["S0012"]]` (a type uses `"type"`) |
| item filter with two details (AND)                | `"\|", ["operation", "!=", "X"], "&", ["client IP", "in", ["10.0.0.7"]], ["URL", "in", ["/shop/chair-12"]]` |
| item filter on what its lines named (Logged during tab) | `"\|", ["operation", "!=", "call_button sale.order.action_confirm"], ["logged records", "in", ["sale.order(42)"]]` |
| item filter on a warning / error                  | `"\|", ["issue", "!=", ["WARNING", "odoo.addons.sale.models.sale_order", "Order # could not be confirmed by user #"]], ["user #", "in", ["6"]]` |
| a warning / error traced to its client            | `"\|", ["issue", "!=", ["WARNING", "odoo.http", "From and to dates must be set, and from must be before to."]], ["client IP", "in", ["78.20.216.95"]]` |

In these tables `\|` is only Markdown's escape for a pipe inside a table: write `"|"`.

Why item filters look like this: *"occurrences of X must carry one of these values; everything else is
untouched"* is the same as *"not X, or carries one of these values"*.

**Tracing a warning to its client.** A warning's own line has no client, but the request it was logged during
does. Its *client IP* and *session* item filters are therefore checked against that request, decided when the
request ends: the occurrences logged during requests from those clients are kept. An occurrence that no request
claims has no client, so a client filter leaves it out. The *client IP* table of a warning lists the clients of
those requests, so clicking one traces the warning back to it.

A warning cannot be filtered by `logged …`, because it has no lines of its own. As custom conditions (outside the
item-filter shape), `client IP`, `session` and `logged …` do not apply to warnings.

Excluding operations has no button of its own. To leave operations out, write it as a custom condition:
`["operation", "not in", ["call_kw res.partner.read", "/web/webclient/load_menus"]]`.

## 7. View settings: what the page shows

These terms are settings, not conditions. Each is one **top-level** term of the form `[setting, "=", value]`;
`chart.item` appears once per item, and `chart.focus` takes two bounds like `time`. Settings may not appear
inside `"|"` or `"!"`. Any setting that is left out takes the default shown.

| setting                 | value                                                                                | default |
|-------------------------|--------------------------------------------------------------------------------------|---------|
| `chart.item`            | one histogram item: an object with **one** of `"operation"`, `"app"`, `"type"`, `"search"` (free text, matched against type, app and operation) or `"issue"` (`[level, logger, message template]`), plus `"bars"` and `"lines"`: lists of series | no items |
| `chart.total`           | `{"bars": [series], "lines": [series]}` for the always-present **total** item          | `{"bars": ["requests"], "lines": ["avg_ms"]}` when there are no items; `{"bars": [], "lines": []}` when there are |
| `chart.bucket`          | bucket size in minutes: `1`, `2`, `5`, `10`, `15`, `30`, `60`, `120`, `180`, `360`, `720`, `1440`, `10080`, or `"auto"` | `"auto"` |
| `chart.fit`             | `true`: squeeze every bucket into the panel; `false`: readable bars, horizontal scroll  | `true` |
| `chart.focus`           | `["chart.focus", ">=", "…"], ["chart.focus", "<", "…"]`: focus mode on that span (both bounds required) | not focused |
| `chart.timezone`        | an IANA timezone for the histogram's labels: `"UTC"`, `"Europe/Brussels"`, …           | `"UTC"` |
| `table.group`           | `"operation"`, `"app"` or `"type"`                                                    | `"operation"` |
| `table.search`          | the text in the table's search box                                                     | `""` |
| `table.sort`            | `[column, "asc" \| "desc"]`                                                           | `["total_s", "desc"]` |
| `table.hidden_columns`  | list of hidden columns                                                                 | `["pct_sql_within_op", "pct_python_within_op", "http_5xx", "bottleneck"]` |
| `errors.level`          | `"all"`, `"errors"` or `"warnings"`                                                   | `"all"` |
| `errors.search`         | the text in the errors table's search box                                              | `""` |
| `panels.hidden`         | list of collapsed panels among `"chart"`, `"table"`, `"selected"`, `"errors"`          | `[]` |

**Series names**, for `bars` and `lines`: `requests`, `avg_ms`, `avg_ms.py`, `avg_ms.sql`, `total_ms`,
`total_ms.py`, `total_ms.sql`. `avg_ms` is the average ms per request (Python + SQL + jobs); `.py` and `.sql` are
its parts. `total_ms` is the summed time of the bucket. An `issue` item only has `requests` (its occurrences).
An item written without `bars` / `lines` gets bars of `requests` and a line of `avg_ms` (an issue: bars only).

**Columns**, for `table.sort` and `table.hidden_columns`: `type`, `app`, `total_s`, `pct_of_total_time`,
`sql_s`, `pct_sql_within_op`, `python_s`, `pct_python_within_op`, `calls`, `avg_ms`, `p95_s`, `max_s`,
`queries_per_call`, `ms_sql_per_query`, `http_5xx`, `bottleneck`. `table.sort` also accepts `operation`, the
first column.

Items name rows exactly as the tables do; an item whose row is not in the selection simply shows empty. The
bucket size falls back to `"auto"` when the data cannot be shown that finely. The whole log's finest size
depends on its length, while focus mode gets down to 1 minute for a focus of up to a day.

## 8. Examples

Only the `mydb` database, only 1 September:

```json
[["database", "=", "mydb"], ["time", ">=", "2026-09-01 00:00:00"], ["time", "<", "2026-09-02 00:00:00"]]
```

Slow requests (and every job), no warnings:

```json
[["kind", "!=", "issue"], "|", ["kind", "=", "job"], ["duration", ">", 2]]
```

The traffic of one client, plus every error:

```json
["|", ["client IP", "=", "10.0.0.7"], ["kind", "=", "issue"]]
```

Requests from 10.0.0.7 are kept. Other requests fail the IP test, and their `kind` is not `issue`, so they are
dropped. Warnings have no client IP, so only `kind` applies and they are kept. Jobs have no client IP either,
and their `kind` is not `issue`, so they are dropped.

Server errors of the website, and the ERROR lines logged by `odoo.http`:

```json
["|", "&", ["app", "=", "website"], ["http_status", ">=", 500], "&", ["level", "=", "ERROR"], ["logger", "=", "odoo.http"]]
```

Everything except the asset bundles and the scanners' probes:

```json
[["type", "not in", ["assets", "probe"]]]
```

Heavy N+1 suspects: many queries per request, within one app:

```json
[["kind", "=", "request"], ["app", "=", "stock"], ["queries", ">=", 200]]
```

Warnings about one record, however the message is worded:

```json
[["kind", "=", "issue"], ["records", "=", "sale.order(18)"]]
```

Every request that worked on order 42, all operations together (debug logs, or warnings naming it):

```json
[["kind", "=", "request"], ["logged records", "=", "sale.order(42)"]]
```

What user 7 did, as far as the lines logged during each request name them:

```json
["|", ["kind", "!=", "request"], ["logged user id", "=", "7"]]
```

A cron job's failed runs only:

```json
[["operation", "=", "cron: Mail: Email Queue Manager"], ["status", "!=", "fully done"]]
```

This drops the other operations too (their `operation` differs), and leaves warnings alone. To keep the other
operations as they are, use the detail-filter shape:
`["|", ["operation", "!=", "cron: Mail: Email Queue Manager"], ["status", "!=", "fully done"]]`.

URLs with a query string, case-insensitively containing "token":

```json
[["URL", "ilike", "%?%token%"]]
```

### A complete domain (every kind of term)

A stress test of the whole editor. It has a file and a database, the time interval, item filters (one of them
tracing a warning to its client), custom conditions (one leaving operations out), five chart items (one of each kind), the total's series, bucket size, scroll
mode, focus, timezone, and every table / errors / panels setting. Replace the names with ones from your own
log; a `not in` naming something that is not loaded is ignored.

```json
[
    ["file", "not in", ["odoo.log.2026-09-30.gz"]],
    ["database", "not in", [false]],
    ["time", ">=", "2026-09-01 06:00:00"],
    ["time", "<", "2026-09-01 18:00:00"],
    ["operation", "not in", ["content (attachment)"]],
    "|", ["operation", "!=", "call_kw ir.attachment.register_as_main_attachment"], ["client IP", "in", ["78.20.216.95"]],
    "|", ["issue", "!=", ["WARNING", "odoo.http", "From and to dates must be set, and from must be before to."]], ["client IP", "in", ["78.20.216.95"]],
    ["type", "!=", "probe"],
    "|", ["kind", "!=", "request"], ["http_status", "<", 500],
    ["chart.item", "=", {"operation": "call_kw ir.attachment.register_as_main_attachment", "bars": ["requests"], "lines": ["avg_ms", "avg_ms.sql"]}],
    ["chart.item", "=", {"app": "mail", "bars": ["total_ms.py", "total_ms.sql"], "lines": []}],
    ["chart.item", "=", {"type": "ui.method", "bars": [], "lines": ["requests"]}],
    ["chart.item", "=", {"search": "call_kw", "bars": [], "lines": ["total_ms"]}],
    ["chart.item", "=", {"issue": ["WARNING", "odoo.http", "From and to dates must be set, and from must be before to."], "bars": ["requests"], "lines": []}],
    ["chart.total", "=", {"bars": [], "lines": ["requests"]}],
    ["chart.bucket", "=", 5],
    ["chart.fit", "=", false],
    ["chart.focus", ">=", "2026-09-01 00:00:00"],
    ["chart.focus", "<", "2026-09-02 00:00:00"],
    ["chart.timezone", "=", "Europe/Brussels"],
    ["table.group", "=", "app"],
    ["table.search", "=", "a"],
    ["table.sort", "=", ["calls", "desc"]],
    ["table.hidden_columns", "=", ["p95_s", "max_s", "bottleneck"]],
    ["errors.level", "=", "warnings"],
    ["errors.search", "=", "dates"],
    ["panels.hidden", "=", ["selected"]]
]
```

## 9. Errors the editor reports

| message starts with                         | fix |
|---------------------------------------------|-----|
| `not a valid domain`                        | the text is neither JSON nor a Python literal: check brackets, quotes and commas |
| `a domain is a list`                        | wrap it in `[ … ]` |
| `a term is [field, operator, value]`        | a term must have exactly three parts |
| `unknown field`                             | use a field of section 3; detail names are case-sensitive (`client IP`, `URL`) |
| `unknown operator`                          | use an operator of section 4 |
| `… takes a list of values`                  | the list given to `in` / `not in` holds something other than plain values |
| `… is a number: … needs a number`           | compare `duration`, `sql_s`, `python_s`, `queries` and `http_status` with numbers |
| `the domain ends too early`                 | an `"&"` or `"\|"` needs two items after it, `"!"` needs one |
| `file, database and time can only be top-level conditions` | move them out of `"\|"` / `"!"` |
| `no file is named` / `no database is named` | the message lists the loaded names |
| `the domain leaves out every …`             | keep at least one file / database |
| `the time conditions leave no time at all`  | the lower bound is after the upper bound |
| `… is a setting: write [...]`               | view settings use `=` (only `chart.focus` takes `>=` / `<`) |
| `view settings … are top-level terms`       | move `chart.*` / `table.*` / `errors.*` / `panels.*` out of `"\|"` / `"!"` |
| `chart.item needs exactly one of …`         | give one of `operation`, `app`, `type`, `search`, `issue` |
| `unknown series`                            | use a series name of section 7 (an issue only has `requests`) |
| `chart.focus needs a lower (>=) and a higher upper (<) bound` | give both bounds, lower first |

## 10. For agents: writing a domain

1. Start from the current filter. Open the editor, or ask the user to paste its text, so file and database
   names are exact.
2. Put `file`, `database` and `time` terms at the top level only.
3. Copy operation, app, type and message names character for character from the tables. `message` is the
   *template*, with `#` in place of numbers.
4. Decide whether a condition should affect every kind of occurrence. If so, add a `kind` term; otherwise rely
   on section 5 and let it apply only where the field exists.
5. To narrow one row while leaving the rest alone, use `"|", [row field, "!=", name], condition`.
6. Keep the view settings you are not changing. Applying replaces the whole page state, and missing settings go
   back to their defaults, so start from the editor's text and edit only what you mean to change.
7. Output a single JSON list. Apply it, and if the editor reports an error, fix the part it names (section 9).
