"use strict";

/* renderAll() (every panel redrawn from state.data), the statistics cards, the Request time popup, the
 * collapsed panels.
 * One of the page's scripts: templates/index.html lists them in load order (see ARCHITECTURE.md). */

/* ---------------------------------------------------------------------- *
 * rendering: analysis
 * ---------------------------------------------------------------------- */
function renderAll() {
    renderFiles();
    renderDbs();
    renderTime();
    renderSeriesBtn();
    renderFfButton();
    const has = !!state.data;
    for (const id of ["statsGroup", "tablePanel", "issuesPanel"]) $("#" + id).hidden = !has;
    $("#histPanel").hidden = !has || !state.data.hist_key;                       // no per-minute data in an older import
    $("#exportBtn").hidden = $("#exportHtmlBtn").hidden = !has || state.data.imported;   // re-exporting an import would just hand back what it already is
    $("#selPanel").hidden = !has || !pickedCount();
    const hasSq = state.files.some(f => f.kind === "slowq") || sqData().n > 0;   // the slow-query view only once such a log is loaded
    $('#tableGroup option[value="slowq"]').hidden = !hasSq;
    if (!hasSq && state.tableGroup === "slowq") $("#tableGroup").value = state.tableGroup = "operation";
    hideTip();                                                                     // its element is about to be replaced
    if (!has) return;
    try {
        renderStats();
        if (!state.hidden.hist) renderHist();                                      // hidden panels are drawn when opened
        renderTable();
        if (!state.hidden.issues) renderIssues();
        applyCollapsed();
    } catch (e) {                                                                  // e.g. an imported summary missing a part
        console.error(e);
        setNotice("Some of this data could not be shown (" + e.message + "). An imported summary may be from another version or edited.", true);
    }
}

const view = () => state.data.view;

function stat(k, v, s, tipText, cls, extra) {
    return `<div class="stat ${cls || ""}"${tipText ? ` data-tip="${esc(tipText)}"` : ""}>` +
        `<span class="k">${k}</span><span class="v">${v}</span><span class="s">${s || "&nbsp;"}</span>${extra || ""}</div>`;
}

// HTTP status: a colored code badge followed by its request count
function statusCard(status) {
    const items = status.map(([code, n]) =>
        `<span class="code c${"2345".includes(code[0]) ? code[0] : "x"}"><i>${esc(code)}</i><span class="n">${nf0.format(n)}</span></span>`).join("");
    return `<div class="stat wide" data-tip="Number of requests per HTTP status code."><span class="k">HTTP status · requests</span>` +
        `<div class="codes">${items || `<span class="s">none</span>`}</div></div>`;
}

// errors and warnings: HTTP 5xx responses plus ERROR / WARNING log entries
function errorsCard(s, lv, errPct) {
    const row = (cls, tag, n, what) =>
        `<span class="code ${cls}" data-tip="${esc(what)}"><i>${tag}</i><span class="n">${nf0.format(n)}</span></span>`;
    return `<div class="stat"><span class="k">Errors &amp; warnings</span><div class="codes">` +
        row("c5", "5xx", s.errors_5xx, `HTTP 5xx responses: ${fPct(errPct)} of requests`) +
        row("c5", "ERROR", lv.errors, "Log entries at level ERROR or CRITICAL") +
        row("c4", "WARN", lv.warnings, "Log entries at level WARNING") + `</div></div>`;
}

function renderStats() {
    const v = view(), s = v.stats, full = state.data.full_totals, iv = state.interval;
    const share = (a, b) => iv && b ? `${fPct(100 * a / b)} of selection` : "";
    const sqlPct = s.sql_s + s.python_s ? 100 * s.sql_s / (s.sql_s + s.python_s) : 0;
    const errPct = s.requests ? 100 * s.errors_5xx / s.requests : 0;
    $("#stats").innerHTML =
        stat(iv ? "Time interval" : "Time window", fDur(s.duration_s), `${fStamp((s.window || [])[0])} → ${fStamp((s.window || [])[1])}`, "First to last log timestamp of the analyzed lines.", "wide") +
        stat("Log entries", nf0.format(s.header_lines), `${nf0.format(s.lines)} lines · ${v.files.length} file${v.files.length > 1 ? "s" : ""}`,
            "A log entry starts with a timestamp. Tracebacks and other continuation lines belong to the entry above them, so lines ≥ entries.") +
        stat("Requests", nf0.format(s.requests),
            share(s.requests, full.req_calls) || ((s.req_per_s ? nf2.format(s.req_per_s) + " / s avg" : "") + (s.unparsable ? ` · ${nf0.format(s.unparsable)} unparsable` : "")) +
                (s.held_n ? ` · ${nf0.format(s.held_n)} held open` : ""),
            "Recognized log formats: " + ((s.formats || []).map(([k, n]) => `${esc(k)} (${nf0.format(n)})`).join("; ") || "none") +
                (s.held_n ? `. ${nf0.format(s.held_n)} WebSocket / long-poll connections were held open for ${fDur(s.held_s)} in total; that is waiting, so it is not counted as processing time.` : "")) +
        stat("Request time", fDur(s.request_s), share(s.request_s, full.req_total_s) || `${nf0.format(s.request_s)} s across all workers`,
            "Sum of SQL + Python time of every HTTP request. Can exceed the window because workers run in parallel.",
            "filtercard", state.data.imported || SNAP || !s.requests ? "" : `<button class="btn ghost" id="reqHistOpen">Histogram</button>`) +
        (s.jobs ? stat("Background jobs", fDur(s.jobs_s), `${nf0.format(s.jobs)} runs · ${fPct(s.total_s ? 100 * s.jobs_s / s.total_s : 0)} of all time`,
            "Cron jobs, automation rules and module loading, timed from their log messages. They are part of the table totals but have no SQL / Python split.") : "") +
        `<div class="stat" data-tip="Time inside PostgreSQL vs everything else."><span class="k">SQL / Python</span>` +
        `<span class="v">${nf0.format(sqlPct)}% / ${nf0.format(100 - sqlPct)}%</span>` +
        `<div class="split"><i style="width:${sqlPct}%"></i><i style="width:${100 - sqlPct}%"></i></div>` +
        `<span class="s"><span class="legend-dot" style="background:#5b8def"></span>SQL ${fDur(s.sql_s)} ` +
        `<span class="legend-dot" style="background:#e6a35a"></span>Py ${fDur(s.python_s)}</span></div>` +
        stat("SQL queries", nf0.format(s.queries), s.requests ? nf1.format(s.queries / s.requests) + " per request" : "") +
        (sqData().n ? stat("Slow queries", nf0.format(sqData().n), `${fDur(sqData().total_s)} in all · longest ${fDur(sqData().max_s)} · ${plural(sqData().shapes, "shape")}`,
            "Queries of the PostgreSQL slow-query logs logged in the selection. Runs of one query with other values share a shape.",
            "filtercard", `<button class="btn ghost" id="sqOpen">List</button>`) : "") +
        errorsCard(s, v.levels, errPct) +
        stat("Operations", nf0.format(s.operations), `${nf0.format(s.apps)} apps · ${nf0.format(s.types)} types`, "Distinct operations, Odoo apps and work types in the current selection.") +
        statusCard(s.status || []) + filterCard() +
        (s.untimed ? stat("Untimed requests", nf0.format(s.untimed), "no timing data (older Odoo); counted as 0 s") : "");
}

// the "Request time" statistic's popup: how long the requests of the selection took, their average marked
let reqHistSeq = 0;
function openReqHist() {
    const seq = ++reqHistSeq, s = view().stats, avg = s.requests ? 1000 * s.request_s / s.requests : null;
    $("#reqHistBody").innerHTML = `<div class="sub loading">Loading…</div>`;
    $("#reqHistModal").hidden = false;
    api("/api/request_durations?" + selectionParams()).then(d => {
        if (seq === reqHistSeq) $("#reqHistBody").innerHTML = durHist(d.durations, avg === null ? [] : [["avg", avg, TONE.avg_ms]], "requests");
    }).catch(e => {
        if (seq === reqHistSeq) $("#reqHistBody").innerHTML = `<div class="sub err">Could not load the histogram: ${esc(e.message)}</div>`;
    });
}

function applyCollapsed() {
    for (const [name, panel] of [["hist", "#histPanel"], ["table", "#tablePanel"], ["sel", "#selPanel"], ["issues", "#issuesPanel"]]) {
        $(panel).classList.toggle("collapsed", state.hidden[name]);
        $(`[data-toggle="${name}"]`).textContent = state.hidden[name] ? "Show" : "Hide";
    }
}
