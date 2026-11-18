"use strict";

/* Slow queries (PostgreSQL slow-query logs): their table, SQL laid out and highlighted, the form of one.
 * One of the page's scripts: templates/index.html lists them in load order (see ARCHITECTURE.md). */

/* ---- slow queries (PostgreSQL slow-query logs): one table row per query, a form with its SQL ---------------- */
// one row per shape: the runs of one query whatever its values (its SQL with every literal replaced by ?)
const SQ_COLS = [
    {key: "last", label: "last logged", l: true, tip: "When PostgreSQL logged its last run (UTC), as that run ended."},
    {key: "db", label: "database", l: true},
    {key: "n", label: "runs", tip: "Runs of this query in the selection, whatever its values."},
    {key: "total_s", label: "total s", tip: "How long its runs ran, added up."},
    {key: "avg_s", label: "avg s"},
    {key: "max_s", label: "max s", tip: "Its longest run: clicking the row opens it."},
    {key: "head", label: "SQL (shape)", l: true, tip: "Its SQL with every literal replaced by ? and lists of values folded into one."},
];
const sqData = () => (state.data && state.data.slowq) || {rows: [], n: 0, labels: {}};
const fEpoch = s => new Date(s * 1000).toISOString().replace("T", " ").slice(0, 23);

const sqDbs = r => r.dbs.length === 1 ? r.dbs[0] || "–" : `${r.dbs.length} databases`;
function sqRowHtml(r) {
    return `<tr data-sq="${esc(r.id)}"${isSqPicked(r.shape) ? ` class="picked"` : ""}><td class="l dim">${fEpoch(r.last)}</td>` +
        `<td class="l dim" title="${esc(r.dbs.map(d => d || "–").join(", "))}">${esc(sqDbs(r))}</td>` +
        `<td style="color:${TONE.calls}">${nf0.format(r.n)}</td><td>${fSec(r.total_s)}</td><td style="color:${TONE.avg_ms}">${fSec(r.avg_s)}</td><td>${fSec(r.max_s)}</td>` +
        `<td class="op sqhead" data-tip="${esc("<b>" + esc(r.head) + "</b><span class=\"scale\">Click for its longest run · right-click to select it (its runs become a histogram item).</span>")}">${esc(r.head)}</td></tr>`;
}

function renderSqTable(T, bodyOnly) {
    const d = sqData(), {key, dir} = state.sqSort, q = state.filter.trim().toLowerCase();
    const cache = d._sorted || (d._sorted = {});
    let rows = cache[key + dir] || (cache[key + dir] = d.rows.slice().sort((a, b) => {
        const x = key === "db" ? sqDbs(a) : a[key], y = key === "db" ? sqDbs(b) : b[key];
        return (typeof x === "number" ? x - y : String(x || "").localeCompare(String(y || ""))) * dir;
    }));
    if (q) rows = rows.filter(r => (r._s || (r._s = (r.dbs.join(" ") + " " + r.head).toLowerCase())).includes(q));
    Object.assign(T, {rows, shown: Math.min(TABLE_CHUNK, rows.length), by: "slowq", denom: undefined, total: d.shapes || 0});
    setTableCount(T);
    const body = rows.slice(0, T.shown).map(sqRowHtml).join("") ||
        `<tr><td class="empty" colspan="${SQ_COLS.length}">${d.n ? "No matching slow queries" : "No slow queries in the selection"}</td></tr>`;
    const tb = $(T.tbl + " tbody");
    if (bodyOnly && tb) {
        tb.innerHTML = body;
        $(T.wrap).scrollTop = 0;
        return;
    }
    const head = SQ_COLS.map(c => `<th class="${c.l ? "l" : ""}${c.key === key ? " sorted" + (dir > 0 ? " asc" : "") : ""}" data-sqsort="${c.key}"` +
        `${c.tip ? ` data-tip="${esc(c.tip)}"` : ""}>${c.label}</th>`).join("");
    $(T.tbl).innerHTML = `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>`;
}

// SQL laid out for reading (a logged query is often one long line) and highlighted. layout: false keeps it as logged
const SQL_TOKEN = /(--[^\n]*|\/\*[\s\S]*?\*\/)|([Ee]?'(?:[^']|'')*'?)|("(?:[^"]|"")*"?)|(\$\d+|%\(\w+\)s|%s)|((?<![\w$.])\d+(?:\.\d+)?(?![\w.]))|([A-Za-z_][\w$]*)|(\s+)|([\s\S])/g;
const SQL_KW = new Set(("select from where and or not in is null as on join left right inner outer full cross lateral natural group by " +
    "order having limit offset fetch union intersect except all distinct insert into values update set delete returning with recursive " +
    "case when then else end exists between like ilike similar asc desc nulls first last for share no key nowait skip locked conflict do " +
    "nothing using over partition window filter true false any some cast interval array default").split(" "));
const SQL_BREAK = new Set(["select", "from", "where", "group", "order", "having", "limit", "offset", "union", "intersect", "except",
    "returning", "values", "set", "with", "join", "left", "right", "inner", "full", "cross", "natural", "on", "and", "or", "insert",
    "update", "delete", "for", "window"]);
const SQL_DEEPER = new Set(["and", "or", "on"]);                // continuation lines: indented under their clause
const SQL_GLUE = new Set(["left", "right", "inner", "outer", "full", "cross", "natural", "for", "do", "on", "union", "intersect",
    "except", "distinct", "is", "not"]);                         // no new line after these (LEFT JOIN, ON CONFLICT, FOR UPDATE...)
const SQL_CLS = ["sql-cm", "sql-str", "sql-qid", "sql-param", "sql-num"];   // the first five groups of SQL_TOKEN

function sqlHtml(sql, layout) {
    let out = "", depth = 0, space = false, prev = "", between = false;
    for (const m of sql.matchAll(SQL_TOKEN)) {
        const tok = m[0], g = m.slice(1).findIndex(x => x !== undefined), word = g === 5 ? tok.toLowerCase() : "";
        if (g === 6 && layout) { space = true; continue; }          // whitespace: one space, or the new line below
        if (layout) {
            const brk = out && SQL_BREAK.has(word) && !SQL_GLUE.has(prev) && !(word === "and" && between);   // x BETWEEN a AND b
            if (brk) out += "\n" + "  ".repeat(depth * 2 + (SQL_DEEPER.has(word) ? 1 : 0));
            else if (space) out += " ";
            space = false;
            if (tok === "(") depth++;
            else if (tok === ")") depth = Math.max(0, depth - 1);
        }
        if (word === "between") between = true;
        else if (word === "and") between = false;
        if (g !== 6) prev = word;
        out += g >= 0 && g < 5 ? `<span class="${SQL_CLS[g]}">${esc(tok)}</span>` : SQL_KW.has(word) ? `<span class="sql-kw">${esc(tok)}</span>` : esc(tok);
    }
    return out;
}

function renderSqlPane() {
    const d = form && form.kind === "slowq" && form.d;
    if (!d || !$("#sqlPane")) return;
    const lay = form.layout !== false;
    $("#sqlPane").innerHTML = `<div class="exhead"><span class="seg">` +
        `<button class="btn ghost${lay ? " on" : ""}" data-sql-mode="1">Formatted</button><button class="btn ghost${lay ? "" : " on"}" data-sql-mode="0">As logged</button></span>` +
        `<button class="btn ghost" data-copy="${esc(d.sql)}">Copy</button></div>` +
        (d.cut ? `<div class="sub">The query was longer than 100,000 characters: the rest is cut.</div>` : "") +
        `<pre class="full sql">${sqlHtml(d.sql, lay)}</pre>`;
}

function sameShapeSection(d) {
    const s = d.same;
    if (!s.n) return `<div class="sub">No run of this query is left in the selection.</div>`;
    const f = (k, val) => `<div class="fact"><span class="k">${k}</span><span class="val">${val}</span></div>`;
    const rows = s.items.map(x => `<tr class="link${x.id === d.id ? " picked" : ""}" data-sq-open="${esc(x.id)}"><td class="l">${fEpoch(x.t)}</td>` +
        `<td class="l dim">${esc(x.db || "–")}</td><td style="color:${TONE.avg_ms}">${fSec(x.s)}</td></tr>`).join("");
    return `<div class="pane-card"><div class="facts">` + f("Runs", `<span style="color:${TONE.calls}">${nf0.format(s.n)}</span>`) + f("Total time", fDur(s.total_s)) +
        f("Average · max", `${fSec(s.total_s / s.n)} s · ${fSec(s.max_s)} s`) + f("First · last", `${fEpoch(s.first)} · ${fEpoch(s.last)}`) + `</div>` +
        `<div class="msg-block"><span class="k">Shape <span class="sub">its SQL with every literal replaced by ?, lists folded</span></span>` +
        `<pre class="full sql">${sqlHtml(d.shape_text, true)}</pre></div></div>` +
        `<div class="pane-card">${durHist(s.durations, [["this one", 1000 * d.s, "#ef8a5a"]], "runs")}</div>` +
        `<div class="pane-card"><div class="sub">${s.n > s.items.length ? `The ${nf0.format(s.items.length)} longest runs, in time order` : "Every run, in time order"}; click one to open it.</div>` +
        `<div class="table-wrap occwrap"><table class="static occtbl"><thead><tr><th class="l">logged at</th><th class="l">database</th><th>duration s</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
}

function runningSection(r) {
    if (!r.n) return `<div class="sub">No request or job of its database overlaps it in the loaded logs.</div>`;
    const rows = r.items.map(o => `<tr class="link" data-run-op="${esc(o.operation)}"><td class="l">${fStamp(o.stamp)}</td>` +
        `<td class="l op">${esc(o.operation)}</td><td class="l dim">${esc(o.family)}</td><td style="color:${TONE.avg_ms}">${fSec(o.seconds)}</td></tr>`).join("");
    return `<div class="sub srcnote">The requests and jobs whose run overlaps the query's, to the second, in its database${r.n > r.items.length ? ` (the ${nf0.format(r.items.length)} longest of ${nf0.format(r.n)})` : ""}. ` +
        `Matched by time only: the slow-query log names no worker. Click one to open its operation.</div>` +
        `<div class="table-wrap occwrap"><table class="static occtbl"><thead><tr><th class="l">ended at</th><th class="l">operation</th><th class="l">type</th><th>seconds</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

// the slow queries that overlapped an operation / app / type's calls (its "Logged during" tab)
function sqDuringSection(sd, grouped) {
    const head = `<div class="pane-head"><b>Slow queries during it</b></div>`;
    if (!sd.n) return head + `<div class="sub">No slow query of the slow-query logs overlaps ${grouped ? "its operations' calls" : "its calls"}.</div>`;
    const rows = sd.items.map(x => `<tr class="link" data-sq-open="${esc(x.id)}"><td class="l">${fEpoch(x.last)}</td>` +
        `<td class="l dim" title="${esc(x.dbs.map(d => d || "–").join(", "))}">${esc(sqDbs(x))}</td><td style="color:${TONE.calls}">${nf0.format(x.n)}</td>` +
        `<td>${fSec(x.total_s)}</td><td>${fSec(x.max_s)}</td><td>${nf0.format(x.calls)}</td><td class="l op sqhead" title="${esc(x.head)}">${esc(x.head)}</td></tr>`).join("");
    return head + `<div class="sub srcnote">${plural(sd.runs, "run")} of ${plural(sd.n, "distinct slow query", "distinct slow queries")} ran while ` +
        `${grouped ? "its operations" : "it"} did: their spans overlap, to the second, in the same database` +
        `${sd.n > sd.items.length ? ` (the ${nf0.format(sd.items.length)} with the most time shown)` : ""}. One row per query whatever its values. ` +
        `Matched by time only: the slow-query log names no worker, so a query may belong to another request that ran meanwhile. Click one to open its longest run.</div>` +
        `<div class="table-wrap occwrap"><table class="static occtbl"><thead><tr><th class="l">last logged</th><th class="l">database</th>` +
        `<th data-tip="Its runs that overlap these calls.">runs</th><th>total s</th><th>max s</th>` +
        `<th data-tip="How many of the calls its runs overlap, added up.">calls</th><th class="l">SQL (shape)</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function openSlowq(id) {
    if (!state.data) return false;
    pushForm();
    const r = sqData().rows.find(x => x.id === id);                     // a shape's row, opened on its longest run
    form = {kind: "slowq", id, shape: r && r.shape, layout: form && form.kind === "slowq" ? form.layout : true};
    const seq = ++detailSeq;
    raw.seq++;
    const loading = `<div class="sub">Loading…</div>`;
    const head = d => `<div class="mhead"><span class="badge b-slow-queries">slow query</span>` +
        (d ? `<span><b>${fSec(d.s)} s</b></span><span class="sub">${esc(d.db || "no database")} · logged ${fEpoch(d.t)} UTC</span>` +
             (state.data.hist_key ? selButton().replace("Selected items are plotted", "Selecting a slow query plots the runs of its shape") : "") : "") + `</div>`;
    const tabs = [{id: "sql", label: "SQL", html: `<div class="pane-card" id="sqlPane">${loading}</div>`},
                  {id: "same", label: r ? `Same shape (${nf0.format(r.n)})` : "Same shape", html: `<div id="sameSection">${loading}</div>`}];
    const logs = !SNAP && state.files.some(f => f.kind === "log");
    if (logs) tabs.push({id: "running", label: "Running at the same time", html: `<div class="pane-card" id="runSection">${loading}</div>`});
    showForm("Slow query", head(null), tabs, "slowq");
    const p = selectionParams();
    p.set("id", id);
    api("/api/slowq?" + p).then(d => {
        if (seq !== detailSeq) return;
        form.d = d;
        form.shape = d.shape;
        const f = (k, val) => `<div class="fact"><span class="k">${k}</span><span class="val">${val}</span></div>`;
        $("#mSheet").innerHTML = head(d) + `<div class="facts">` + f("Started", `≈ ${fEpoch(d.t - d.s)}`) + f("Logged (ended)", fEpoch(d.t)) +
            f("Duration", `<span style="color:${TONE.avg_ms}">${fSec(d.s)} s</span>`) + f("Database", esc(d.db || "–")) +
            f("Same shape", `${nf0.format(d.same.n)} run${d.same.n === 1 ? "" : "s"} in the selection`) + `</div>`;
        renderSqlPane();
        $("#sameSection").innerHTML = sameShapeSection(d);
        setTabLabel("same", `Same shape (${nf0.format(d.same.n)})`);
    }).catch(e => {
        if (seq !== detailSeq) return;
        for (const id_ of ["#sqlPane", "#sameSection"]) $(id_).innerHTML = `<div class="sub err">Could not load the query: ${esc(e.message)}</div>`;
    });
    if (!logs) return true;
    p.set("running", "1");
    api("/api/slowq?" + p).then(rn => {
        if (seq !== detailSeq) return;
        $("#runSection").innerHTML = runningSection(rn);
        setTabLabel("running", `Running at the same time (${nf0.format(rn.n)})`);
    }).catch(e => { if (seq === detailSeq) $("#runSection").innerHTML = `<div class="sub err">${esc(e.message)}</div>`; });
    return true;
}
