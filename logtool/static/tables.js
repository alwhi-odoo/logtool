"use strict";

/* The tables: operations / apps / types (columns menu, totals, sorted and chunked rows), the selected
 * items, the errors and warnings.
 * One of the page's scripts: templates/index.html lists them in load order (see ARCHITECTURE.md). */

/* ---- table ----------------------------------------------------------- */
// Plain-language help for generic operations, shown when hovering the operation name
const OP_HINTS = [
    [/^run server action/, "The web client asked Odoo to run a <b>server action</b> (<code>ir.actions.server</code>): a button, menu item or automation-style action defined in Settings. The log does not say <i>which</i> server action, and the time includes everything its code does (Python, creating or updating records, sending mail…). Many queries per call usually means the action loops over records."],
    [/^load action/, "Loads the definition of a menu / window action when a user opens it."],
    [/^webclient load_menus/, "Loads the menu tree when the web client starts. Runs once per page load."],
    [/^webclient translations/, "Loads the translation terms when the web client starts."],
    [/^client shell/, "The HTML page of the web client itself, served on every full page load."],
    [/^cron: /, "One run of a scheduled action (cron job). The time is the duration Odoo logged for the run; there is no SQL / Python split."],
    [/^automation: /, "One run of an automation rule. The time is the duration Odoo logged; there is no SQL / Python split."],
    [/^load modules/, "Odoo loaded its modules into the registry (server or worker start-up, module install or upgrade)."],
    [/^call_kw .*\.(web_search_read|web_read|search_read|read)$/, "A list or form view reading records."],
    [/^call_kw .*\.(web_save|write|create)$/, "Saving a record from a form or list view."],
    [/^call_button /, "A button click on a form or list: a workflow step such as confirm, validate or post."],
    [/^bundle /, "A compiled JS / CSS asset bundle. Slow times usually mean the bundle was regenerated."],
];
const opHint = name => { const h = OP_HINTS.find(([rx]) => rx.test(name)); return h ? h[1] : ""; };

const NOSPLIT = new Set(["sql_s", "pct_sql_within_op", "python_s", "pct_python_within_op", "ms_sql_per_query"]);

const visCols = () => COLS.filter(c => c.cls === "op" || !state.hiddenCols.has(c.key));

function renderColsMenu() {
    $("#colsMenu").innerHTML = COLS.filter(c => c.cls !== "op").map(c =>
        `<label><input type="checkbox" data-col="${c.key}"${state.hiddenCols.has(c.key) ? "" : " checked"}> ${esc(c.label)}</label>`).join("");
}

// one totals row from a totals object {calls, total_s, sql_s, python_s, queries, errors, max_s}
function absRow(label, t, cls) {
    const div = (a, b) => b ? a / b : 0;
    return "<tr" + (cls ? ` class="${cls}"` : "") + ">" + visCols().map(c => {
        switch (c.key) {
            case "operation": return `<td class="op lbl">${label}</td>`;
            case "total_s": return `<td>${fSec(t.total_s)}</td>`;
            case "pct_of_total_time": return `<td>100.0%</td>`;
            case "sql_s": return `<td>${fSec(t.sql_s)}</td>`;
            case "python_s": return `<td>${fSec(t.python_s)}</td>`;
            case "pct_sql_within_op": return `<td>${nf0.format(100 * div(t.sql_s, t.total_s))}%</td>`;
            case "pct_python_within_op": return `<td>${nf0.format(100 * div(t.python_s, t.total_s))}%</td>`;
            case "calls": return `<td style="color:${TONE.calls}">${nf0.format(t.calls)}</td>`;
            case "avg_ms": return `<td style="color:${TONE.avg_ms}">${nf0.format(1000 * div(t.total_s, t.calls))}</td>`;
            case "max_s": return `<td>${fSec(t.max_s)}</td>`;
            case "queries_per_call": return `<td>${nf1.format(div(t.queries, t.calls))}</td>`;
            case "ms_sql_per_query": return `<td>${nf1.format(1000 * div(t.sql_s, t.queries))}</td>`;
            case "http_5xx": return `<td>${nf0.format(t.errors)}</td>`;
            default: return `<td class="na">–</td>`;
        }
    }).join("") + "</tr>";
}

// the sum of just the rows matching an active histogram search tag (the same OR-match the bars use);
// opRows() already reflects the current time interval, so this also combines with one if set
function selectionTotal() {
    if (!state.histTags.length) return null;
    const rows = opRows().filter(r => state.histTags.some(t => tagMatcher(t)(r)));
    if (!rows.length) return null;
    const t = {calls: 0, total_s: 0, sql_s: 0, python_s: 0, queries: 0, errors: 0, max_s: 0};
    for (const r of rows) {
        t.calls += r.calls; t.total_s += r.total_s; t.sql_s += r.sql_s; t.python_s += r.python_s;
        t.queries += r.queries; t.errors += r.http_5xx; t.max_s = Math.max(t.max_s, r.max_s);
    }
    return t;
}

function tagsLabel() {
    const labels = state.histTags.map(t => t.label);
    return labels.length <= 2 ? labels.join(", ") : `${labels.slice(0, 2).join(", ")} +${labels.length - 2} more`;
}

function totalsRows() {
    const d = state.data, cut = d.view.totals, full = d.full_totals, iv = state.interval;
    const div = (a, b) => b ? a / b : 0;
    let out;
    if (!iv) {
        out = absRow("Total", full);
    } else {
        const ratio = (a, b, col) => `<td${col ? ` style="color:${col}"` : ""}>${fPct(100 * div(a, b))}</td>`;
        const share = "<tr class=\"share\">" + visCols().map(c => {
            switch (c.key) {
                case "operation": return `<td class="op lbl">Interval share of whole</td>`;
                case "total_s": return ratio(cut.total_s, full.total_s);
                case "pct_of_total_time": return ratio(cut.total_s, full.total_s, heat(100 * div(cut.total_s, full.total_s), SCALES.pct_of_total_time));
                case "sql_s": return ratio(cut.sql_s, full.sql_s);
                case "python_s": return ratio(cut.python_s, full.python_s);
                case "calls": return ratio(cut.calls, full.calls, TONE.calls);
                case "http_5xx": return ratio(cut.errors, full.errors);
                default: return `<td class="na">–</td>`;
            }
        }).join("") + "</tr>";
        out = absRow("Total · this interval", cut) + absRow("Total", full) + share;
    }
    const sel = selectionTotal();
    return sel ? out + absRow("Total · " + esc(tagsLabel()), sel, "sel") : out;
}

// The tables can hold thousands of rows. Three things keep typing in the search box instant:
//   * a column sort is computed once per (data, table, group, column, direction) and reused,
//   * each row's search text is built once,
//   * only the first TABLE_CHUNK rows are in the DOM; more are appended as you scroll down.
const TABLE_CHUNK = 200;
const TABLES = {
    main: {id: "main", tbl: "#tbl", wrap: "#tableBody .table-wrap", count: "#rowCount", rows: [], shown: 0, by: "operation", total: 0},
};

// Operation rows travel once per file / database / interval selection (the browser never re-downloads them).
const opRows = () => state.rowsAll || [];

function tableSource(T, v, by) {
    return by === "operation" ? opRows() : v.groups[by];
}

function sortedRows(T, v, by, key, dir) {
    const op = by === "operation", owner = op ? state.rowsAll : v;
    if (!owner) return [];
    const cache = owner._sorted || (owner._sorted = {});                   // sorted once per data / column / direction
    const ck = (op ? "" : T.id + "|") + by + "|" + key + "|" + dir;
    const rows = cache[ck] || (cache[ck] = tableSource(T, v, by).slice().sort((a, b) => {
        const x = a[key], y = b[key];
        return (typeof x === "string" ? x.localeCompare(y) : x - y) * dir;
    }));
    return rows;
}

// denom: the total the "% tot" column is a share of (operation rows carry no share of their own)
function rowHtml(r, by, cols, denom) {
    const name0 = groupName(r, by), isOp = by === "operation";
    const cls = isPicked(by, name0) ? " picked" : "";
    return `<tr data-name="${esc(name0)}"${cls ? ` class="${cls.trim()}"` : ""}>` + cols.map(c => {
        let val = r[c.key];
        if (c.cls === "op") {
            const hint = isOp ? opHint(name0) : "";
            const dbl = isOp ? `<span class="scale">Click for details · right-click to select it (it becomes a histogram item and joins the selected items table).</span>` : "";
            const mark = ffBadge(by, name0);
            return isOp ? `<td class="op" data-tip="${esc("<b>" + esc(name0) + "</b>" + (hint ? "<br>" + hint : "") + dbl)}">${esc(name0)}${mark}</td>`
                        : `<td class="op" title="${esc(name0)}">${esc(name0)}${mark}</td>`;
        }
        if (c.cls === "l") return `<td class="l dim" title="${esc(val || "")}">${c.key === by ? "" : esc(val || "")}</td>`;   // the grouped column is the first one
        if (c.badge) return `<td><span class="badge b-${esc(val)}">${esc(val)}</span></td>`;
        if (c.key === "pct_of_total_time" && isOp) val = denom ? 100 * r.total_s / denom : 0;
        if (NOSPLIT.has(c.key) && r.unsplit_s > 0 && !(r.sql_s + r.python_s)) return `<td class="na">–</td>`;   // job: no SQL/Python split
        if (c.key === "queries_per_call" && r.unsplit_s > 0 && !r.queries) return `<td class="na">–</td>`;
        const col = c.tone ? TONE[c.key] : c.color ? heat(val, SCALES[c.key]) : null;
        return `<td${col ? ` style="color:${col}"` : ""}>${c.f(val)}</td>`;
    }).join("") + "</tr>";
}

function setTableCount(T) {
    $(T.count).textContent = `${nf0.format(T.rows.length)} of ${nf0.format(T.total)} ${GROUP_UNIT[T.by]}` +
        (T.id === "main" && T.by === "operation" && T.total >= 5000 ? " (top 5,000 by time)" : "") +
        (T.by === "slowq" ? ` · ${plural(sqData().n, "run")}` + (T.total > sqData().rows.length ? ` (the ${nf0.format(sqData().rows.length)} with the most time)` : "") : "") +
        (T.shown < T.rows.length ? ` · showing ${nf0.format(T.shown)}, scroll for more` : "");
}

// bodyOnly: only the rows changed (search box); otherwise header, rows and totals are rebuilt
function renderOpTable(T, bodyOnly) {
    if (state.tableGroup === "slowq") return renderSqTable(T, bodyOnly);
    const v = view(), by = state.tableGroup, {key, dir} = state.sort;
    const q = T.id === "main" ? state.filter.trim().toLowerCase() : "";
    const base = sortedRows(T, v, by, key, dir);
    let rows = base;
    if (q) rows = base.filter(r => (r._s || (r._s = (r.family + " " + r.app + " " + r.operation).toLowerCase())).includes(q));
    const denom = by === "operation" ? v.totals.total_s : undefined;
    Object.assign(T, {rows, shown: Math.min(TABLE_CHUNK, rows.length), by, denom, total: base.length});
    setTableCount(T);

    const cols = visCols();
    const body = rows.slice(0, T.shown).map(r => rowHtml(r, by, cols, denom)).join("") ||
        `<tr><td class="empty" colspan="${cols.length}">No matching ${GROUP_UNIT[by]}</td></tr>`;
    const wrap = $(T.wrap), tb = $(T.tbl + " tbody");
    if (bodyOnly && tb) {
        tb.innerHTML = body;
        wrap.scrollTop = 0;
        return;
    }
    const head = cols.map(c =>
        `<th class="${c.cls || ""}${c.key === key ? " sorted" + (dir > 0 ? " asc" : "") : ""}" data-sort="${c.key}" data-tip="${esc(colTip(c))}">${c.cls === "op" ? GROUP_LABEL[by] : c.label}</th>`).join("");
    const foot = totalsRows();
    $(T.tbl).innerHTML = `<thead><tr>${head}</tr></thead><tbody>${body}</tbody><tfoot>${foot}</tfoot>`;
}

function renderTable(bodyOnly) {
    const sq = state.tableGroup === "slowq";
    $("#tableTitle").textContent = sq ? "Slow queries" : "Top operations";
    $("#filter").placeholder = sq ? "Filter (SQL or database)…" : "Filter (type, app or operation)…";
    $("#colsBtn").hidden = sq;
    if (!state.hidden.table) renderOpTable(TABLES.main, bodyOnly);
    renderSelTable();
}

// ---- selected items: every histogram item, one row each, under the main table (hidden when there are none) ----
function selItemRow(it) {
    const v = view();
    const sum = rows => {
        const t = {calls: 0, total_s: 0, sql_s: 0, python_s: 0, queries: 0, http_5xx: 0, max_s: 0};
        for (const r of rows) { for (const k of ["calls", "total_s", "sql_s", "python_s", "queries", "http_5xx"]) t[k] += r[k] || 0; t.max_s = Math.max(t.max_s, r.max_s); }
        return Object.assign(t, {avg_ms: t.calls ? 1000 * t.total_s / t.calls : 0, queries_per_call: t.calls ? t.queries / t.calls : 0, p95_s: null});
    };
    if (it.kind === "issue") return v.issues.find(r => sameIssue(r, it.tag)) || null;
    if (it.kind === "slowq") {
        const r = sqData().rows.find(x => x.shape === it.tag.shape);
        return r ? {calls: r.n, total_s: r.total_s, avg_ms: 1000 * r.avg_s, max_s: r.max_s, sq: true} : null;
    }
    if (!it.tag.by) { const rows = opRows().filter(tagMatcher(it.tag)); return rows.length ? sum(rows) : null; }
    return detailRow(it.tag.by, it.tag.term) || null;
}

function renderSelTable() {
    const panel = $("#selPanel"), items = histItems().slice(1);
    panel.hidden = !state.data || !items.length;
    if (panel.hidden || state.hidden.sel) return;
    const total = view().totals.total_s, dash = `<td class="na">–</td>`;
    $("#selCount").textContent = plural(items.length, "item");
    const rows = items.map(it => {
        const r = selItemRow(it), issue = it.kind === "issue";
        const kind = issue ? (it.tag.level === "WARNING" ? "warning" : "error") : it.kind === "slowq" ? "slow query" : it.tag.by ? GROUP_LABEL[it.tag.by] : "search";
        const label = issue ? it.tag.message : it.tag.label;
        const name = `<td class="op l"><span class="sw-item" style="background:${seriesColor(it.hue, "req")}"></span>` +
            `<span class="badge b-mixed selkind">${esc(kind)}</span> ${esc(label)}${issue ? ffBadge("issue", issueKeyOf(it.tag)) : it.tag.by ? ffBadge(it.tag.by, it.tag.term) : ""}</td>`;
        const attrs = `data-sel-item="${it.id}"`;
        if (!r) return `<tr ${attrs}>${name}<td class="empty-cell" colspan="9">not in the current selection</td></tr>`;
        if (issue) return `<tr ${attrs}>${name}<td style="color:${TONE.calls}">${nf0.format(r.n)}</td>${dash.repeat(8)}</tr>`;
        if (r.sq) return `<tr ${attrs}>${name}<td style="color:${TONE.calls}">${nf0.format(r.calls)}</td><td>${fSec(r.total_s)}</td>${dash}` +
            `<td style="color:${TONE.avg_ms}">${nf0.format(r.avg_ms)}</td>${dash.repeat(3)}<td>${fSec(r.max_s)}</td>${dash}</tr>`;
        return `<tr ${attrs}>${name}<td style="color:${TONE.calls}">${nf0.format(r.calls)}</td><td>${fSec(r.total_s)}</td>` +
            `<td style="color:${heat(total ? 100 * r.total_s / total : 0, SCALES.pct_of_total_time) || "inherit"}">${fPct(total ? 100 * r.total_s / total : 0)}</td>` +
            `<td style="color:${TONE.avg_ms}">${nf0.format(r.avg_ms)}</td><td>${fSec(r.sql_s)}</td><td>${fSec(r.python_s)}</td>` +
            `<td>${r.p95_s == null ? "–" : fSec(r.p95_s)}</td><td>${fSec(r.max_s)}</td><td>${nf1.format(r.queries_per_call)}</td></tr>`;
    }).join("");
    $("#selTbl").innerHTML = `<thead><tr><th class="op l">item</th><th>calls</th><th>total s</th><th>% tot</th><th>avg ms</th><th>sql s</th><th>py s</th><th>p95 s</th><th>max s</th><th>qry/call</th></tr></thead><tbody>${rows}</tbody>`;
}

for (const T of Object.values(TABLES)) {
    $(T.wrap).addEventListener("scroll", e => {                          // append the next chunk near the bottom
        const w = e.target;
        if (T.shown >= T.rows.length || w.scrollTop + w.clientHeight < w.scrollHeight - 400) return;
        const next = Math.min(T.shown + TABLE_CHUNK, T.rows.length), cols = visCols();
        $(T.tbl + " tbody").insertAdjacentHTML("beforeend", T.rows.slice(T.shown, next).map(r => T.by === "slowq" ? sqRowHtml(r) : rowHtml(r, T.by, cols, T.denom)).join(""));
        T.shown = next;
        setTableCount(T);
    });
}

/* ---- most common errors and warnings ----------------------------------- */
// which operation(s) were running when a message was logged
function duringCell(r) {
    if (!r.ops.length) return `<td class="l dim" data-tip="${esc("Not logged during a request or job (start-up, another process, or a worker with no matching request).")}">–</td>`;
    const [op, n] = r.ops[0];
    const tip = "<b>Logged while these ran</b>" + r.ops.map(([o, c]) => `<br>${esc(o)} <span style="color:var(--dim)">× ${nf0.format(c)}</span>`).join("") +
        (r.ops_n > r.ops.length ? `<br><span style="color:var(--dim)">…and ${r.ops_n - r.ops.length} more</span>` : "") +
        (r.unattributed ? `<br><span style="color:var(--dim)">${nf0.format(r.unattributed)} not tied to any request</span>` : "");
    return `<td class="l during" data-tip="${esc(tip)}">${esc(op)}${r.ops_n > 1 ? ` <span class="more">+${r.ops_n - 1}</span>` : ""}</td>`;
}

function renderIssues() {
    const v = view(), q = state.issueFilter.trim().toLowerCase(), lv = state.issueLevel;
    const rows = v.issues.filter(r =>
        (lv === "all" || (lv === "errors" ? r.level !== "WARNING" : r.level === "WARNING")) &&
        (!q || (r.message + " " + r.logger + " " + r.exc + " " + r.src).toLowerCase().includes(q)));
    const total = v.levels.errors + v.levels.warnings;
    $("#issueCount").textContent = `${nf0.format(v.levels.errors)} errors · ${nf0.format(v.levels.warnings)} warnings · ${nf0.format(v.issues_distinct)} distinct messages`;
    const short = l => l.split(".").slice(-2).join(".");
    const head = `<thead><tr><th>level</th><th>count</th><th>share</th><th class="l">message</th><th class="l">logger</th><th class="l">exception</th><th class="l">source</th><th class="l" data-tip="The operation the worker was running when this was logged (matched by worker and time). Filter that operation\'s requests away and these messages go with them.">during</th><th>first seen</th><th>last seen</th></tr></thead>`;
    const body = rows.map(r => `<tr data-i="${v.issues.indexOf(r)}"${isIssuePicked(r) ? ` class="picked"` : ""}>` +
        `<td><span class="badge ${r.level === "WARNING" ? "b-mixed" : "b-slow-queries"}">${esc(r.level)}</span></td>` +
        `<td style="color:${TONE.calls}">${nf0.format(r.n)}</td>` +
        `<td>${fPct(total ? 100 * r.n / total : 0)}</td>` +
        `<td class="l msg" data-tip="${esc(esc(r.sample) + '<span class="scale">Click for details · right-click to add/remove it as a histogram item.</span>')}">${esc(r.message)}${ffBadge("issue", issueKeyOf(r))}</td>` +
        `<td class="l" title="${esc(r.logger)}">${esc(short(r.logger))}</td>` +
        `<td class="l">${esc(r.exc || "")}</td><td class="l">${esc(r.src || "")}</td>${duringCell(r)}` +
        `<td>${esc(fStamp(r.first))}</td><td>${esc(fStamp(r.last))}</td></tr>`).join("") ||
        `<tr><td class="empty" colspan="10">${v.issues.length ? "No matching messages" : "No warnings or errors in this selection"}</td></tr>`;
    $("#issuesTbl").innerHTML = head + `<tbody>${body}</tbody>`;
}
