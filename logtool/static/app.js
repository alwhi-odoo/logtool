"use strict";

const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

const store = {
    get(k, d) { try { const v = localStorage.getItem("logtool." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem("logtool." + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
};

/* Filter hierarchy: files -> databases -> time interval. Everything below the
 * filter row (stats, histogram, table, errors) follows all three. */
const state = {
    files: [],
    excludedFiles: new Set(),            // ids of loaded files left out
    excluded: new Set(),                 // database names left out ("-" = lines without a database)
    dbInit: false,                       // first load picks the busiest database
    anchor: null,                        // {from, to} of the first bar clicked, while a second click may still extend it
    interval: null,                      // null = whole selection, else {from, to} in unix seconds, to exclusive
    data: null,                          // last /api/analyze response
    rowsAll: null,                       // every operation row of the current selection (kept between responses)
    rowsKey: null,
    hidden: {hist: store.get("hideHist", false), table: store.get("hideTable", false), issues: store.get("hideIssues", false), sel: store.get("hideSel", false)},
    sort: {key: "total_s", dir: -1},
    filter: "",
    // chart items besides "total": search terms / table rows [{by: null|"operation"|"app"|"family", term, label, bars, lines, hue}]
    // and pinned errors / warnings [{level, logger, message, label, bars, lines, hue}]; bars / lines: Sets of SERIES keys
    histTags: [],
    issueTags: [],
    totalSeries: {bars: new Set(["req"]), lines: new Set(["avg_all"])},   // what the "total" item plots
    histSize: "auto",                   // bucket size in minutes; "auto" = 10 min (or ~200 bars when focused)
    focus: null,                         // "Focus interval": {from, to} in unix seconds, the only span the chart shows
    focusHist: null,                     // ... and its timeline, at the finer resolution the server sends for it
    factFilters: [],                     // values picked in the details behind a row (see toggleFact)
    extraDomain: null,                   // the rest of the filter editor's domain: a prefix list the server evaluates per occurrence
    tableGroup: ["operation", "app", "family"].includes(store.get("tableGroup")) ? store.get("tableGroup") : "operation",
    issueLevel: "all",
    issueFilter: "",
    histFit: store.get("histFit", true),    // squeeze every bucket into the panel width instead of scrolling
    tz: store.get("tz", "UTC"),          // display-only: relabels the histogram, logs stay parsed and filtered in UTC
};
let analyzeSeq = 0;
const DEFAULT_HIDDEN = ["pct_sql_within_op", "pct_python_within_op", "http_5xx", "bottleneck"];
state.hiddenCols = new Set(Array.isArray(store.get("hiddenCols")) ? store.get("hiddenCols") : DEFAULT_HIDDEN);   // like an Odoo list view

/* ---------------------------------------------------------------------- *
 * formatting
 * ---------------------------------------------------------------------- */
const nf0 = new Intl.NumberFormat("en-US", {maximumFractionDigits: 0});
const nf1 = new Intl.NumberFormat("en-US", {minimumFractionDigits: 1, maximumFractionDigits: 1});
const nf2 = new Intl.NumberFormat("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const fSec = v => v >= 100 ? nf0.format(v) : v >= 1 ? nf1.format(v) : nf2.format(v);
const fPct = v => nf1.format(v) + "%";
const fBytes = n => n >= 1e9 ? (n / 1e9).toFixed(2) + " GB" : n >= 1e6 ? (n / 1e6).toFixed(1) + " MB" : n >= 1e3 ? (n / 1e3).toFixed(0) + " KB" : n + " B";
const fCompact = n => n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n);
function fDur(s) {
    if (s == null) return "-";
    if (s < 60) return Math.round(s) + " s";
    const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
    if (h >= 24) return Math.floor(h / 24) + " d " + (h % 24) + " h";
    return h ? h + " h " + m + " m" : m + " m " + Math.round(s % 60) + " s";
}
const fStamp = s => s ? esc(String(s).replace("T", " ")) : "?";
// seconds from first to last seen; a minute stamp (YYYY-MM-DDTHH:MM) counts its whole last minute
function spanSecs(first, last) {
    if (!first || !last) return null;
    const t = v => Date.parse(String(v).replace(" ", "T") + "Z"), a = t(first), b = t(last);
    return isNaN(a) || isNaN(b) ? null : (b - a) / 1000 + (String(last).length <= 16 ? 60 : 0);
}
// a time span in days / hours / minutes: "3 d 4 h", "2 h 5 m", "12 m"
function fSpan(s) {
    if (s < 60) return Math.max(1, Math.round(s)) + " s";
    const h = Math.floor(s / 3600), m = Math.round(s % 3600 / 60);
    return h >= 24 ? Math.floor(h / 24) + " d " + (h % 24) + " h" : h ? h + " h " + m + " m" : m + " m";
}
const dbLabel = n => n === "-" ? "None" : n;

/* ---------------------------------------------------------------------- *
 * color coding: the "% tot" column runs green (small share) -> yellow -> red
 * (big share) on a log scale; calls and avg ms have fixed colors.
 * ---------------------------------------------------------------------- */
const SCALES = {
    pct_of_total_time: {lo: 1, hi: 15, unit: "%", why: "share of all request time"},
};
const TONE = {calls: "#6ea0ff", avg_ms: "#e6c15a"};

function heat(v, sc) {
    if (!(v > 0)) return null;
    const t = Math.min(1, Math.max(0, Math.log(v / sc.lo) / Math.log(sc.hi / sc.lo)));
    const hue = t < .5 ? 140 - (t / .5) * 85 : 55 - ((t - .5) / .5) * 53;     // green -> amber -> red
    return `hsl(${hue.toFixed(0)} 65% 62%)`;
}

/* ---------------------------------------------------------------------- *
 * table columns  (desc = tooltip text)
 * ---------------------------------------------------------------------- */
const COLS = [
    {key: "operation", label: "operation", cls: "op",
     desc: "The specific thing being timed: a request such as <b>call_kw sale.order.action_confirm</b> or <b>/shop/&lt;slug&gt;</b> (ids, tokens and slugs are collapsed so one page is one row), or a background job such as <b>cron: Mail: Email Queue Manager</b>."},
    {key: "family", label: "type", cls: "l",
     desc: "What kind of work this is: <b>ui.read</b> / <b>ui.write</b> / <b>ui.action</b> (buttons, workflow steps) / <b>ui.method</b> / <b>ui.onchange</b> / <b>ui.load</b> (web client start-up), <b>api.*</b> (XML-RPC, JSON-RPC), <b>auth</b>, <b>report</b>, <b>import</b>, <b>export</b>, <b>assets</b>, <b>messaging</b> (bus, discuss), <b>website</b>, <b>controller</b> (other module routes), <b>not-found</b> (404s), <b>probe</b> (scanners), <b>internal</b>, and the background jobs <b>cron</b>, <b>automation</b>, <b>module-load</b>."},
    {key: "app", label: "app", cls: "l",
     desc: "Which Odoo app owns the work: the module prefix of the model (<b>sale</b>.order, <b>stock</b>.picking), the module that owns the URL, or the prefix of a cron job's name (<b>Mail</b>: Email Queue Manager). <b>web</b> is the core client, <b>base</b> is settings, users and technical models."},
    {key: "total_s", label: "total s", f: fSec,
     desc: "Total seconds all calls kept a worker busy: <b>SQL + Python</b> time for requests, or the duration the log reports for cron jobs, automation rules and module loading."},
    {key: "pct_of_total_time", label: "% tot", f: fPct, color: true,
     desc: "This operation's share of <b>all</b> request time in the current selection (the time interval, if one is set)."},
    {key: "sql_s", label: "sql s", f: fSec,
     desc: "Seconds spent inside PostgreSQL. Odoo logs it on every request line as the second number after the query count. Background jobs are logged as one duration, so they show – here."},
    {key: "pct_sql_within_op", label: "sql%", f: v => nf0.format(v) + "%",
     desc: "SQL time as a percentage of <i>this operation's</i> own total time."},
    {key: "python_s", label: "py s", f: fSec,
     desc: "Seconds not spent in SQL: Python code, ORM computation, rendering, hashing. The last number on the Odoo request line."},
    {key: "pct_python_within_op", label: "py%", f: v => nf0.format(v) + "%",
     desc: "Python time as a percentage of <i>this operation's</i> own total time."},
    {key: "calls", label: "calls", f: nf0.format.bind(nf0), tone: true,
     desc: "Number of requests of this operation."},
    {key: "avg_ms", label: "avg ms", f: v => nf0.format(v), tone: true,
     desc: "Mean time per call in milliseconds: <b>total time ÷ calls</b>."},
    {key: "p95_s", label: "p95 s", f: fSec,
     desc: "95% of calls finished faster than this. Estimated from up to 20,000 sampled calls per operation. Shows the slow tail that the average hides."},
    {key: "max_s", label: "max s", f: fSec,
     desc: "The single slowest call seen."},
    {key: "queries_per_call", label: "qry/call", f: v => nf1.format(v),
     desc: "Average SQL queries per call: <b>queries ÷ calls</b>. Very high values point at N+1 patterns, where batching helps more than an index."},
    {key: "ms_sql_per_query", label: "ms/qry", f: v => nf1.format(v),
     desc: "Average SQL time per query in ms: <b>SQL time ÷ queries</b>. High values mean individual slow queries (index / plan / domain candidates)."},
    {key: "http_5xx", label: "5xx", f: nf0.format.bind(nf0),
     desc: "Calls that returned an HTTP 5xx server error."},
    {key: "bottleneck", label: "bottleneck", badge: true,
     desc: "Where the time is, hence what kind of fix can help.<br><b>python</b>: SQL &lt; 35% of time.<br><b>slow-queries</b>: ≥ 20 ms per query.<br><b>too-many-qry</b>: ≥ 60 queries per call.<br><b>mixed</b>: none of the above.<br><b>job</b>: cron / automation / module loading, logged as one duration with no SQL / Python split."},
];

/* ---------------------------------------------------------------------- *
 * tooltip
 * ---------------------------------------------------------------------- */
const tip = $("#tip");
function showTip(html, x, y) {
    tip.innerHTML = html;
    tip.classList.add("on");
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.max(8, Math.min(x, innerWidth - w - 8)) + "px";
    tip.style.top = (y + h + 16 > innerHeight ? y - h - 12 : y + 16) + "px";
}
const hideTip = () => tip.classList.remove("on");

document.addEventListener("mouseover", e => {
    const t = e.target.closest("[data-tip]");
    if (!t) return;
    const r = t.getBoundingClientRect();
    showTip(t.dataset.tip, r.left, r.bottom - 12);
});
document.addEventListener("mouseout", e => {
    if (e.target.closest("[data-tip]")) hideTip();
});

function colTip(c) {
    let h = "<b>" + esc(c.label) + "</b><br>" + c.desc;
    const sc = SCALES[c.key];
    if (sc) h += `<span class="scale">Color: green ≤ ${sc.lo}${sc.unit}, red ≥ ${sc.hi}${sc.unit} (${sc.why}).</span>`;
    return h;
}

/* ---------------------------------------------------------------------- *
 * busy indicator: fixed bottom-right, whatever the scroll position
 * ---------------------------------------------------------------------- */
const tasks = new Map();
let uploadPct = null;                             // null unless a file is being sent
function busy(key, text) {
    if (text) tasks.set(key, text); else tasks.delete(key);
    const on = tasks.size > 0;
    $("#busy").hidden = !on;
    if (on) $("#busyText").textContent = [...tasks.values()].pop();
    // the bar in the Files box follows the same tasks: it fills while uploading and
    // sweeps while analyzing, so it stays active for the whole loading process
    const bar = $("#progress");
    bar.hidden = !on;
    bar.classList.toggle("busy", on && uploadPct === null);
    $("#progress > div").style.width = uploadPct === null ? "" : uploadPct + "%";
}

/* ---------------------------------------------------------------------- *
 * API
 * ---------------------------------------------------------------------- */
async function api(url, opts) {
    const r = await fetch(url, opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
}

async function refresh() {                       // files changed: reload the list, then analyze
    state.files = await api("/api/files");
    const ids = new Set(state.files.map(f => f.id));
    state.excludedFiles.forEach(i => ids.has(i) || state.excludedFiles.delete(i));
    if (state.excludedFiles.size >= state.files.length) state.excludedFiles.clear();
    if (!state.files.length) {
        analyzeSeq++;
        busy("analyze");
        Object.assign(state, {data: null, dbInit: false, interval: null, anchor: null, rowsAll: null, rowsKey: null, focus: null, focusHist: null,
            factFilters: [], extraDomain: null, histTags: [], issueTags: [], totalSeries: defaultSeries("total")});
        focusSeq++;
        busy("focus");
        renderFfButton();
        state.excluded.clear();
        state.excludedFiles.clear();
    }
    renderAll();
    if (state.files.length) await analyzeNow();
}

// files that could not be read (completely), for the notice line
const fileProblems = () => state.files.filter(f => f.problem).map(f => `${f.name}: ${f.problem}`).join(" · ");
let carryNotice = "";                             // a message that must survive the re-analysis it triggered

function selectionParams() {                      // the file -> database -> time interval selection, as the server takes it
    const p = new URLSearchParams();
    state.excluded.forEach(d => p.append("exclude_db", d));
    state.excludedFiles.forEach(i => p.append("exclude_file", i));
    if (state.interval) { p.set("from", state.interval.from); p.set("to", state.interval.to); }
    if (state.factFilters.length) p.set("ff", JSON.stringify(state.factFilters));
    if (state.extraDomain) p.set("xd", JSON.stringify(state.extraDomain));
    return p;
}

// Downloads the current selection's summary, scrubbed of database / file names and raw log lines
// (see app.py's EXPORT_NOTE), by simply navigating to the attachment: the browser does the download.
async function exportSummary() {
    busy("export", "Exporting…");
    try {
        const r = await fetch("/api/export?" + selectionParams());
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
        const name = (/filename="([^"]+)"/.exec(r.headers.get("Content-Disposition") || "") || [])[1] || "logtool-export.json";
        const url = URL.createObjectURL(await r.blob()), a = document.createElement("a");
        a.href = url;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
    } catch (e) {
        setNotice("Export failed: " + e.message, true);
    } finally {
        busy("export");
    }
}

// what is pinned (chart items, selected errors): the server sends those even outside its top-N lists
function pinsParam() {
    const ops = state.histTags.filter(t => t.by === "operation").map(t => t.term), issues = state.issueTags.map(issueKeyOf);
    return ops.length || issues.length ? JSON.stringify({ops, issues}) : "";
}

// true when this request's answer is what the page now shows (a newer one did not overtake it)
async function analyzeNow() {
    const seq = ++analyzeSeq;                     // only the newest request may update the page
    busy("analyze", "Analyzing…");
    const sent = new Set(state.excluded);
    try {
        const p = selectionParams();
        if (pinsParam()) p.set("pins", pinsParam());
        if (!state.dbInit) p.set("init", "1");
        if (state.rowsAll) p.set("rows", state.rowsKey);                              // operation rows are only re-sent when the selection changes
        if (state.data && state.data.hist_key) p.set("hist", state.data.hist_key);  // interval changes do not resend the histogram
        const j = await api("/api/analyze?" + p);
        if (seq !== analyzeSeq) return false;
        if (j.hist.ops === null && state.data && state.data.hist) j.hist.ops = state.data.hist.ops;      // the per-operation timelines only travel when the file/db selection changes
        if (j.view.rows !== null) { state.rowsAll = j.view.rows; state.rowsKey = j.view.rows_key; }
        delete j.view.rows;
        if (!j.window) state.interval = state.anchor = state.focus = state.focusHist = null;   // an imported summary has no timeline left to select from
        else if (state.interval)                  // the server clamps the interval to the logs (null: it covers them all)
            state.interval = j.interval ? {from: j.interval[0], to: j.interval[1]} : null;
        state.data = j;
        state.files = j.files;
        // the server picks the databases on the first load and when every selected one has gone away
        if (!state.dbInit || j.excluded_db.join("\n") !== [...sent].sort().join("\n")) state.excluded = new Set(j.excluded_db);
        const known = new Set(j.databases.map(d => d[0]));
        if (known.size) state.excluded.forEach(d => known.has(d) || state.excluded.delete(d));   // gone with their file
        state.dbInit = true;
        const note = carryNotice || fileProblems() ||
            (j.imported ? `Imported summary — read-only, no database or time-interval filtering${j.hist_key ? "" : " or histogram"}.` : "");
        carryNotice = "";
        setNotice(note, !!note);
    } catch (e) {
        if (seq !== analyzeSeq) return false;
        if (state.interval && /interval/.test(e.message)) {      // it no longer fits the data: fall back to the whole selection
            state.interval = state.anchor = null;
            carryNotice = "Time interval dropped: " + e.message;
            busy("analyze");
            return analyzeNow();
        }
        const files = await api("/api/files").catch(() => state.files);       // the failure may be about a file
        if (seq !== analyzeSeq) return false;
        state.data = null;
        state.files = files;
        setNotice(e.message === "Failed to fetch" ? "Could not reach the server." : `Analysis failed: ${e.message}. ${fileProblems()}`, true);
    }
    busy("analyze");
    renderAll();
    if (state.focus && state.data) loadFocus();       // the focused timeline follows the selection and filters
    if (state.data && !$("#modal").hidden && form) {  // an open form follows too (or goes back, if it is gone)
        const top = $("#mScroll").scrollTop;
        if (reopen(form)) $("#mScroll").scrollTop = top;
        else closeModal();
    }
    return true;
}

function upload(fileList) {
    const files = [...fileList];
    if (!files.length) return;
    const fd = new FormData();
    files.forEach(f => fd.append("files", f));
    const xhr = new XMLHttpRequest();
    uploadPct = 0;
    busy("upload", "Uploading… 0%");
    xhr.upload.onprogress = e => {
        if (!e.lengthComputable) return;
        uploadPct = Math.round(100 * e.loaded / e.total);
        busy("upload", `Uploading… ${uploadPct}%`);
    };
    xhr.onload = async () => {
        uploadPct = null;
        busy("upload", files.length > 1 ? `Reading ${files.length} files…` : "Reading the file…");   // stays on until analyzed (logs read before: from the parse cache)
        try { await refresh(); } catch (e) { setNotice("Could not reach the server: " + e.message, true); } finally { busy("upload"); }
        if (xhr.status !== 200) {
            let msg = xhr.statusText || String(xhr.status);
            try { const body = JSON.parse(xhr.responseText); if (body && body.error) msg = body.error; } catch (e) { /* not JSON */ }
            setNotice("Upload failed: " + msg, true);
        }
    };
    xhr.onerror = () => { uploadPct = null; busy("upload"); setNotice("Upload failed.", true); };
    xhr.open("POST", "/api/files");
    xhr.send(fd);
}

async function unload(id) {
    try {
        await api("/api/files/" + id, {method: "DELETE"});
    } catch (e) {
        setNotice("Could not unload the file: " + e.message, true);
    }
    await refresh().catch(e => setNotice("Could not reach the server: " + e.message, true));
}

// Unload everything and return every filter / view setting to its default
async function resetAll() {
    clearTimeout(selTimer);
    clearTimeout(intervalTimer);
    analyzeSeq++;
    tasks.clear();
    busy("reset", "Resetting…");
    try {
        await api("/api/reset", {method: "POST"});
        try { Object.keys(localStorage).filter(k => k.startsWith("logtool.")).forEach(k => localStorage.removeItem(k)); } catch (e) { /* private mode */ }
        Object.assign(state, {
            anchor: null, interval: null, filter: "", histTags: [], issueTags: [], histSize: "auto", histFit: true,
            totalSeries: {bars: new Set(["req"]), lines: new Set(["avg_all"])}, focus: null, focusHist: null, factFilters: [], extraDomain: null,
            tableGroup: "operation", issueLevel: "all", issueFilter: "", sort: {key: "total_s", dir: -1},
            hidden: {hist: false, table: false, issues: false, sel: false}, tz: "UTC",
        });
        focusSeq++;
        state.rowsAll = state.rowsKey = null;
        state.hiddenCols = new Set(DEFAULT_HIDDEN);
        $("#colsMenu").hidden = true;
        $("#histSeriesMenu").hidden = true;
        renderFfButton();
        onlyDb = "";
        dbScan.seq++;
        dbScan.counts = null;
        $("#dbScan").hidden = true;
        renderOnlyDb();
        for (const [id, v] of [["#filter", ""], ["#histFilter", ""], ["#tableGroup", "operation"], ["#issueLevel", "all"], ["#issueFilter", ""]]) $(id).value = v;
        setNotice("");
        await refresh();                          // no files: clears data and the remaining selection state
    } catch (e) {
        setNotice("Reset failed: " + e.message, true);
    } finally {
        busy("reset");
    }
}

/* ---------------------------------------------------------------------- *
 * selection: files, databases, time interval
 * ---------------------------------------------------------------------- */
let selTimer = null;
function reselect() {                             // instant feedback, results follow
    state.anchor = null;                          // a half-picked range belonged to the old selection
    renderFiles();
    renderDbs();
    analyzeSeq++;                                 // what is in flight answers the old selection: drop it when it lands
    clearTimeout(selTimer);
    selTimer = setTimeout(analyzeNow, 250);
}

function toggleFile(id) {
    if (state.excludedFiles.has(id)) state.excludedFiles.delete(id);
    else if (state.files.length - state.excludedFiles.size > 1) state.excludedFiles.add(id);   // keep one
    else return;
    reselect();
}

function toggleDb(name) {
    if (!state.data) return;
    const names = state.data.databases.map(d => d[0]);
    if (state.excluded.has(name)) state.excluded.delete(name);
    else if (names.filter(n => !state.excluded.has(n)).length > 1) state.excluded.add(name);   // keep one
    else return;
    reselect();
}

// timestamps are shown and typed exactly like the log lines: YYYY-MM-DD HH:MM:SS
const fTs = sec => new Date(sec * 1000).toISOString().slice(0, 19).replace("T", " ");
const TS_RE = /^(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])[ T]([01]\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?(?:[.,]\d+)?$/;
function parseTs(v) {
    const m = TS_RE.exec(v.trim());
    if (!m) return NaN;
    const sec = Date.UTC(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)) / 1000;
    return fTs(sec).slice(0, 10) === `${m[1]}-${m[2]}-${m[3]}` ? sec : NaN;      // 2026-02-31 would roll over to March
}

/* ---------------------------------------------------------------------- *
 * histogram timezone: display only. Logs are parsed and filtered in UTC
 * everywhere else on the page (time interval inputs, stats, log lines);
 * only the histogram's bar labels and tooltip switch to this timezone.
 * ---------------------------------------------------------------------- */
const LOCAL_TZ = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { return "UTC"; } })();
const TZ_LIST = (() => {
    try { return Intl.supportedValuesOf("timeZone"); } catch (e) {
        return ["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Sao_Paulo",
            "Europe/London", "Europe/Paris", "Europe/Brussels", "Europe/Berlin", "Europe/Moscow", "Africa/Cairo",
            "Asia/Dubai", "Asia/Kolkata", "Asia/Shanghai", "Asia/Tokyo", "Asia/Singapore", "Australia/Sydney", "Pacific/Auckland"];
    }
})();
// same YYYY-MM-DD HH:MM:SS shape as fTs, but rendered in state.tz instead of UTC
function fTsTz(sec) {
    if (state.tz === "UTC") return fTs(sec);
    try {
        const parts = new Intl.DateTimeFormat("en-US", {
            timeZone: state.tz, hour12: false,
            year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
        }).formatToParts(new Date(sec * 1000));
        const get = t => parts.find(p => p.type === t).value;
        return `${get("year")}-${get("month")}-${get("day")} ${get("hour")}:${get("minute")}:${get("second")}`;
    } catch (e) {
        return fTs(sec);
    }
}

function renderTzUi() {
    const sel = $("#tzSelect");
    if (!sel.options.length) {
        const opts = ["UTC"];
        if (LOCAL_TZ !== "UTC") opts.push(LOCAL_TZ);
        for (const z of TZ_LIST) if (!opts.includes(z)) opts.push(z);
        sel.innerHTML = opts.map(z => `<option value="${esc(z)}">${esc(z === "UTC" ? "UTC (log time)" : z === LOCAL_TZ ? `${z} (your browser)` : z)}</option>`).join("");
    }
    sel.value = state.tz;
    const tag = $("#tzTag");
    tag.hidden = state.tz === "UTC";
    tag.textContent = `histogram tz · ${state.tz}`;
}

let intervalTimer = null;
function setInterval_(from, to) {                 // to exclusive; null / whole window clears
    const w = state.data && state.data.window;
    state.interval = (from == null || (w && from <= w[0] && to >= w[1])) ? null : {from, to};
    if (!state.interval) state.anchor = null;
    renderTime();
    if (state.data && !state.hidden.hist) renderHist();   // show the selection at once; the numbers follow
    // scrubbing across several bars in a row should only ask the server for the last one, not every one
    analyzeSeq++;                                 // what is in flight answers the old interval: drop it when it lands
    clearTimeout(intervalTimer);
    intervalTimer = setTimeout(analyzeNow, 150);
}

function applyTimeInputs() {
    const f = parseTs($("#tFrom").value), t = parseTs($("#tTo").value);
    if (Number.isNaN(f) || Number.isNaN(t)) return timeError("Use the log format: YYYY-MM-DD HH:MM:SS");
    if (t < f) return timeError("The end is before the start.");
    state.anchor = null;
    setInterval_(f, t + 1);                       // the "To" second is inclusive
}

/* ---------------------------------------------------------------------- *
 * rendering: filter row
 * ---------------------------------------------------------------------- */
function setNotice(msg, err) {
    const n = $("#notice");
    n.hidden = !msg;
    n.textContent = msg;
    n.classList.toggle("err", !!err);
}

const resetDropText = () => {
    const none = !state.files.length;
    $("#dropText").textContent = none
        ? "Drop Odoo log files here (plain, .gz, .bz2), or a summary exported earlier (.json), or click to choose them"
        : "Drop more logs here, or click to choose them";
    $("#drop").classList.toggle("empty", none);                            // a big target while there is nothing else to see
    document.documentElement.classList.toggle("nothing-loaded", none);     // ... and the patterned page background
};

function renderFiles() {
    $("#chips").innerHTML = state.files.map(f => {
        const off = state.excludedFiles.has(f.id), isSummary = f.kind === "summary";
        return `<span class="pill${off ? " off" : ""}${f.problem ? " bad" : ""}${isSummary ? " summary" : ""}">` +
            `<button class="pname" data-file="${f.id}" title="${esc(f.name)}${f.problem ? " - " + esc(f.problem) : isSummary ? " - imported summary (read-only)" : ""} - click to ${off ? "include in" : "exclude from"} the analysis">${esc(f.name)}</button>` +
            `<span class="pn">${isSummary ? "import" : fBytes(f.size)}</span>` +
            `<button class="px" data-unload="${f.id}" title="Unload this file" aria-label="Unload ${esc(f.name)}">×</button></span>`;
    }).join("");
    resetDropText();
}

function renderDbs() {
    const box = $("#dbs"), dbs = state.data ? state.data.databases : [];
    if (!dbs.length) {
        box.innerHTML = `<span class="sub">${state.data && state.data.imported ? "Not saved in an imported summary."
            : state.files.length ? "No database names found in the log headers." : "Load a log file to see its databases."}</span>`;
        $("#dbSub").textContent = "";
        return;
    }
    box.innerHTML = dbs.map(([n, c]) => {
        const off = state.excluded.has(n);
        return `<span class="pill${off ? " off" : ""}"><button class="pname" data-db="${esc(n)}" ` +
            `title="${esc(n === "-" ? "Log lines that carry no database name" : n)} · ${nf0.format(c)} log lines - click to ${off ? "include" : "exclude"}">${esc(dbLabel(n))}</button>` +
            `<span class="pn">${fCompact(c)}</span></span>`;
    }).join("");
    $("#dbSub").textContent = `${dbs.filter(d => !state.excluded.has(d[0])).length} of ${dbs.length} selected` +
        (onlyDb ? ` · only ${onlyDb} read` : "");
}

/* ---------------------------------------------------------------------- *
 * only read one database: the server skips every other database's lines while parsing
 * ---------------------------------------------------------------------- */
let onlyDb = "";                                  // what the server reads ("" = every database)

function renderOnlyDb() {
    const inp = $("#onlyDb");
    if (document.activeElement !== inp) inp.value = onlyDb;
    inp.classList.toggle("on", !!onlyDb);
    $("#onlyDbClear").hidden = !onlyDb;
    renderDbScan();
}

async function setOnlyDb(name) {
    name = name.trim();
    if (name === onlyDb) return renderOnlyDb();
    if (state.files.some(f => f.kind !== "summary") &&
        !confirm(`Read the loaded logs again, ${name ? "keeping only database " + name : "with every database"}?`)) return renderOnlyDb();
    busy("onlydb", "Changing the database…");
    try {
        const j = await api("/api/only_db", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({db: name})});
        onlyDb = j.db || "";
        if (j.changed) {                           // the loaded logs are parsed again: nothing of the old selection holds
            analyzeSeq++;
            clearTimeout(selTimer);
            clearTimeout(intervalTimer);
            Object.assign(state, {data: null, dbInit: false, interval: null, anchor: null, rowsAll: null, rowsKey: null, focus: null, focusHist: null});
            state.excluded.clear();
            focusSeq++;
            busy("focus");
            renderOnlyDb();
            await refresh();
        }
    } catch (e) {
        setNotice("Could not set the database: " + e.message, true);
    } finally {
        busy("onlydb");
        renderOnlyDb();
    }
}

// Lists the databases of log files picked here, read in the browser (nothing is uploaded or parsed by the server):
// the dbname column of Odoo's default header format, "%(asctime)s %(pid)s %(levelname)s %(dbname)s %(name)s: ...".
const DB_HEAD = /^\d{4}-\d\d-\d\d[ T](?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d[,.]?\d*[ \t]+\S+[ \t]+[A-Z]+[ \t]+(\S*[^\s:])[ \t]+[\w.\-]+:/gm;
// ... and with anything in front of the timestamp (syslog, journald, docker): slower, so only for files where no line
// starts with a timestamp
const DB_HEAD_ANY = /^[^\n]*?\d{4}-\d\d-\d\d[ T](?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d[,.]?\d*[ \t]+\S+[ \t]+[A-Z]+[ \t]+(\S*[^\s:])[ \t]+[\w.\-]+:/gm;
const ANSI_RE = /\x1b\[[0-9;]*m/g;                // colored console logs: the server ignores these codes too
const dbScan = {counts: null, names: "", skipped: [], seq: 0};

async function scanDbs(fileList) {
    const files = [...fileList];
    if (!files.length) return;
    const seq = ++dbScan.seq, counts = new Map(), skipped = [];
    const total = files.reduce((n, f) => n + f.size, 0) || 1;
    let read = 0, shown = -1;
    const progress = () => {
        const pct = Math.floor(100 * read / total);
        if (pct !== shown) { shown = pct; busy("dbscan", `Finding databases… ${pct}%`); }
    };
    progress();
    try {
        for (const f of files) {
            const lower = f.name.toLowerCase();
            if (lower.endsWith(".bz2") || lower.endsWith(".json") || (lower.endsWith(".gz") && typeof DecompressionStream === "undefined")) {
                skipped.push(f.name);
                read += f.size;
                continue;
            }
            let stream = f.stream().pipeThrough(new TransformStream({transform(c, out) { read += c.byteLength; out.enqueue(c); }}));
            if (lower.endsWith(".gz")) stream = stream.pipeThrough(new DecompressionStream("gzip"));
            const reader = stream.pipeThrough(new TextDecoderStream()).getReader();
            let tail = "", head = DB_HEAD, found = 0;
            for (;;) {
                const {value, done} = await reader.read();
                if (seq !== dbScan.seq) { reader.cancel(); return; }     // another scan started
                const text = done ? tail : tail + value;
                const cut = done ? text.length : text.lastIndexOf("\n") + 1;   // whole lines only; the rest waits for the next piece
                tail = text.slice(cut);
                let piece = text.slice(0, cut);
                if (piece.includes("\x1b")) piece = piece.replace(ANSI_RE, "");
                let ms = [...piece.matchAll(head)];
                if (!ms.length && !found && head === DB_HEAD && piece.trim()) {      // no line starts with a timestamp: prefixed lines?
                    head = DB_HEAD_ANY;
                    ms = [...piece.matchAll(head)];
                }
                for (const m of ms) {
                    const db = m[1] === "None" || m[1] === "?" ? "-" : m[1];
                    counts.set(db, (counts.get(db) || 0) + 1);
                }
                found += ms.length;
                progress();
                if (done) break;
            }
        }
    } catch (e) {
        if (seq === dbScan.seq) setNotice(`Could not read ${files.length > 1 ? "the files" : files[0].name}: ${e.message}`, true);
        return;
    } finally {
        if (seq === dbScan.seq) busy("dbscan");
    }
    Object.assign(dbScan, {counts, skipped, names: files.map(f => f.name).join(", ")});
    $("#dbScanFilter").value = "";
    $("#dbScan").hidden = false;
    renderDbScan();
    $("#dbScanFilter").focus();
}

const DB_SCAN_MAX = 500;                          // pills shown at once; the search narrows the rest
function renderDbScan() {
    if (!dbScan.counts) return;
    const q = $("#dbScanFilter").value.trim().toLowerCase();
    const all = [...dbScan.counts].filter(([n]) => n !== "-").sort((a, b) => b[1] - a[1]);
    const hits = q ? all.filter(([n]) => n.toLowerCase().includes(q)) : all;
    const none = dbScan.counts.get("-") || 0;
    const parts = [`${nf0.format(all.length)} database${all.length === 1 ? "" : "s"} in ${dbScan.names}`];
    if (q) parts.push(`${nf0.format(hits.length)} matching`);
    if (hits.length > DB_SCAN_MAX) parts.push(`showing the ${DB_SCAN_MAX} busiest`);
    if (none) parts.push(`${fCompact(none)} lines without a database (always read)`);
    if (dbScan.skipped.length) parts.push(`not readable here: ${dbScan.skipped.join(", ")}`);
    $("#dbScanSub").textContent = parts.join(" · ");
    $("#dbScanList").innerHTML = hits.length ? hits.slice(0, DB_SCAN_MAX).map(([n, c]) =>
        `<span class="pill${n === onlyDb ? " cur" : ""}"><button class="pname" data-only-db="${esc(n)}" ` +
        `title="${esc(n)} · ${nf0.format(c)} log lines - click to read only this database">${esc(n)}</button>` +
        `<span class="pn">${fCompact(c)}</span></span>`).join("")
        : `<span class="sub">${all.length ? "No database matches." : "No database names found (Odoo's default log line format is expected)."}</span>`;
}

function timeError(msg) {
    setNotice(msg, true);
}

function renderTime() {
    renderTzUi();
    if (state.data && state.data.hist) renderHistTools();         // the focus button follows the interval
    const d = state.data, w = d && d.window, iv = state.interval;
    $("#clearHist").hidden = !iv;
    const top = $("#topInterval");
    top.hidden = !w;
    if (!w) return;
    const a = iv ? iv.from : w[0], b = iv ? iv.to : w[1];
    $("#tFrom").value = fTs(a);
    $("#tTo").value = fTs(b - 1);
    const tag = $("#ivTag");
    tag.textContent = iv ? `filtered · ${fDur(b - a)} of ${fDur(w[1] - w[0])}` : `whole selection · ${fDur(w[1] - w[0])}`;
    tag.classList.toggle("on", !!iv);
    $("#clearTop").hidden = !iv;
}

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
    $("#exportBtn").hidden = !has || state.data.imported;                        // re-exporting an import would just hand back what it already is
    $("#selPanel").hidden = !has || !pickedCount();
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

function stat(k, v, s, tipText, cls) {
    return `<div class="stat ${cls || ""}"${tipText ? ` data-tip="${esc(tipText)}"` : ""}>` +
        `<span class="k">${k}</span><span class="v">${v}</span><span class="s">${s || "&nbsp;"}</span></div>`;
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
            "Sum of SQL + Python time of every HTTP request. Can exceed the window because workers run in parallel.") +
        (s.jobs ? stat("Background jobs", fDur(s.jobs_s), `${nf0.format(s.jobs)} runs · ${fPct(s.total_s ? 100 * s.jobs_s / s.total_s : 0)} of all time`,
            "Cron jobs, automation rules and module loading, timed from their log messages. They are part of the table totals but have no SQL / Python split.") : "") +
        `<div class="stat" data-tip="Time inside PostgreSQL vs everything else."><span class="k">SQL / Python</span>` +
        `<span class="v">${nf0.format(sqlPct)}% / ${nf0.format(100 - sqlPct)}%</span>` +
        `<div class="split"><i style="width:${sqlPct}%"></i><i style="width:${100 - sqlPct}%"></i></div>` +
        `<span class="s"><span class="legend-dot" style="background:#5b8def"></span>SQL ${fDur(s.sql_s)} ` +
        `<span class="legend-dot" style="background:#e6a35a"></span>Py ${fDur(s.python_s)}</span></div>` +
        stat("SQL queries", nf0.format(s.queries), s.requests ? nf1.format(s.queries / s.requests) + " per request" : "") +
        errorsCard(s, v.levels, errPct) +
        stat("Operations", nf0.format(s.operations), `${nf0.format(s.apps)} apps · ${nf0.format(s.types)} types`, "Distinct operations, Odoo apps and work types in the current selection.") +
        statusCard(s.status || []) + filterCard() +
        (s.untimed ? stat("Untimed requests", nf0.format(s.untimed), "no timing data (older Odoo); counted as 0 s") : "");
}

/* ---- filter editor: the whole global filter as an Odoo-style domain (see FILTERS.md) ---------------- */
const plural = (n, one, many) => `${nf0.format(n)} ${n === 1 ? one : many || one + "s"}`;
const domTerms = d => (d || []).filter(x => Array.isArray(x)).length;
function filterParts() {
    const parts = [], nf = state.factFilters.reduce((n, f) => n + Object.values(f.facts).reduce((m, v) => m + v.length, 0), 0);
    if (state.excludedFiles.size) parts.push(plural(state.excludedFiles.size, "file") + " left out");
    if (state.excluded.size) parts.push(plural(state.excluded.size, "database") + " left out");
    if (state.interval) parts.push("time interval");
    if (nf) parts.push(plural(nf, "item filter"));
    if (state.extraDomain) parts.push(plural(domTerms(state.extraDomain), "custom condition"));
    return parts;
}
const viewParts = () => [pickedCount() && plural(pickedCount(), "chart item"), state.focus && "focused"].filter(Boolean);

function filterCard() {
    const parts = filterParts(), off = state.data.imported;
    return `<div class="stat filtercard" data-tip="${esc("The whole global filter as an Odoo-style domain, to read, copy, paste or write by hand (FILTERS.md explains the language).")}">` +
        `<span class="k">Filter editor</span>` +
        `<span class="s">${esc(off ? "not available on an imported summary" : [parts.join(" · ") || "the whole selection"].concat(viewParts()).join(" · "))}</span>` +
        `<button class="btn ghost" id="domOpen"${off ? " disabled" : ""}>Edit filter</button></div>`;
}

// ---- the view in the domain: chart.*, table.*, errors.*, panels.* settings (see FILTERS.md) ----
const SERIES_PUBLIC = {req: "requests", avg_all: "avg_ms", avg_py: "avg_ms.py", avg_sql: "avg_ms.sql", tot_all: "total_ms", tot_py: "total_ms.py", tot_sql: "total_ms.sql"};
const SERIES_INTERNAL = Object.fromEntries(Object.entries(SERIES_PUBLIC).map(([k, v]) => [v, k]));
const PANEL_PUBLIC = {hist: "chart", table: "table", sel: "selected", issues: "errors"};
const pubSeries = set => SERIES.map(x => x.key).filter(k => set.has(k)).map(k => SERIES_PUBLIC[k]);
const intSeries = list => new Set((list || []).map(x => SERIES_INTERNAL[x]).filter(Boolean));
const pubCol = k => k === "family" ? "type" : k;
const intCol = k => k === "type" ? "family" : k;

// everything the page shows besides the filter itself, in the shape the server writes as view settings
function viewState() {
    const cols = new Set(COLS.filter(c => c.cls !== "op").map(c => c.key));
    return {
        items: state.histTags.map(t => Object.assign(t.by ? {[pubCol(t.by)]: t.term} : {search: t.label}, {bars: pubSeries(t.bars), lines: pubSeries(t.lines)}))
            .concat(state.issueTags.map(t => ({issue: [t.level, t.logger, t.message], bars: pubSeries(t.bars).filter(x => x === "requests"), lines: pubSeries(t.lines).filter(x => x === "requests")}))),
        total: {bars: pubSeries(state.totalSeries.bars), lines: pubSeries(state.totalSeries.lines)},
        bucket: state.histSize === "auto" ? "auto" : +state.histSize,
        fit: !!state.histFit,
        focus: state.focus ? [state.focus.from, state.focus.to] : null,
        timezone: state.tz,
        table: {group: pubCol(state.tableGroup), search: state.filter, sort: [pubCol(state.sort.key), state.sort.dir > 0 ? "asc" : "desc"],
                hidden_columns: [...state.hiddenCols].filter(k => cols.has(k)).map(pubCol)},
        errors: {level: state.issueLevel, search: state.issueFilter},
        panels_hidden: Object.keys(PANEL_PUBLIC).filter(k => state.hidden[k]).map(k => PANEL_PUBLIC[k]),
    };
}

function validTz(z) {
    if (z === "UTC" || TZ_LIST.includes(z)) return z;
    try { new Intl.DateTimeFormat("en-US", {timeZone: z}); return z; } catch (e) { return "UTC"; }
}

// the reverse: a view from the server (every setting filled in) becomes the page's state
function applyView(v) {
    state.histTags = [];
    state.issueTags = [];
    for (const it of v.items) {
        const series = {bars: intSeries(it.bars), lines: intSeries(it.lines)};
        if (it.issue) {
            const [level, logger, message] = it.issue;
            if (!state.issueTags.some(t => sameIssue(t, {level, logger, message})))
                state.issueTags.push(Object.assign({level, logger, message, label: message.length > 40 ? message.slice(0, 39) + "…" : message}, series, {hue: nextHue()}));
            continue;
        }
        const by = it.search != null ? null : it.operation != null ? "operation" : it.app != null ? "app" : "family";
        const name = it.search ?? it.operation ?? it.app ?? it.type, term = by ? name : name.toLowerCase();
        if (!state.histTags.some(t => t.by === by && t.term === term))
            state.histTags.push(Object.assign({by, term, label: name}, series, {hue: nextHue()}));
    }
    state.totalSeries = {bars: intSeries(v.total.bars), lines: intSeries(v.total.lines)};
    state.histSize = v.bucket === "auto" ? "auto" : String(v.bucket);
    state.histFit = v.fit;
    store.set("histFit", state.histFit);
    focusSeq++;
    busy("focus");
    state.focus = v.focus ? {from: v.focus[0], to: v.focus[1]} : null;
    state.focusHist = null;
    state.tz = validTz(v.timezone);
    store.set("tz", state.tz);
    state.tableGroup = intCol(v.table.group);
    store.set("tableGroup", state.tableGroup);
    $("#tableGroup").value = state.tableGroup;
    state.filter = v.table.search;
    $("#filter").value = state.filter;
    state.sort = {key: intCol(v.table.sort[0]), dir: v.table.sort[1] === "asc" ? 1 : -1};
    state.hiddenCols = new Set(v.table.hidden_columns.map(intCol));
    store.set("hiddenCols", [...state.hiddenCols]);
    state.issueLevel = v.errors.level;
    $("#issueLevel").value = state.issueLevel;
    state.issueFilter = v.errors.search;
    $("#issueFilter").value = state.issueFilter;
    for (const [k, pub] of Object.entries(PANEL_PUBLIC)) {
        state.hidden[k] = v.panels_hidden.includes(pub);
        store.set("hide" + k[0].toUpperCase() + k.slice(1), state.hidden[k]);
    }
    markPicked();
    renderSeriesBtn();
    if (!$("#histSeriesMenu").hidden) renderHistSeriesMenu();
}

let domSeq = 0, domApplied = "";
const domParams = () => selectionParams();
function domMsg(html, err) {
    const m = $("#domMsg");
    m.innerHTML = html;
    m.classList.toggle("err", !!err);
}

async function postDomain(text) {
    const r = await fetch("/api/domain", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({domain: text})});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j.state;
}

function describeDomainState(st) {
    const parts = [];
    if (st.exclude_file.length) parts.push(plural(st.exclude_file.length, "file") + " left out");
    if (st.exclude_db.length) parts.push(plural(st.exclude_db.length, "database") + " left out");
    if (st.interval) parts.push("time interval");
    if (st.ff.length) parts.push(plural(st.ff.length, "item") + " with item filters");
    if (st.xd) parts.push(plural(domTerms(st.xd), "custom condition") + " (checked on every request, job and warning)");
    const v = st.view, view = [plural(v.items.length, "chart item"), v.bucket === "auto" ? "auto buckets" : fSize(v.bucket) + " buckets"];
    if (v.focus) view.push("focused on " + fDur(v.focus[1] - v.focus[0]));
    view.push(`table by ${v.table.group}`);
    return (parts.join(" · ") || "no filter: the whole selection") + ` — view: ${view.join(" · ")}`;
}

let domOpenSeq = 0;
async function openFilterEditor() {
    if (!state.data || state.data.imported) return;
    const seq = ++domOpenSeq;
    $("#domModal").hidden = false;
    $("#domText").value = "";
    $("#domText").focus();
    domMsg(`<span class="loading">Loading the current filter…</span>`);
    try {
        const p = domParams();
        p.set("view", JSON.stringify(viewState()));
        const text = (await api("/api/domain?" + p)).domain;
        if (seq !== domOpenSeq || $("#domModal").hidden) return;            // closed (or opened again) meanwhile
        domApplied = text;
        if (!$("#domText").value.trim()) $("#domText").value = text;       // never over what was typed meanwhile
        checkDomain();
    } catch (e) {
        if (seq === domOpenSeq) domMsg("Could not read the current filter: " + esc(e.message), true);
    }
}

async function checkDomain() {
    const seq = ++domSeq;
    try {
        const st = await postDomain($("#domText").value);
        if (seq === domSeq) domMsg(`<b>✓ Valid</b> · ${esc(describeDomainState(st))}`);
        return st;
    } catch (e) {
        if (seq === domSeq) domMsg(`<b>✗</b> ${esc(e.message)}`, true);
        return null;
    }
}

async function applyDomain() {
    const st = await checkDomain();                       // never runs a filter that does not check out
    if (!st || $("#domModal").hidden) return;             // (or one whose editor was closed meanwhile)
    const w = state.data.window;
    state.excludedFiles = new Set(st.exclude_file);
    state.excluded = new Set(st.exclude_db);
    state.factFilters = st.ff;
    state.extraDomain = st.xd;
    state.anchor = null;
    if (st.interval && w) {
        const from = st.interval[0] ?? w[0], to = st.interval[1] ?? w[1];
        state.interval = from <= w[0] && to >= w[1] ? null : {from, to};
    } else state.interval = null;
    state.dbInit = true;
    applyView(st.view);
    $("#domModal").hidden = true;
    renderFfButton();
    renderFiles();
    renderDbs();
    renderTime();
    await analyzeNow();
}

let domCheckTimer = null;
$("#domText").addEventListener("input", () => { clearTimeout(domCheckTimer); domCheckTimer = setTimeout(checkDomain, 400); });
$("#domText").addEventListener("keydown", e => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); applyDomain(); }
    if (e.key === "Tab") {                                // indent instead of leaving the editor
        e.preventDefault();
        const t = e.target, a = t.selectionStart;
        t.setRangeText("    ", a, t.selectionEnd, "end");
    }
});

const GROUP_LABEL = {family: "type", app: "app", operation: "operation"};
const GROUP_UNIT = {family: "types", app: "apps", operation: "operations"};
const groupName = (r, by) => by === "family" ? r.family : by === "app" ? r.app : r.operation;

/* ---- requests over time -------------------------------------------------
 * The server sends, per operation, [bucket_start, requests, sql_s, python_s, jobs_s]
 * at its finest time resolution ("base", minutes since 1970 on the log's own
 * clock); here they are merged into the bucket size picked in the UI.
 * Clicking a bucket makes it the time interval.
 *
 * What is plotted is a list of items -- "total" (everything selected) plus one per
 * search term, right-clicked table row or pinned error -- and for each item any of
 * the SERIES below, as bars and / or as lines (the "Chart items & series" popup).
 * "Focus interval" swaps in a timeline of just that interval (see /api/timeline),
 * which the server sends at a finer bucket size: down to 1 minute however long the log.
 * ---------------------------------------------------------------------- */
const SIZES = [1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 10080];      // minutes
// unit: which scale a series is drawn against (series of one unit on one side share a scale);
// val returns the displayed number, or null where it is undefined (no requests: no average)
const SERIES = [
    {key: "avg_all", group: "avg", comp: "all", unit: "avg", val: e => e[0] ? 1000 * (e[1] + e[2] + e[3]) / e[0] : null},
    {key: "avg_py", group: "avg", comp: "py", unit: "avg", val: e => e[0] ? 1000 * e[2] / e[0] : null},
    {key: "avg_sql", group: "avg", comp: "sql", unit: "avg", val: e => e[0] ? 1000 * e[1] / e[0] : null},
    {key: "tot_all", group: "tot", comp: "all", unit: "tot", val: e => 1000 * (e[1] + e[2] + e[3])},
    {key: "tot_py", group: "tot", comp: "py", unit: "tot", val: e => 1000 * e[2]},
    {key: "tot_sql", group: "tot", comp: "sql", unit: "tot", val: e => 1000 * e[1]},
    {key: "req", group: "req", comp: "", unit: "req", val: e => e[0]},
];
const SERIES_BY = Object.fromEntries(SERIES.map(s => [s.key, s]));
const UNIT_LABEL = {req: "requests", avg: "avg ms", tot: "total ms"};
const GROUP_LABEL_S = {avg: "average ms", tot: "total ms"};
const COMP_LABEL = {all: "", py: " · py", sql: " · sql"};
// an error / warning only has occurrences: no processing time of its own
const seriesAllowed = (it, key) => it.kind !== "issue" || key === "req";
const seriesName = (it, s) => s.key === "req" ? (it.kind === "issue" ? "occurrences" : "requests") : GROUP_LABEL_S[s.group] + COMP_LABEL[s.comp];
const unitLabel = (u, items) => u === "req" && items.length && items.every(i => i.kind === "issue") ? "occurrences" : UNIT_LABEL[u];
// one hue per item; a series is a shade of it, and lines are dashed by what they measure
const SHADE = {req: [0, 0], avg_all: [0, 16], avg_py: [34, 18], avg_sql: [-34, 8], tot_all: [0, -16], tot_py: [34, -8], tot_sql: [-34, -22]};
const seriesColor = (hue, key) => `hsl(${(hue + SHADE[key][0] + 360) % 360} 60% ${60 + SHADE[key][1]}%)`;
const DASH = {avg: "", tot: "7 4", req: "2 3"};
const ITEM_HUES = [190, 20, 100, 330, 45, 215, 150, 300, 0, 75, 240, 165];
const TOTAL_HUE = 270;
const defaultSeries = kind => ({bars: new Set(["req"]), lines: new Set(kind === "issue" ? [] : ["avg_all"])});

const pad2 = n => String(n).padStart(2, "0");
const fSize = m => m < 60 ? m + " min" : m < 1440 ? m / 60 + " h" : m === 1440 ? "1 day" : m / 1440 + " days";
const fTick = t => t >= 1e6 ? Number((t / 1e6).toPrecision(3)) + "M" : t >= 1e4 ? Number((t / 1e3).toPrecision(3)) + "k" : t >= 1000 ? nf0.format(t) : String(Number(t.toPrecision(3)));
const fSeriesVal = (s, v) => v === null ? "–" : s.unit === "req" ? nf0.format(v) : nf0.format(v) + " ms";
function niceMax(m) {
    const p = 10 ** Math.floor(Math.log10(m)), f = m / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
}

// the items the chart can plot: "total" first, then every search tag and pinned error
function histItems() {
    return [{id: "total", kind: "total", label: "total", cfg: state.totalSeries, hue: TOTAL_HUE}]
        .concat(state.histTags.map((t, i) => ({id: "t" + i, kind: "tag", tag: t, label: t.label, cfg: t, hue: t.hue})))
        .concat(state.issueTags.map((t, i) => ({id: "i" + i, kind: "issue", tag: t, label: t.label, cfg: t, hue: t.hue})));
}
const itemById = id => histItems().find(it => it.id === id);
const pickedCount = () => state.histTags.length + state.issueTags.length;
function nextHue() {
    const used = new Set([...state.histTags, ...state.issueTags].map(t => t.hue));
    return ITEM_HUES.find(h => !used.has(h)) ?? ITEM_HUES[(state.histTags.length + state.issueTags.length) % ITEM_HUES.length];
}
// pinned items the current data has nothing for, though they may well be in the selection (outside its top N)
function missingPins() {
    const ops = new Set(histOps(state.data.hist || {ops: []}).map(o => o.operation)), issues = view().issues;
    return state.histTags.some(t => t.by === "operation" && !ops.has(t.term)) ||
        state.issueTags.some(t => !issues.some(r => sameIssue(r, t)));
}

// "total" steps aside while something is picked, and comes back once nothing is
function itemsChanged(before) {
    const after = pickedCount();
    if (!before && after) state.totalSeries = {bars: new Set(), lines: new Set()};
    else if (before && !after) state.totalSeries = defaultSeries("total");
    if (state.focus) loadFocus();                                   // the focused timeline carries the pinned items' own (if they changed)
    if (state.data && missingPins()) analyzeNow();                   // a pinned item outside what the server sent
    renderSeriesBtn();
    renderFormSelect();
    if (state.data) { renderStats(); renderSelTable(); }            // the filter editor's summary counts the items
    if (state.data && !state.hidden.hist) renderHist();
    if (!$("#histSeriesMenu").hidden) renderHistSeriesMenu();
}

const histData = () => state.focus && state.focusHist ? state.focusHist : state.data.hist;
// the operation timelines: the top ones the server always sends, plus those of pinned operations outside them
function histOps(h) {
    const extra = h === state.data.hist ? (state.data.pinned_ops || []) : [];
    if (!extra.length) return h.ops;
    const have = new Set(h.ops.map(o => o.operation));
    return h.ops.concat(extra.filter(o => !have.has(o.operation)));
}
const focusSpanMin = () => state.focus ? Math.max(1, Math.ceil(state.focus.to / 60) - Math.floor(state.focus.from / 60)) : 0;
function histSizes() {
    const base = histData().base, ok = SIZES.filter(s => s >= base && s % base === 0);
    if (!state.focus) return ok;
    const fit = ok.filter(s => s < focusSpanMin());                  // focused: no bucket bigger than the span itself
    return fit.length ? fit : ok.slice(0, 1);
}

function autoSize() {
    const sizes = histSizes();
    if (state.focus) return sizes.find(s => focusSpanMin() / s <= 200) || sizes[sizes.length - 1];   // ~200 bars at most
    return sizes.find(s => s >= 10) || sizes[sizes.length - 1];      // default 10 min (or the finest the log allows)
}

function histSize() {
    if (state.histSize !== "auto" && histSizes().includes(+state.histSize)) return +state.histSize;
    return autoSize();
}

// a search tag matches either one exact operation / app / type (added by right-click) or a free-text
// substring across all three (added by typing + Enter)
const tagMatcher = t => t.by ? (r => groupName(r, t.by) === t.term)
    : (r => (r.family + " " + r.app + " " + r.operation).toLowerCase().includes(t.term));
const issueKeyOf = r => [r.level, r.logger, r.message];
const sameIssue = (a, b) => a.level === b.level && a.logger === b.logger && a.message === b.message;

function binHist(packs, size) {
    const map = new Map();
    let total = 0;
    for (const p of packs)
        for (const [m, n, s, py, u] of p) {
            const k = Math.floor(m / size) * size, e = map.get(k);
            total += n;
            if (e) { e[0] += n; e[1] += s; e[2] += py; e[3] += u; } else map.set(k, [n, s, py, u]);
        }
    return {map, total};
}

function histBuckets() {
    const h = histData(), size = histSize();
    const {map: allMap, total} = binHist([h.all], size);
    let k0 = null, k1 = null;
    if (state.focus) {                                                   // the focused span, empty buckets included
        k0 = Math.floor(state.focus.from / 60 / size) * size;
        k1 = Math.floor((Math.ceil(state.focus.to / 60) - 1) / size) * size;
    } else if (allMap.size) {
        const keys = [...allMap.keys()];
        k0 = Math.min(...keys);
        k1 = Math.max(...keys);
    }
    const keys = [];
    if (k0 !== null) for (let k = k0; k <= k1; k += size) keys.push(k);
    const align = map => keys.map(k => [k, map.get(k) || [0, 0, 0, 0]]);
    // an error's own timeline: the focused one when focused, else the current view's (looked up fresh
    // each render, so it never goes stale when the interval or the exclusions change)
    const issueRows = state.focus && state.focusHist ? state.focusHist.issues || [] : (state.data && view().issues) || [];
    const items = histItems().map(it => {
        let packs;
        if (it.kind === "total") packs = [h.all];
        else if (it.kind === "tag") packs = histOps(h).filter(r => tagMatcher(it.tag)(r)).map(r => r.hist);
        else { const row = issueRows.find(r => sameIssue(r, it.tag)); packs = row ? [row.hist] : []; }
        return Object.assign({}, it, {list: align(binHist(packs, size).map)});
    });
    return {keys, size, total, items};
}

// every series switched on, as {item, s, side, color, vals}
function histSeriesOn(items) {
    const out = [];
    for (const side of ["bars", "lines"])
        for (const it of items)
            for (const s of SERIES)
                if (it.cfg[side].has(s.key) && seriesAllowed(it, s.key))
                    out.push({item: it, s, side, color: seriesColor(it.hue, s.key), vals: it.list ? it.list.map(([, e]) => s.val(e)) : []});
    return out;
}

function renderHist() {
    const svg = $("#hist");
    const {keys, size, total, items} = histBuckets();
    $("#histCount").textContent = `${nf0.format(total)} requests` + (state.focus ? ` · focused on ${fDur(state.focus.to - state.focus.from)}` : "");

    const sel = $("#histSize");                                      // sizes depend on the loaded log (and the focus)
    const sizes = histSizes(), dflt = autoSize();
    sel.innerHTML = sizes.map(s => `<option value="${s}">${fSize(s)} buckets${s === dflt ? " (default)" : ""}</option>`).join("");
    sel.value = String(size);
    renderHistTools();
    if (!$("#histSeriesMenu").hidden) renderHistSeriesMenu();      // keep an open popup in sync

    const on = histSeriesOn(items), bars = on.filter(x => x.side === "bars"), lines = on.filter(x => x.side === "lines");
    renderHistKey(on);
    const H = 440, T = 26, B = 58, ph = H - T - B, COLW = 52;
    if (!keys.length || (!total && !on.some(x => x.vals.some(v => v > 0)))) {
        svg.setAttribute("viewBox", `0 0 1100 ${H}`);
        svg.style.width = "100%";
        svg.innerHTML = `<text x="550" y="${H / 2}" text-anchor="middle" class="dim">No matching requests</text>`;
        svg._info = null;
        return;
    }
    // one scale per unit and side: bars on the left, lines on the right, one tick column per unit
    const scales = side => {
        const units = [];
        for (const x of on) if (x.side === side && !units.includes(x.s.unit)) units.push(x.s.unit);
        return units.map(u => {
            const xs = on.filter(x => x.side === side && x.s.unit === u);
            let max = 0;
            for (const x of xs) for (const v of x.vals) if (v !== null && v > max) max = v;
            return {unit: u, top: max > 0 ? niceMax(max) : 1, items: xs.map(x => x.item)};      // nothing to show: a plain 0-1 scale
        });
    };
    const left = scales("bars"), right = scales("lines");
    const topOf = (side, u) => (side === "bars" ? left : right).find(sc => sc.unit === u).top;
    const L = left.length ? 12 + COLW * left.length : 24, R = right.length ? 12 + COLW * right.length : 24;
    const n = keys.length, k = Math.max(1, bars.length);

    // With many small buckets, a fixed-width chart squeezes bars down to nothing. Instead, once
    // the available width can no longer give each bucket a legible minimum, widen the SVG past
    // its container so .hist-scroll can scroll it horizontally, keeping every bar readable.
    const minGw = Math.max(14, k * 6);
    const availW = Math.max(300, svg.parentElement.clientWidth || 1100);
    const neededW = L + R + n * minGw;
    const scrollMode = !state.histFit && neededW > availW;             // "Fit all buckets" forces everything into view instead
    const W = scrollMode ? neededW : availW;                          // drawn at the panel's own width: no scaling, no side gaps
    const pw = W - L - R;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.style.width = scrollMode ? `${W}px` : "100%";
    const gw = pw / n;
    const bw = Math.min(gw * 0.82 / k, 42);

    const iv = state.interval;
    let out = "";
    for (let i = 0; i <= 4; i++) {
        const f = i / 4, y = T + ph * (1 - f);
        out += `<line x1="${L}" x2="${W - R}" y1="${y}" y2="${y}" stroke="#2a2e37"/>`;
        left.forEach((sc, j) => { out += `<text class="dim" x="${L - 8 - j * COLW}" y="${y + 4}" text-anchor="end">${fTick(sc.top * f)}</text>`; });
        right.forEach((sc, j) => { out += `<text class="dim" x="${W - R + 8 + j * COLW}" y="${y + 4}" text-anchor="start">${fTick(sc.top * f)}</text>`; });
    }
    left.forEach((sc, j) => { out += `<text class="dim unit" x="${L - 8 - j * COLW}" y="${T - 12}" text-anchor="end">${esc(unitLabel(sc.unit, sc.items))}</text>`; });
    right.forEach((sc, j) => { out += `<text class="dim unit" x="${W - R + 8 + j * COLW}" y="${T - 12}" text-anchor="start">${esc(unitLabel(sc.unit, sc.items))}</text>`; });
    keys.forEach((m, i) => {
        const x0 = L + i * gw;
        // buckets inside the selected interval -- not when it is the focused span itself, which would light up everything
        const sel_ = iv && !(state.focus && iv.from <= state.focus.from && iv.to >= state.focus.to) && m * 60 < iv.to && (m + size) * 60 > iv.from;
        out += `<rect class="hsel${sel_ ? " on" : ""}" x="${x0}" y="${T}" width="${gw}" height="${ph}"/>`;
        out += `<rect class="hcol" data-g="${i}" x="${x0}" y="${T}" width="${gw}" height="${ph}"/>`;
        bars.forEach((b, j) => {
            const c = b.vals[i];
            if (!(c > 0)) return;
            const h = Math.max(1.5, ph * c / topOf("bars", b.s.unit));
            out += `<rect class="bar" x="${x0 + (gw - bw * k) / 2 + j * bw}" y="${T + ph - h}" width="${Math.max(1, bw - 1)}" height="${h}" fill="${b.color}"/>`;
        });
    });

    const step = Math.max(1, Math.ceil(n / (pw / 92)));                // labels: log format, date over time
    for (let i = 0; i <= n; i += step) {
        const [d, tm] = fTsTz((keys[0] + i * size) * 60).split(" "), x = L + i * gw;
        out += `<line x1="${x}" x2="${x}" y1="${T + ph}" y2="${T + ph + 4}" stroke="#3a3e46"/>` +
               `<text class="dim" x="${x}" y="${T + ph + 17}" text-anchor="middle">${d}</text>` +
               `<text class="dim" x="${x}" y="${T + ph + 30}" text-anchor="middle">${tm}</text>`;
    }
    out += `<line x1="${L}" x2="${W - R}" y1="${T + ph}" y2="${T + ph}" stroke="#3a3e46"/>` +
           `<text class="dim" x="${L + pw / 2}" y="${H - 8}" text-anchor="middle">time of request${state.tz !== "UTC" ? ` (${state.tz})` : ""} · ${fSize(size)} buckets${state.focus ? " · focused" : ""}</text>`;

    // lines go last so they draw over the bars; a bucket with no value breaks a line into separate
    // segments instead of faking a value for it.
    for (const l of lines) {
        const top = topOf("lines", l.s.unit), dash = DASH[l.s.group] ? ` stroke-dasharray="${DASH[l.s.group]}"` : "";
        let seg = [];
        const flush = () => {
            if (seg.length > 1) out += `<polyline class="avgline" stroke="${l.color}"${dash} points="${seg.map(p => p.join(",")).join(" ")}"/>`;
            else if (seg.length === 1) {                      // an isolated bucket: a short tick, nothing to connect it to
                const [x, y] = seg[0], d = Math.max(3, gw * 0.3);
                out += `<line class="avgline" stroke="${l.color}" x1="${x - d}" y1="${y}" x2="${x + d}" y2="${y}"/>`;
            }
            seg = [];
        };
        l.vals.forEach((v, i) => v === null ? flush() : seg.push([L + i * gw + gw / 2, T + ph * (1 - v / top)]));
        flush();
    }
    svg.innerHTML = out;
    svg._info = {keys, size, items, on};
    svg._last = null;
    if (state.anchor) paintPreview(null);                  // a picked start bar stays marked through a redraw
}

// a small picture of one series, as drawn: a bar swatch, or a line with its dash
const seriesIcon = x => x.side === "bars"
    ? `<svg class="kicon" width="12" height="12" viewBox="0 0 12 12"><rect x="1" y="1" width="10" height="10" rx="1.5" fill="${x.color}"/></svg>`
    : `<svg class="kicon" width="20" height="12" viewBox="0 0 20 12"><line x1="1" y1="6" x2="19" y2="6" stroke="${x.color}" stroke-width="2.2"${DASH[x.s.group] ? ` stroke-dasharray="${DASH[x.s.group]}"` : ""}/></svg>`;

// the item filters behind one chart item, for its key entry; "total" gets those of items that are not plotted
function keyFilters(it, items) {
    const fmt = f => Object.entries(f.facts).map(([k, v]) => `${k}: ${v.map(shortVal).join(", ")}`).join(" · ");
    const own = it => it.kind === "issue" ? ffFind("issue", issueKeyOf(it.tag)) : it.kind === "tag" && it.tag.by ? ffFind(it.tag.by, it.tag.term) : null;
    if (it.kind !== "total") { const f = own(it); return f ? fmt(f) : ""; }
    const shown = new Set(items.map(own).filter(Boolean));
    return state.factFilters.filter(f => !shown.has(f)).map(f => `${shortVal(ffLabel(f))} → ${fmt(f)}`).join("; ");
}

function renderHistKey(on) {
    const items = [...new Set(on.map(x => x.item))];
    // filters on items that are not plotted belong to "total"; with total not plotted either, they stand alone
    const loose = items.some(it => it.kind === "total") ? "" : keyFilters({kind: "total"}, items);
    $("#histKey").innerHTML = (loose ? `<span class="key-item key-ff" title="${esc(loose)}">(${esc(loose)})</span>` : "") + (on.length
        ? on.map(x => {
            const f = keyFilters(x.item, items), label = x.item.label.length > 50 ? x.item.label.slice(0, 49) + "…" : x.item.label;
            return `<span class="key-item" title="${esc(x.item.label + (f ? " — filtered: " + f : ""))}">${seriesIcon(x)}<span class="key-name">${esc(label)}</span> ` +
                `<span class="sub">(${esc(seriesName(x.item, x.s))})</span>${f ? ` <span class="key-ff">(${esc(f)})</span>` : ""}</span>`;
        }).join("")
        : `<span class="sub">Nothing is plotted: switch series on in <b>Chart items &amp; series</b>.</span>`);
}

function renderSeriesBtn() {
    const n = pickedCount();
    $("#histSeriesBtn").textContent = `Chart items & series${n ? ` (${n + 1})` : ""} ▾`;
}

// the buttons whose text follows the state: fit / scroll, focus / clear focus
function renderHistTools() {
    const fit = $("#histFit");
    fit.textContent = state.histFit ? "Scroll buckets" : "Fit all buckets";
    fit.dataset.tip = state.histFit
        ? "All buckets are squeezed into the panel width. Click to keep every bar readable instead, scrolling sideways when there are too many."
        : "Buckets keep a readable width and the chart scrolls sideways. Click to squeeze every bucket into the panel width instead.";
    const f = $("#histFocus"), iv = state.interval, imported = state.data && !state.data.window;
    f.textContent = state.focus ? "Clear focus" : "Focus interval";
    f.disabled = !state.focus && (!iv || imported);
    f.classList.toggle("accent", !!state.focus);
    f.dataset.tip = state.focus
        ? "Show the whole timeline again. The time interval stays selected."
        : iv ? "Show only the selected time interval on the chart, cut out of the timeline (not squeezed), with finer bucket sizes: down to 1 minute however long the log is."
             : "Select a time interval first (click one bar, or two for a range), then show only that part of the timeline, with finer bucket sizes.";
    $("#histFocusSel").hidden = !(state.focus && iv && (iv.from !== state.focus.from || iv.to !== state.focus.to));
}

// ---- focus: the chart shows only one interval, fetched at its own finer resolution ------------------------
let focusSeq = 0;
function focusKey() {
    return [state.data && state.data.hist_key, state.focus && state.focus.from, state.focus && state.focus.to,
        JSON.stringify(state.issueTags.map(issueKeyOf)), state.histTags.filter(t => t.by === "operation").map(t => t.term).join("\x1f")].join("|");
}

async function loadFocus() {
    if (!state.focus || !state.data) return;
    const key = focusKey();
    if (state.focusHist && state.focusHist._key === key) return;
    const seq = ++focusSeq;
    const p = selectionParams();
    p.set("from", state.focus.from);
    p.set("to", state.focus.to);
    if (state.issueTags.length) p.set("issues", JSON.stringify(state.issueTags.map(issueKeyOf)));
    state.histTags.filter(t => t.by === "operation").forEach(t => p.append("pin_op", t.term));
    busy("focus", "Loading the focused histogram…");
    try {
        const h = await api("/api/timeline?" + p);
        if (seq !== focusSeq) return;
        h._key = key;
        state.focusHist = h;
    } catch (e) {
        if (seq !== focusSeq) return;
        state.focus = state.focusHist = null;
        setNotice("Focus cleared: " + e.message, true);
    } finally {
        if (seq === focusSeq) busy("focus");
    }
    if (state.data && !state.hidden.hist) renderHist();
}

let resizeTimer = null;
addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (state.data && !state.hidden.hist) renderHist(); }, 150); });

function setFocus(iv) {
    if (!iv || !state.data || !state.data.window) return;
    state.focus = {from: iv.from, to: iv.to};
    state.focusHist = null;                         // until it arrives, the coarse timeline is shown cut to the span
    state.histSize = "auto";
    if (!state.hidden.hist) renderHist();
    renderStats();
    loadFocus();
}

function clearFocus() {
    focusSeq++;
    busy("focus");
    state.focus = state.focusHist = null;
    state.histSize = "auto";
    if (state.data) { renderHist(); renderStats(); }
}

const hist = $("#hist");
// After the first bar is picked (it only marks where the range starts), the highlight follows the mouse: the
// range from that bar to the hovered one; with the mouse elsewhere, just the picked bar.
function paintPreview(hi) {
    const info = hist._info, a = state.anchor;
    let from = 0, to = 0;
    if (a && info) {
        const m = hi === null ? null : info.keys[hi];
        from = m === null ? a.from : Math.min(a.from, m * 60);
        to = m === null ? a.to : Math.max(a.to, (m + info.size) * 60);
    }
    hist.querySelectorAll(".hsel").forEach((r, i) => {
        const on = to > 0 && info.keys[i] * 60 < to && (info.keys[i] + info.size) * 60 > from;
        r.classList.toggle("pv", on);
    });
}

hist.addEventListener("mousemove", e => {
    const el = e.target.closest("[data-g]"), info = hist._info;
    const gi = el && info ? +el.dataset.g : null;
    if (hist._last !== gi) {                                     // only when the pointer moves to another bar
        hist._last = gi;
        hist.querySelectorAll(".hcol.hot").forEach(c => c.classList.remove("hot"));
        if (gi !== null) el.classList.add("hot");
        paintPreview(state.anchor ? gi : null);
    }
    if (gi === null) return hideTip();
    const m = info.keys[gi], tot = info.items[0].list[gi][1];
    let h = `<b>${fTsTz(m * 60)}</b> → ${fTsTz((m + info.size) * 60)}<br>Requests: <b>${nf0.format(tot[0])}</b>`;
    for (const x of info.on) {
        const label = x.item.label.length > 40 ? x.item.label.slice(0, 39) + "…" : x.item.label;
        h += `<br>${seriesIcon(x)} ${esc(label)} <span style="color:var(--dim)">(${esc(seriesName(x.item, x.s))})</span>: <b>${fSeriesVal(x.s, x.vals[gi])}</b>`;
    }
    h += `<br><span style="color:var(--dim)">${state.anchor ? "click to select the range up to this bar (this bar alone if it is the same one) · Esc to cancel" : "click to start a range here · a second click selects it"}</span>`;
    showTip(h, e.clientX, e.clientY);
});
hist.addEventListener("mouseleave", () => {
    hist.querySelectorAll(".hcol.hot").forEach(c => c.classList.remove("hot"));
    hist._last = null;
    paintPreview(null);
    hideTip();
});
hist.addEventListener("click", e => {
    const el = e.target.closest("[data-g]"), info = hist._info;
    if (!el || !info || !state.data.window) return;      // an imported summary has no raw data left to re-slice
    const m = info.keys[+el.dataset.g];
    const bar = {from: m * 60, to: (m + info.size) * 60};
    const a = state.anchor;
    if (a) {                                                     // second click: the range between the two bars, edges included
        state.anchor = null;                                     // (the same bar twice: that bar alone)
        return setInterval_(Math.min(a.from, bar.from), Math.max(a.to, bar.to));
    }
    state.anchor = bar;                                          // first click: only marks where the range starts
    paintPreview(+el.dataset.g);
    hideTip();
});

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
        (T.shown < T.rows.length ? ` · showing ${nf0.format(T.shown)}, scroll for more` : "");
}

// bodyOnly: only the rows changed (search box); otherwise header, rows and totals are rebuilt
function renderOpTable(T, bodyOnly) {
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
        const kind = issue ? (it.tag.level === "WARNING" ? "warning" : "error") : it.tag.by ? GROUP_LABEL[it.tag.by] : "search";
        const label = issue ? it.tag.message : it.tag.label;
        const name = `<td class="op l"><span class="sw-item" style="background:${seriesColor(it.hue, "req")}"></span>` +
            `<span class="badge b-mixed selkind">${esc(kind)}</span> ${esc(label)}${issue ? ffBadge("issue", issueKeyOf(it.tag)) : it.tag.by ? ffBadge(it.tag.by, it.tag.term) : ""}</td>`;
        const attrs = `data-sel-item="${it.id}"`;
        if (!r) return `<tr ${attrs}>${name}<td class="empty-cell" colspan="9">not in the current selection</td></tr>`;
        if (issue) return `<tr ${attrs}>${name}<td style="color:${TONE.calls}">${nf0.format(r.n)}</td>${dash.repeat(8)}</tr>`;
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
        $(T.tbl + " tbody").insertAdjacentHTML("beforeend", T.rows.slice(T.shown, next).map(r => rowHtml(r, T.by, cols, T.denom)).join(""));
        T.shown = next;
        setTableCount(T);
    });
}

// ---- chart items: typed text (Enter), a right-clicked table row, or a right-clicked error -----------------------
const isPicked = (by, name) => state.histTags.some(t => t.by === by && t.term === name);

function markPicked() {
    document.querySelectorAll("#tbl tbody tr").forEach(tr => tr.classList.toggle("picked", isPicked(state.tableGroup, tr.dataset.name)));
}

function addHistTag(tag) {
    if (!tag.term || state.histTags.some(t => t.by === tag.by && t.term === tag.term)) return;
    const before = pickedCount();
    state.histTags.push(Object.assign(tag, defaultSeries("tag"), {hue: nextHue()}));
    markPicked();
    itemsChanged(before);
    if (state.data && !state.hidden.table) renderOpTable(TABLES.main);   // the table's "Total · <selection>" row follows the tags too
}

function removeHistTag(i) {
    const before = pickedCount();
    state.histTags.splice(i, 1);
    markPicked();
    itemsChanged(before);
    if (state.data && !state.hidden.table) renderOpTable(TABLES.main);
}

// ---- errors / warnings pinned to the chart, each with its own occurrence timeline -----------------
const isIssuePicked = r => state.issueTags.some(t => sameIssue(t, r));

function addIssueTag(r) {
    if (isIssuePicked(r)) return;
    const before = pickedCount();
    const label = r.message.length > 40 ? r.message.slice(0, 39) + "…" : r.message;
    state.issueTags.push(Object.assign({level: r.level, logger: r.logger, message: r.message, label}, defaultSeries("issue"), {hue: nextHue()}));
    itemsChanged(before);
    if (state.data && !state.hidden.issues) renderIssues();
}

function removeIssueTag(i) {
    const before = pickedCount();
    state.issueTags.splice(i, 1);
    itemsChanged(before);
    if (state.data && !state.hidden.issues) renderIssues();
}

function removeItem(id) {
    if (id[0] === "t") return removeHistTag(+id.slice(1));
    if (id[0] === "i") return removeIssueTag(+id.slice(1));
}

function removeAllItems() {
    const before = pickedCount();
    state.histTags = [];
    state.issueTags = [];
    markPicked();
    itemsChanged(before);
    if (state.data && !state.hidden.table) renderTable();
    if (state.data && !state.hidden.issues) renderIssues();
}

// ---- "Chart items & series": one row per item, one checkbox per series it can plot as bars or lines ---------
const ITEM_KIND = {total: "everything selected", tag: "", issue: "error / warning"};
function itemKindLabel(it) {
    if (it.kind !== "tag") return ITEM_KIND[it.kind];
    return it.tag.by ? GROUP_LABEL[it.tag.by] : "text search";
}

function renderHistSeriesMenu() {
    const items = histItems();
    const order = ["avg_all", "avg_py", "avg_sql", "tot_all", "tot_py", "tot_sql", "req"];
    const sideHead = `<th colspan="3" class="grp">average ms</th><th colspan="3" class="grp">total ms</th><th rowspan="2" class="grp">requests</th>`;
    const compHead = `<th>all</th><th>py</th><th>sql</th><th>all</th><th>py</th><th>sql</th>`;
    const cell = (it, side, key) => {
        if (!seriesAllowed(it, key)) return `<td class="na" data-tip="An error or warning only has occurrences, no processing time.">–</td>`;
        const on = it.cfg[side].has(key), color = seriesColor(it.hue, key);
        return `<td class="cbcell${on ? " on" : ""}" style="--c:${color}"><label class="cb"><input type="checkbox" data-item="${it.id}" data-side="${side}" data-s="${key}"${on ? " checked" : ""}>` +
            `<i class="sw${side === "lines" ? " ln" : ""}" style="${side === "lines" ? `border-top-color:${color};border-top-style:${DASH[SERIES_BY[key].group] ? (key === "req" ? "dotted" : "dashed") : "solid"}` : `background:${color}`}"></i></label></td>`;
    };
    const rows = items.map(it =>
        `<tr><td class="l item"><span class="sw-item" style="background:${seriesColor(it.hue, "req")}"></span>` +
        `<span class="iname" title="${esc(it.label)}">${esc(it.label)}</span> <span class="sub">${esc(itemKindLabel(it))}</span></td>` +
        order.map(k => cell(it, "bars", k)).join("") + order.map(k => cell(it, "lines", k)).join("") +
        `<td>${it.kind === "total" ? "" : `<button class="btn ghost rm" data-rm-item="${it.id}" title="Remove this item from the chart">Remove</button>`}</td></tr>`).join("");
    $("#histItemsTbl").innerHTML =
        `<table class="sertbl static"><thead>` +
        `<tr><th rowspan="3" class="l">item</th><th colspan="7" class="side">Bars</th><th colspan="7" class="side">Lines</th><th rowspan="3"></th></tr>` +
        `<tr>${sideHead}${sideHead}</tr><tr>${compHead}${compHead}</tr></thead><tbody>${rows}</tbody></table>`;
    $("#histRmAll").disabled = !pickedCount();
}

// a click on a table row opens its details; on a selected item, its own details
document.addEventListener("click", e => {
    if (getSelection().toString()) return;
    const tr = e.target.closest("#tbl tbody tr[data-name]");
    if (tr) return openDetail(state.tableGroup, tr.dataset.name);
    const sel = e.target.closest("#selTbl tbody tr[data-sel-item]"), it = sel && itemById(sel.dataset.selItem);
    if (!it) return;
    if (it.kind === "issue") return openIssue(it.tag);
    if (it.tag.by) return openDetail(it.tag.by, it.tag.term);
    state.filter = it.tag.label;                                        // a search: show what it matches in the main table
    $("#filter").value = state.filter;
    renderOpTable(TABLES.main, true);
    $("#tablePanel").scrollIntoView({behavior: "smooth", block: "start"});
});
document.addEventListener("contextmenu", e => {                          // right-click a selected item: deselect it
    const sel = e.target.closest("#selTbl tbody tr[data-sel-item]");
    if (!sel) return;
    e.preventDefault();
    removeItem(sel.dataset.selItem);
});

/* ---- raw log lines of the open popup (a warning / error, or an operation / app / type) ------------ */
// The server keeps only counts, so the lines themselves are re-found in the loaded files on demand: a list of
// occurrences to pick from, and the raw lines around the picked one.
const MAX_CTX = 200;
// lines of context on each side: kept while this server runs, back to 3 when it restarts
const CTX_KEY = "issueContext." + (document.body.dataset.boot || "");
try { Object.keys(localStorage).filter(k => k.startsWith("logtool.issueContext") && k !== "logtool." + CTX_KEY).forEach(k => localStorage.removeItem(k)); } catch (e) { /* private mode */ }
let issueContext = Math.max(0, Math.min(MAX_CTX, store.get(CTX_KEY, 3)));
const raw = {seq: 0, ctxSeq: 0, occ: null, sel: 0, total: 0, url: "", timed: false, order: store.get("rawOrder", "time")};

function rawSection(example) {
    return (example ? `<div class="pane-card"><div class="pane-head"><b>Example, as logged</b><span class="grow"></span>` +
                      `<button class="btn ghost" data-copy="${esc(example)}">Copy</button></div><pre class="full">${esc(example)}</pre></div>` : "") +
        `<div id="rawOcc"><div class="pane-card"><div class="sub loading">Finding occurrences…</div></div></div>`;
}

// url: the /api/*_lines request without the order; total: how many occurrences the row counts;
// timed: the occurrences have a duration (operations), so they can also be listed slowest first
// ident: what the occurrences are ({by, name} or {level, logger, msg}), so the log view can color the lines around one
function loadRawLines(url, total, timed, ident) {
    const seq = ++raw.seq;
    Object.assign(raw, {occ: null, sel: 0, total, url, timed, ident: ident || raw.ident});
    api(url + (timed && raw.order === "slow" ? "&order=slow" : "")).then(occ => {
        if (seq !== raw.seq) return;
        raw.occ = occ;
        renderRawOcc();
    }).catch(e => {
        if (seq !== raw.seq) return;
        const box = $("#rawOcc");
        if (box) box.innerHTML = `<div class="pane-card"><div class="sub err">Could not find occurrences: ${esc(e.message)}</div></div>`;
    });
}

// the occurrences (left) and the log around the picked one (right) -- rebuilt once when the list loads; picking
// another occurrence, or changing the context size, only refreshes the context box itself (see renderRawCtx)
function renderRawOcc() {
    const box = $("#rawOcc");
    if (!box || !raw.occ) return;
    const occ = raw.occ, slow = raw.timed && raw.order === "slow";
    const manyFiles = new Set(occ.map(o => o.file)).size > 1, ops = occ.some(o => o.operation), secs = occ.some(o => o.seconds != null);
    const order = raw.timed
        ? `<select id="rawOrder" aria-label="Order"><option value="time"${slow ? "" : " selected"}>in log order</option><option value="slow"${slow ? " selected" : ""}>slowest first</option></select>` : "";
    if (!occ.length) {
        box.innerHTML = `<div class="pane-card"><div class="pane-head"><b>Occurrences</b><span class="grow"></span>${order}</div>` +
            `<div class="sub">No occurrence could be re-found in the currently loaded files (they may have changed since, or the line was too long to match exactly).</div></div>`;
        return;
    }
    const rows = occ.map((o, idx) => `<tr class="link${idx === raw.sel ? " picked" : ""}" data-occ-i="${idx}"><td class="l">${esc(fStamp(o.stamp))}</td>` +
        (ops ? `<td class="l">${esc(o.operation || "")}</td>` : "") + (manyFiles ? `<td class="l dim">${esc(o.file_name)}</td>` : "") +
        (secs ? `<td>${o.seconds != null ? fSec(o.seconds) : "–"}</td>` : "") + `</tr>`).join("");
    box.innerHTML = `<div class="rawgrid">` +
        `<div class="pane-card rawlist"><div class="pane-head"><b>Occurrences</b><span class="sub">${raw.total > occ.length
            ? `the ${slow ? "slowest" : "first"} ${nf0.format(occ.length)} of ${nf0.format(raw.total)}` : nf0.format(occ.length)} · click one to see the log around it</span>` +
        `<span class="grow"></span>${order}</div>` +
        `<div class="table-wrap occwrap"><table class="static occtbl"><thead><tr><th class="l">time</th>${ops ? `<th class="l">operation</th>` : ""}` +
        `${manyFiles ? `<th class="l">file</th>` : ""}${secs ? `<th>seconds</th>` : ""}</tr></thead><tbody>${rows}</tbody></table></div></div>` +
        `<div class="pane-card rawctx"><div class="pane-head"><b>Log around it</b><span class="grow"></span>` +
        `<label class="sub">context lines <input type="number" id="ctxN" min="0" max="${MAX_CTX}" value="${issueContext}"></label>` +
        `<button class="btn ghost" id="ctxCopy">Copy</button></div><div id="rawCtx"></div></div></div>`;
    renderRawCtx();
}

// what a line around the picked occurrence is to it (see mark_context in profiler.py), in color order
const LOG_MARKS = [
    ["main", "this occurrence", "The occurrence you picked, with its traceback / continuation lines."],
    ["same", "same item", "Other occurrences of the same operation / app / type, or of the same warning."],
    ["parent", "parent", "The request or cron run this line was logged during: the next one its worker ended whose run covers it."],
    ["during", "logged during it", "What the same worker logged while this request / job ran: its ORM calls, messages, warnings."],
];

function renderRawCtx() {
    const box = $("#rawCtx");
    const o = raw.occ && raw.occ[raw.sel];
    if (!box || !o) return;
    const seq = ++raw.ctxSeq;
    box.innerHTML = `<div class="sub loading">Loading…</div>`;
    const ident = new URLSearchParams(raw.ident || {});
    api(`/api/issue_context?file=${encodeURIComponent(o.file)}&line=${o.line}&before=${issueContext}&after=${issueContext}&${ident}`).then(d => {
        if (seq !== raw.ctxSeq) return;
        raw.ctxText = d.lines.join("\n");
        const first = o.line - d.match_at, marks = d.marks || d.lines.map((_, i) => i === d.match_at ? "main" : null);
        const present = LOG_MARKS.filter(([k]) => marks.includes(k));
        box.innerHTML = `<div class="loglegend">` + present.map(([k, label, tip]) =>
            `<span class="lg lg-${k}" data-tip="${esc(tip)}"><i></i>${label} <span class="sub">${nf0.format(marks.filter(m => m === k).length)}</span></span>`).join("") + `</div>` +
            `<div class="logview">` + d.lines.map((l, idx) =>
            `<div class="ll${marks[idx] ? " m-" + marks[idx] : ""}${idx === d.match_at ? " hit" : ""}"><span class="ln">${first + idx}</span><span class="lt">${esc(l) || " "}</span></div>`).join("") + `</div>`;
        const hit = box.querySelector(".ll.hit");
        if (hit) box.querySelector(".logview").scrollTop = Math.max(0, hit.offsetTop - 60);
    }).catch(e => {
        if (seq !== raw.ctxSeq) return;
        box.innerHTML = `<div class="sub err">Could not load the lines: ${esc(e.message)}</div>`;
    });
}

/* ---- full-screen form of one row (operation / app / type, or a warning / error), in tabs ---------- */
let form = null;                 // what is open: {kind: "detail", by, name} | {kind: "issue", key: [level, logger, message]}
let formTab = "details";         // the open tab, kept while moving from one form to another when the next one has it
let formStack = [];              // forms opened before this one (Back returns to them): [{...form, tab}]
let restoringForm = false;       // re-opening a form (Back, or new data after a filter change): not a new visit
let detailSeq = 0;

const formOwner = () => !form ? null : form.kind === "issue" ? {by: "issue", name: form.key} : {by: form.by, name: form.name};

// sheet: the row's own numbers, always shown above the tabs (like an Odoo form's sheet)
// one icon per kind of row, in the form's title
const FORM_ICON = {
    operation: `<path d="M13 2L4 14h7l-1 8 9-12h-7z" fill="none" stroke="#6ea0ff" stroke-width="2" stroke-linejoin="round"/>`,
    app: `<g fill="none" stroke="#b083e8" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></g>`,
    family: `<g fill="none" stroke="#6ec1c9" stroke-width="2" stroke-linejoin="round"><path d="M3 12V4a1 1 0 0 1 1-1h8l9 9-9 9z"/><circle cx="8" cy="8" r="1.6"/></g>`,
    WARNING: `<g fill="none" stroke="#e6c15a" stroke-width="2" stroke-linejoin="round"><path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18v.5" stroke-linecap="round"/></g>`,
    ERROR: `<g fill="none" stroke="#ef5350" stroke-width="2"><circle cx="12" cy="12" r="9.5"/><path d="M8.5 8.5l7 7M15.5 8.5l-7 7" stroke-linecap="round"/></g>`,
};
const formIcon = kind => `<svg class="ficon" width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">${FORM_ICON[kind] || FORM_ICON.ERROR}</svg>`;

function showForm(title, sheet, tabs, icon) {
    $("#mTitle").innerHTML = formIcon(icon) + `<span>${esc(title)}</span>`;
    document.documentElement.classList.add("modal-open");      // the page behind must not scroll (or show its scrollbar)
    $("#mSheet").innerHTML = sheet;
    if (!tabs.some(t => t.id === formTab)) formTab = tabs[0].id;
    $("#mTabs").innerHTML = tabs.map(t => `<button class="tab${t.id === formTab ? " on" : ""}" data-tab="${t.id}">${esc(t.label)}</button>`).join("");
    $("#mBody").innerHTML = tabs.map(t => `<section class="tabpane" data-pane="${t.id}"${t.id === formTab ? "" : " hidden"}>${t.html}</section>`).join("");
    renderFormFilters();
    $("#modal").hidden = false;
}

function setTab(id) {
    formTab = id;
    document.querySelectorAll("#mTabs .tab").forEach(b => b.classList.toggle("on", b.dataset.tab === id));
    document.querySelectorAll("#mBody .tabpane").forEach(p => { p.hidden = p.dataset.pane !== id; });
    const sc = $("#mScroll"), tabsTop = $("#mTabs").offsetTop;
    if (sc.scrollTop > tabsTop) sc.scrollTop = tabsTop;          // the new tab starts at its top, the sheet stays scrolled away
}

function setTabLabel(id, label) {
    const b = $(`#mTabs [data-tab="${id}"]`);
    if (b) b.textContent = label;
}

function pushForm() {
    if (!$("#modal").hidden && form && !restoringForm) formStack.push(Object.assign({}, form, {tab: formTab}));
}

// true when the form could open (its row, or a filter on it, is still there)
function reopen(f) {
    restoringForm = true;
    try { return f.kind === "issue" ? openIssue({level: f.key[0], logger: f.key[1], message: f.key[2]}) : openDetail(f.by, f.name); }
    finally { restoringForm = false; }
}

function closeModal(all) {
    while (!all && formStack.length) {                  // back to the previous form instead of closing
        const prev = formStack.pop(), tab = formTab;
        formTab = prev.tab;
        if (reopen(prev)) return;
        formTab = tab;                                  // gone from the selection: try the one before it
    }
    detailSeq++;
    raw.seq++;
    formStack = [];
    form = null;
    $("#modal").hidden = true;
    document.documentElement.classList.remove("modal-open");
}
document.addEventListener("keydown", e => {
    if (e.key !== "Escape") return;
    if (state.anchor && $("#modal").hidden) { state.anchor = null; return paintPreview(null); }   // a half-picked range
    if (!$("#domModal").hidden) { $("#domModal").hidden = true; return; }
    if (!$("#ffModal").hidden) { $("#ffModal").hidden = true; return; }
    if (!$("#histSeriesMenu").hidden) { $("#histSeriesMenu").hidden = true; return; }
    if (!$("#modal").hidden) closeModal();
});

/* ---- fact filters: values picked in the details behind a row ------------------------------------ */
// state.factFilters: [{by: operation | app | family | issue, name (an issue's is [level, logger, message]),
// facts: {detail: [values]}}]. Sent to the server with every request (see selectionParams), which keeps only
// the occurrences carrying one of the picked values of each detail -- so they follow into every chart,
// table and statistic, not only into the form they were picked in.
const FACT_OTHER = "(other values)";
const ISSUE_UNFILTERABLE = ["logged user id", "logged records"];      // a warning has no lines of its own
const ffKey = (by, name) => by + "\x1f" + (Array.isArray(name) ? name.join("\x1f") : name);
const ffFind = (by, name) => state.factFilters.find(f => ffKey(f.by, f.name) === ffKey(by, name));
const ffCount = () => state.factFilters.reduce((n, f) => n + Object.values(f.facts).reduce((m, v) => m + v.length, 0), 0);
const shortVal = v => v.length > 60 ? v.slice(0, 59) + "…" : v;
const ffLabel = f => f.by === "issue" ? shortVal(f.name[2]) : f.name;

function toggleFact(by, name, slot, value) {
    let f = ffFind(by, name);
    if (!f) state.factFilters.push(f = {by, name, facts: {}});
    const vals = f.facts[slot] || (f.facts[slot] = []);
    const i = vals.indexOf(value);
    if (i >= 0) vals.splice(i, 1); else vals.push(value);
    if (!vals.length) delete f.facts[slot];
    if (!Object.keys(f.facts).length) state.factFilters.splice(state.factFilters.indexOf(f), 1);
    factsChanged();
}

function clearFacts(by, name) {                         // one row's filters, or every one (by = null)
    state.factFilters = by ? state.factFilters.filter(f => ffKey(f.by, f.name) !== ffKey(by, name)) : [];
    factsChanged();
}

async function factsChanged() {
    renderFfButton();
    if (state.data && !state.hidden.hist) renderHistKey(histSeriesOn(histBuckets().items));
    renderFormFilters();
    markFactRows();                                     // instant feedback; the numbers follow
    busy("ff", "Applying the item filters…");
    try { await analyzeNow(); } finally { busy("ff"); }              // (reopens the form with the new numbers)
}

function renderFfButton() {
    const n = ffCount(), b = $("#clearFacts");
    b.hidden = !n || !!(state.data && state.data.imported);
    b.textContent = `${n} item filter${n === 1 ? "" : "s"}`;
    b.dataset.tip = "Values picked in an item's details. They apply to the histogram, the tables and every statistic. Click to see and clear them.";
    if (!n) $("#ffModal").hidden = true;
    else if (!$("#ffModal").hidden) renderFfTable();
}

const ffKind = f => f.by === "issue" ? (f.name[0] === "WARNING" ? "warning" : "error") : GROUP_LABEL[f.by];

// the popup behind that button: one row per item and detail, each value removable on its own
function renderFfTable() {
    const rows = state.factFilters.flatMap((f, i) => Object.entries(f.facts).map(([slot, vals]) =>
        `<tr><td class="l"><span class="badge b-mixed selkind">${esc(ffKind(f))}</span> <a class="fflink" data-ffi="${i}">${esc(ffLabel(f))}</a></td>` +
        `<td class="l">${esc(slot)}</td><td class="l">` + vals.map(v =>
            `<button class="tag-chip" data-ffi="${i}" data-ff-del="${esc(slot)}" data-v="${esc(v)}" title="${esc(v)}">${esc(shortVal(v))} <span class="tag-x">×</span></button>`).join(" ") +
        `</td><td><button class="btn ghost" data-ffi="${i}" data-ff-row="${esc(slot)}">Clear</button></td></tr>`)).join("");
    $("#ffTbl").innerHTML = `<thead><tr><th class="l">item</th><th class="l">detail</th><th class="l">values (any of them)</th><th></th></tr></thead>` +
        `<tbody>${rows || `<tr><td class="empty" colspan="4">No item filters.</td></tr>`}</tbody>`;
}

function openFfModal() {
    renderFfTable();
    $("#ffModal").hidden = false;
}

// a small "filtered" mark on a table row whose details are filtered
function ffBadge(by, name) {
    const f = ffFind(by, name);
    if (!f) return "";
    const txt = Object.entries(f.facts).map(([k, v]) => `${esc(k)}: ${v.map(x => esc(shortVal(x))).join(", ")}`).join("<br>");
    return ` <span class="ffmark" data-tip="${esc("<b>Filtered to</b><br>" + txt + '<span class="scale">Open it to change or clear the filter.</span>')}">filtered</span>`;
}

function renderFormFilters() {
    const box = $("#mFilters"), o = formOwner(), f = o && ffFind(o.by, o.name);
    if (!f) { box.hidden = true; box.innerHTML = ""; return; }
    box.hidden = false;
    const chips = Object.entries(f.facts).flatMap(([slot, vals]) => vals.map(v =>
        `<button class="tag-chip" data-ff-rm="${esc(slot)}" data-v="${esc(v)}" data-tip="${esc(esc(slot) + ": " + esc(v) + " -- click to remove")}">` +
        `<span class="sub">${esc(slot)}</span> ${esc(shortVal(v))} <span class="tag-x">×</span></button>`));
    box.innerHTML = `<span class="k">Filtered to</span>${chips.join("")}<button class="btn ghost" id="ffClearForm">Clear all</button>` +
        `<span class="sub">several values of one detail: any of them · different details: all of them · applies to the histogram, the tables and every statistic</span>`;
}

function markFactRows() {
    const o = formOwner(), f = o && ffFind(o.by, o.name);
    document.querySelectorAll("#mBody tr[data-fact]").forEach(tr => {
        const on = !!(f && (f.facts[tr.dataset.fact] || []).includes(tr.dataset.v));
        tr.classList.toggle("picked", on);
    });
}

/* ---- what grouping collapsed: ids, users, records, clients ---------------------------------------- */
const FACT_CHUNK = 15;
let factShown = {};              // detail -> rows shown, reset per form
let lastFacts = null;            // {facts, owner} of the open form, so "Load more" can re-render one table

// how much of its parent item one group is: its count over the parent's calls / occurrences, and the time
// the parent spans -- 40% over 5 m is a pattern, 1% over 3 d is noise. sig: {base, unit, span} or null.
function sigText(n, sig, unit) {
    if (!sig || !sig.base) return "";
    return `${fPct(100 * n / sig.base)}${unit ? ` <span class="sub">of ${sig.unit}</span>` : ""}${sig.span ? ` <span class="sub">over ${fSpan(sig.span)}</span>` : ""}`;
}
const sigTip = sig => !sig || !sig.base ? "" : `Count divided by the ${nf0.format(sig.base)} ${sig.unit} of this item` +
    (sig.span ? `, which ran over ${fSpan(sig.span)} (first to last seen)` : "") + `: how significant this group is to it.`;

const AVG_TIP = "Average time of the requests / jobs of this item that carry this value: compare it with the item's own average to see which values are slow.";

function factTable(f, owner) {
    const shown = Math.min(factShown[f.detail] || FACT_CHUNK, f.values.length);
    const filt = owner && ffFind(owner.by, owner.name);
    const picked = new Set((filt && filt.facts[f.detail]) || []);
    const can = !!owner && !state.data.imported && !(owner.by === "issue" && ISSUE_UNFILTERABLE.includes(f.detail));
    const logins = f.values.some(v => v[2]), sig = !!(f.sig && f.sig.base), avg = !!f.avg_ms;
    const rows = f.values.slice(0, shown).map(([v, n, who], i) => {
        const ok = can && v !== FACT_OTHER;
        return `<tr${ok ? ` class="link${picked.has(v) ? " picked" : ""}" data-fact="${esc(f.detail)}" data-v="${esc(v)}"` : ""}>` +
            `<td class="l val">${esc(v)}</td>${logins ? `<td class="l dim">${who ? esc(who.join(", ")) : ""}</td>` : ""}` +
            `<td style="color:${TONE.calls}">${nf0.format(n)}</td><td>${fPct(f.total ? 100 * n / f.total : 0)}</td>` +
            (sig ? `<td class="sig">${sigText(n, f.sig)}</td>` : "") +
            (avg ? `<td style="color:${TONE.avg_ms}">${f.avg_ms[i] == null ? "" : nf0.format(f.avg_ms[i])}</td>` : "") + `</tr>`;
    }).join("");
    const notes = (f.distinct > f.values.length ? `<div class="sub">…and ${nf0.format(f.distinct - f.values.length)} more values not kept</div>` : "") +
        (f.other_n ? `<div class="sub">${nf0.format(f.other_n)} more not told apart (too many distinct values)</div>` : "");
    const why = !owner || state.data.imported ? "" : !can ? " · a warning has no lines of its own, so it can't filter by these"
        : f.unfiltered ? " · every value listed, not only the picked ones" : "";
    return `<div class="factbox" data-factbox="${esc(f.detail)}"><div class="facthead"><b>${esc(f.detail)}</b>` +
        `<span class="sub">${nf0.format(f.distinct)}${f.other_n ? "+" : ""} distinct · ${nf0.format(f.total)} in all${why}</span></div>` +
        `<div class="table-wrap factwrap"><table class="facttbl static"><thead><tr><th class="l">value</th>${logins ? `<th class="l">accounts logged in from it</th>` : ""}` +
        `<th>count</th><th data-tip="Count divided by all the values of this detail.">share</th>` +
        (sig ? `<th data-tip="${esc(sigTip(f.sig))}">of ${esc(f.sig.unit)}</th>` : "") +
        (avg ? `<th data-tip="${esc(AVG_TIP)}">avg ms</th>` : "") + `</tr></thead><tbody>${rows}</tbody></table></div>` +
        loadMoreBtn("fact:" + f.detail, f.values.length, shown) + notes + `</div>`;
}

// owner: {by, name} of the row the values can filter, or null (not filterable). A form can show several of
// these (its own details, those of its logged lines): lastFacts keeps them all, for "Load more".
// note / empty: what the tables are, and what to say when there are none; sig: see sigText
function factsSection(facts, what, owner, note, empty, sig) {
    if (!lastFacts || lastFacts.owner !== owner) lastFacts = {facts: [], owner};
    for (const f of facts || []) f.sig = sig;
    lastFacts.facts = lastFacts.facts.filter(f => !(facts || []).some(g => g.detail === f.detail)).concat(facts || []);
    if (!facts || !facts.length) return `<div class="sub">${empty || `Nothing was collapsed into this ${what}: no ids, clients or numbers were told apart.`}</div>`;
    const can = owner && !state.data.imported;
    return `<div class="sub srcnote">${note || `The ${what} collapses these into one row; most frequent first.`}` +
        (can ? ` <b>Click to filter.</b>` : "") + `</div><div class="factgrid">` + facts.map(f => factTable(f, owner)).join("") + `</div>`;
}

/* ---- popup with everything about one warning / error ------------------------------------------- */
function issueOpsTable(r) {
    if (!r.ops.length) return `<div class="sub">Not logged during a request or job (start-up, another process, or a worker with no matching request).</div>`;
    return `<div class="sub srcnote">The operation each worker was running when it logged this, matched by worker and time. Click one to open it.` +
        (r.unattributed ? ` ${nf0.format(r.unattributed)} not tied to any request.` : "") + `</div>` +
        `<div class="table-wrap formtbl"><table class="static"><thead><tr><th class="l">operation</th><th>times logged</th><th>share</th></tr></thead><tbody>` +
        r.ops.map(([o, c]) => `<tr class="link" data-open-op="${esc(o)}"><td class="l">${esc(o)}</td><td style="color:${TONE.calls}">${nf0.format(c)}</td><td>${fPct(r.n ? 100 * c / r.n : 0)}</td></tr>`).join("") +
        `</tbody></table></div>` + (r.ops_n > r.ops.length ? `<div class="sub">…and ${nf0.format(r.ops_n - r.ops.length)} more operations</div>` : "");
}

// "Select" in a form's sheet: the same as right-clicking its row (a histogram item, a row of the selected items table)
const formSelected = () => !form ? false : form.kind === "issue"
    ? state.issueTags.some(t => sameIssue(t, {level: form.key[0], logger: form.key[1], message: form.key[2]}))
    : isPicked(form.by, form.name);
const selButton = () => `<button class="btn ghost selbtn${formSelected() ? " on" : ""}" id="formSel"` +
    ` data-tip="Selected items are plotted on the histogram and listed in the selected items table.">${formSelected() ? "★ Selected" : "☆ Select"}</button>`;

function toggleFormSelect() {
    if (!form) return;
    if (form.kind === "issue") {
        const k = {level: form.key[0], logger: form.key[1], message: form.key[2]}, i = state.issueTags.findIndex(t => sameIssue(t, k));
        if (i >= 0) removeIssueTag(i); else addIssueTag(k);
    } else {
        const i = state.histTags.findIndex(t => t.by === form.by && t.term === form.name);
        if (i >= 0) removeHistTag(i); else addHistTag({by: form.by, term: form.name, label: form.name});
    }
}

function renderFormSelect() {
    const b = $("#formSel");
    if (!b) return;
    b.classList.toggle("on", formSelected());
    b.textContent = formSelected() ? "★ Selected" : "☆ Select";
}

function openIssue(key) {
    if (!state.data) return;
    const v = view(), r = v.issues.find(x => sameIssue(x, key)), k = issueKeyOf(key);
    if (!r && !ffFind("issue", k)) return false;
    pushForm();
    if (!restoringForm) factShown = {};
    form = {kind: "issue", key: k};
    const seq = ++detailSeq;
    raw.seq++;                                                          // occurrences of the previous form must not land here
    const total = v.levels.errors + v.levels.warnings;
    const fact = (name, val) => val ? `<div class="fact"><span class="k">${name}</span><span class="val">${esc(val)}</span></div>` : "";
    const head = `<div class="mhead"><span class="badge ${key.level === "WARNING" ? "b-mixed" : "b-slow-queries"}">${esc(key.level)}</span>` +
        (r ? `<span><b>${nf0.format(r.n)}</b> times</span><span class="sub">${fPct(total ? 100 * r.n / total : 0)} of all warnings and errors</span>`
           : `<span class="sub">no occurrence left with the current filters</span>`) + selButton() + `</div>`;
    const sheet = head +
        `<div class="msg-block"><span class="k">Message <span class="sub">numbers and ids replaced by #, so repeats group together</span></span><pre class="full">${esc(key.message)}</pre></div>` +
        (r ? `<div class="facts">` + fact("Logger", r.logger) + fact("Exception", r.exc) + fact("Source", r.src) + fact("First seen", fStamp(r.first)) + fact("Last seen", fStamp(r.last)) + `</div>` : "");
    const owner = {by: "issue", name: k}, sig = r ? {base: r.n, unit: "occurrences", span: spanSecs(r.first, r.last)} : null;
    const tabs = [
        {id: "details", label: "Details behind the grouping", html: `<div id="factsSection">${factsSection(r ? r.facts : [], "message template", owner, null, null, sig)}</div>`},
        {id: "ops", label: r ? `Parent operations (${nf0.format(r.ops_n)})` : "Parent operations", html: `<div class="pane-card">${r ? issueOpsTable(r) : `<div class="sub">No occurrence left with the current filters.</div>`}</div>`},
    ];
    if (!state.data.imported) tabs.push({id: "raw", label: "Raw log lines", html: rawSection(r && r.sample)});
    showForm(key.level === "WARNING" ? "Warning details" : "Error details", sheet, tabs, key.level === "WARNING" ? "WARNING" : "ERROR");
    if (state.data.imported) return true;                               // no log behind an imported summary
    const p = selectionParams();
    p.set("level", key.level);
    p.set("logger", key.logger);
    p.set("msg", key.message);
    api("/api/issue_detail?" + p).then(d => {                          // every kept value, not only the top few
        if (seq !== detailSeq || !d.facts) return;
        $("#factsSection").innerHTML = factsSection(d.facts, "message template", owner, null, null, sig);
    }).catch(() => { /* the top values shown already will do */ });
    if (r) loadRawLines("/api/issue_lines?" + p, r.n, false, {level: key.level, logger: key.logger, msg: key.message});
    else $("#rawOcc").innerHTML = `<div class="sub">No occurrence left with the current filters.</div>`;
    return true;
}

/* ---- popup with everything about one operation / app / type ------------------------------------ */
const DETAIL_TITLE = {operation: "Operation details", app: "App details", family: "Type details"};
const DETAIL_CHUNK = {ops: 25, sources: 20};   // items revealed per "Load more" click
let detailShown = {ops: DETAIL_CHUNK.ops, sources: DETAIL_CHUNK.sources};  // how many rows of each list are shown, reset per form
let lastDetail = null;  // {by, name, r, d} of the open form, so "Load more" can re-render its lists without another request

const detailRow = (by, name) => by === "operation" ? opRows().find(r => r.operation === name)
    : ((state.data && view().groups[by]) || []).find(r => groupName(r, by) === name);

function detailFacts(by, r) {
    const job = r.unsplit_s > 0 && !(r.sql_s + r.python_s);
    const total = view().totals.total_s, pct = by === "operation" ? (total ? 100 * r.total_s / total : 0) : r.pct_of_total_time;
    const f = (k, val) => `<div class="fact"><span class="k">${k}</span><span class="val">${val}</span></div>`;
    const fc = (k, val, color) => `<div class="fact"><span class="k">${k}</span><span class="val" style="color:${color}">${val}</span></div>`;
    const pctColor = heat(pct, SCALES.pct_of_total_time);
    return `<div class="facts">` +
        (by === "operation" ? f("Type", esc(r.family)) + f("App", esc(r.app)) : "") +
        fc("Calls", nf0.format(r.calls), TONE.calls) +
        f("Total time", `${fSec(r.total_s)} s · <span style="color:${pctColor}">${fPct(pct)} of all time</span>`) +
        f("Average · p95 · max", `<span style="color:${TONE.avg_ms}">${nf0.format(r.avg_ms)} ms</span> · ${fSec(r.p95_s)} s · ${fSec(r.max_s)} s`) +
        (job ? f("SQL / Python", "not split (background job)") : f("SQL / Python", `${fSec(r.sql_s)} s · ${fSec(r.python_s)} s`) +
            f("Queries", `${nf1.format(r.queries_per_call)} per call · ${nf1.format(r.ms_sql_per_query)} ms each`)) +
        (r.http_5xx ? f("HTTP 5xx", nf0.format(r.http_5xx)) : "") +
        (by === "operation" ? f("Bottleneck", `<span class="badge b-${esc(r.bottleneck)}">${esc(r.bottleneck)}</span>`) : "") + `</div>`;
}

function lineBlock(line, label, extra) {
    return `<div class="exline"><div class="exhead"><span>${label}${extra ? ` <span class="sub">${extra}</span>` : ""}</span>` +
        `<button class="btn ghost" data-copy="${esc(line)}">Copy</button></div><pre class="full">${esc(line)}</pre></div>`;
}

function loadMoreBtn(key, total, shown) {
    return total > shown
        ? `<button class="btn ghost loadmore" data-load-more="${esc(key)}">Load more <span class="sub">(${nf0.format(shown)} of ${nf0.format(total)} shown)</span></button>` : "";
}

function opsSection(d) {
    const shown = Math.min(detailShown.ops, d.top_ops.length), tot = d.top_ops.reduce((s, o) => s + o.total_s, 0);
    return `<div class="sub srcnote">Every operation of this ${GROUP_LABEL[d.by]}, heaviest first. Click one to open it.</div>` +
        `<div class="table-wrap formtbl"><table class="static"><thead><tr><th class="l">operation</th><th>calls</th><th>total s</th><th>share</th><th>avg ms</th></tr></thead><tbody>` +
        d.top_ops.slice(0, shown).map(o => `<tr class="link" data-open-op="${esc(o.operation)}"><td class="l">${esc(o.operation)}${ffBadge("operation", o.operation)}</td>` +
            `<td style="color:${TONE.calls}">${nf0.format(o.calls)}</td><td>${fSec(o.total_s)}</td><td>${fPct(tot ? 100 * o.total_s / tot : 0)}</td>` +
            `<td style="color:${TONE.avg_ms}">${nf0.format(o.calls ? 1000 * o.total_s / o.calls : 0)}</td></tr>`).join("") +
        `</tbody></table></div>` + loadMoreBtn("ops", d.top_ops.length, shown);
}

function sourcesSection(d, grouped) {
    const s = d.sources, sig = detailSig(d);
    const shown = Math.min(detailShown.sources, s.items.length);
    let h = `<div class="pane-head"><b>The lines</b>${s.total ? `<span class="sub">${nf0.format(s.total)} lines, ${nf0.format(s.distinct)} distinct</span>` : ""}</div>`;
    h += s.items.length
        ? `<div class="sub srcnote">Lines with no timing of their own (ORM calls, add-on messages) that the same worker logged while ` +
          `${grouped ? "these" : "this"} ran, matched by worker and time. They inherit the timing above, so each one shows what ` +
          `${grouped ? "they" : "it"} cost.</div>` +
          s.items.slice(0, shown).map(e => `<details class="src"><summary><span class="skey">${esc(e.key)}</span><span class="n"><span style="color:${TONE.calls}">${nf0.format(e.n)} ×</span></span>` +
              (sig ? `<span class="sig" data-tip="${esc(sigTip(sig))}">${sigText(e.n, sig, true)}</span>` : "") +
              (e.avg_ms != null ? `<span class="sig" data-tip="${esc(`Average time of the ${nf0.format(e.runs)} requests / jobs that logged this line.`)}">` +
                  `<span style="color:${TONE.avg_ms}">${nf0.format(e.avg_ms)} ms</span> <span class="sub">avg</span></span>` : "") + `</summary>` +
              (e.sample ? lineBlock(e.sample, "example") : "") +
              (grouped && e.ops.length ? `<div class="sub">during ${e.ops.map(([o, n]) => `${esc(o)} × <span style="color:${TONE.calls}">${nf0.format(n)}</span>`).join(", ")}</div>` : "") + `</details>`).join("") +
          loadMoreBtn("sources", s.items.length, shown)
        : `<div class="sub">Nothing was matched. This needs the worker's INFO / DEBUG lines in the log (for example the <code>odoo.api</code> ` +
          `<code>call model().method(…)</code> lines that <code>--log-level=debug</code> writes).</div>`;
    return h;
}

const detailSig = d => d && d.calls ? {base: d.calls, unit: "calls", span: spanSecs(d.first, d.last)} : null;

// first / last seen, once the details have loaded: more widgets in the sheet
const spanFacts = d => !d.first ? "" : [["First seen", fStamp(d.first)], ["Last seen", fStamp(d.last)],
    ["Active in", `${nf0.format(d.active_minutes)} distinct minutes`]].map(([k, val]) =>
    `<div class="fact"><span class="k">${k}</span><span class="val">${esc(val)}</span></div>`).join("");

function openDetail(by, name) {
    if (!state.data) return false;
    const r = detailRow(by, name);
    if (!r && !ffFind(by, name)) return false;                               // a filtered row stays reachable to change its filter
    pushForm();
    if (!restoringForm) {                                                    // a new visit: start collapsed again
        detailShown = {ops: DETAIL_CHUNK.ops, sources: DETAIL_CHUNK.sources};
        factShown = {};
    }
    form = {kind: "detail", by, name};
    const seq = ++detailSeq;
    raw.seq++;                                                               // occurrences of the previous form must not land here
    lastDetail = null;
    const grouped = by !== "operation", loading = `<div class="sub loading">Loading…</div>`, gone = `No ${GROUP_LABEL[by]} occurrence is left with the current filters.`;
    const sheet = `<div class="mhead"><span class="badge b-mixed">${GROUP_LABEL[by]}</span><b class="mname">${esc(name)}</b>${selButton()}</div>` +
        (by === "operation" && opHint(name) ? `<div class="sub hint">${opHint(name)}</div>` : "") +
        (r ? detailFacts(by, r) : `<div class="sub hint">${gone}</div>`) + `<div id="ovMore"></div>`;
    const tabs = [
        {id: "details", label: "Details behind the grouping", html: `<div id="factsSection">${loading}</div>`},
    ];
    if (grouped) tabs.push({id: "ops", label: "Operations", html: `<div class="pane-card" id="opsSection">${loading}</div>`});
    tabs.push({id: "sources", label: "Logged during", html: `<div class="pane-card" id="srcSection">${loading}</div><div id="loggedFacts">${loading}</div>`});
    if (!state.data.imported) tabs.push({id: "raw", label: "Raw log lines", html: rawSection()});
    showForm(DETAIL_TITLE[by], sheet, tabs, by);
    const owner = {by, name}, what = grouped ? GROUP_LABEL[by] : "operation name";
    const loggedNote = `Users and records named in the lines logged while ${grouped ? "its operations" : "it"} ran (ORM calls, warnings and their tracebacks), ` +
        `each counted once per request: they tell which records a request worked on, and filtering by one keeps only those requests.`;
    const loggedEmpty = `None of the lines logged while ${grouped ? "its operations" : "it"} ran name a user or a record. The ORM's <code>call model(ids).method()</code> lines need <code>--log-level=debug</code>.`;
    if (state.data.imported) {                                               // no log behind it: only what the export kept
        $("#factsSection").innerHTML = factsSection(r && r.facts, what, null, null, null, r && {base: r.calls, unit: "calls", span: null});
        $("#loggedFacts").innerHTML = "";
        for (const id of ["#opsSection", "#srcSection"]) if ($(id)) $(id).innerHTML = `<div class="sub">An imported summary keeps no log lines.</div>`;
        return true;
    }
    const p = selectionParams();
    p.set("by", by);
    p.set("name", name);
    api("/api/detail?" + p).then(d => {
        if (seq !== detailSeq) return;
        const sig = detailSig(d);
        $("#factsSection").innerHTML = factsSection(d.facts, what, owner, null, null, sig);
        $("#loggedFacts").innerHTML = `<div class="pane-card logged-card"><div class="pane-head"><b>Details in these lines</b></div>` +
            factsSection(d.logged_facts, what, owner, loggedNote, loggedEmpty, sig) + `</div>`;
        if (!d.found) {
            const msg = `<div class="sub">${ffFind(by, name) ? gone : `This ${GROUP_LABEL[by]} is not in the current selection any more.`}</div>`;
            for (const id of ["#opsSection", "#srcSection", "#rawOcc"]) if ($(id)) $(id).innerHTML = msg;
            return;
        }
        lastDetail = {by, name, r, d};
        const facts = $("#mSheet .facts");
        if (facts) facts.insertAdjacentHTML("beforeend", spanFacts(d));
        if (grouped) { $("#opsSection").innerHTML = opsSection(d); setTabLabel("ops", `Operations (${nf0.format(d.top_ops.length)})`); }
        $("#srcSection").innerHTML = sourcesSection(d, grouped);
        if (d.sources.total) setTabLabel("sources", `Logged during (${nf0.format(d.sources.total)})`);
        loadRawLines("/api/op_lines?" + p, r ? r.calls : 0, true, {by, name});
    }).catch(e => {                                                          // say so in every pane still waiting
        if (seq !== detailSeq) return;
        const msg = `<div class="sub err">Could not load the details: ${esc(e.message)}</div>`;
        for (const id of ["#factsSection", "#loggedFacts", "#opsSection", "#srcSection", "#rawOcc"]) {
            const el = $(id);
            if (el && el.querySelector(".loading")) el.innerHTML = msg;
        }
    });
    return true;
}

function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    const ta = document.createElement("textarea");                      // fallback for plain http on a non-local address
    ta.value = text;
    ta.style.cssText = "position:fixed;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
    return Promise.resolve();
}

document.addEventListener("contextmenu", e => {                          // right-click a row: add/remove it as a histogram item
    const tr = e.target.closest("#tbl tbody tr[data-name]");
    if (!tr || !(state.data && state.data.hist_key)) return;
    e.preventDefault();
    const by = state.tableGroup, name = tr.dataset.name, i = state.histTags.findIndex(t => t.by === by && t.term === name);
    const short = esc(name.length > 60 ? name.slice(0, 59) + "…" : name);
    if (i >= 0) { removeHistTag(i); showTip(`Removed <b>${short}</b> from the histogram items`, e.clientX, e.clientY); }
    else { addHistTag({by, term: name, label: name}); showTip(`Added <b>${short}</b> to the histogram items`, e.clientX, e.clientY); }
    setTimeout(hideTip, 1400);
});

document.addEventListener("contextmenu", e => {                          // right-click a warning/error: pin its occurrences to the histogram
    const tr = e.target.closest("#issuesTbl tbody tr[data-i]");
    if (!tr || !(state.data && state.data.hist_key)) return;
    const r = view().issues[+tr.dataset.i];
    if (!r) return;
    e.preventDefault();
    const short = esc(r.message.length > 60 ? r.message.slice(0, 59) + "…" : r.message);
    if (isIssuePicked(r)) {
        removeIssueTag(state.issueTags.findIndex(t => t.level === r.level && t.logger === r.logger && t.message === r.message));
        showTip(`Removed <b>${short}</b> from the histogram`, e.clientX, e.clientY);
    } else {
        addIssueTag(r);
        showTip(`Added <b>${short}</b> to the histogram`, e.clientX, e.clientY);
    }
    setTimeout(hideTip, 1400);
});


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

function applyCollapsed() {
    for (const [name, panel] of [["hist", "#histPanel"], ["table", "#tablePanel"], ["sel", "#selPanel"], ["issues", "#issuesPanel"]]) {
        $(panel).classList.toggle("collapsed", state.hidden[name]);
        $(`[data-toggle="${name}"]`).textContent = state.hidden[name] ? "Show" : "Hide";
    }
}

/* ---------------------------------------------------------------------- *
 * events
 * ---------------------------------------------------------------------- */
document.addEventListener("click", e => {
    const t = e.target;
    let el;
    if (!t.closest("#colsMenu") && !t.closest("#colsBtn")) $("#colsMenu").hidden = true;    // click elsewhere closes the column menu
    if (!t.closest("#histSeriesMenu") && !t.closest("#histSeriesBtn")) $("#histSeriesMenu").hidden = true;
    if ((el = t.closest("[data-unload]"))) return unload(el.dataset.unload);
    if ((el = t.closest("[data-db]"))) return toggleDb(el.dataset.db);
    if ((el = t.closest("[data-only-db]"))) return setOnlyDb(el.dataset.onlyDb);
    if (t.closest("#onlyDbClear")) return setOnlyDb("");
    if (t.closest("#dbScanBtn")) return $("#dbScanPicker").click();
    if (t.closest("#dbScanClose")) { $("#dbScan").hidden = true; return; }
    if ((el = t.closest("[data-file]"))) return toggleFile(el.dataset.file);
    if ((el = t.closest("[data-rm-item]"))) return removeItem(el.dataset.rmItem);
    if (t.closest("#histRmAll")) return removeAllItems();
    if (t.closest("#histDefaults")) {                         // every item back to its default series
        state.totalSeries = pickedCount() ? {bars: new Set(), lines: new Set()} : defaultSeries("total");
        state.histTags.forEach(x => Object.assign(x, defaultSeries("tag")));
        state.issueTags.forEach(x => Object.assign(x, defaultSeries("issue")));
        return itemsChanged(pickedCount());
    }
    if ((el = t.closest("[data-tab]"))) return setTab(el.dataset.tab);
    if ((el = t.closest("tr[data-fact]"))) {
        const o = formOwner();
        if (o && !getSelection().toString()) toggleFact(o.by, o.name, el.dataset.fact, el.dataset.v);
        return;
    }
    if ((el = t.closest("[data-ff-rm]"))) { const o = formOwner(); return o && toggleFact(o.by, o.name, el.dataset.ffRm, el.dataset.v); }
    if (t.closest("#ffClearForm")) { const o = formOwner(); return o && clearFacts(o.by, o.name); }
    if (t.closest("#clearFacts")) return openFfModal();
    if (t.closest("#ffClose") || t.id === "ffModal") { $("#ffModal").hidden = true; return; }
    if (t.closest("#ffClearAll")) { $("#ffModal").hidden = true; return clearFacts(null); }
    if ((el = t.closest("[data-ff-del]"))) { const f = state.factFilters[+el.dataset.ffi]; return f && toggleFact(f.by, f.name, el.dataset.ffDel, el.dataset.v); }
    if ((el = t.closest("[data-ff-row]"))) {                    // one item's whole detail
        const f = state.factFilters[+el.dataset.ffi];
        if (!f) return;
        delete f.facts[el.dataset.ffRow];
        if (!Object.keys(f.facts).length) state.factFilters.splice(+el.dataset.ffi, 1);
        return factsChanged();
    }
    if ((el = t.closest(".fflink"))) {
        const f = state.factFilters[+el.dataset.ffi];
        if (!f) return;
        $("#ffModal").hidden = true;
        return f.by === "issue" ? openIssue({level: f.name[0], logger: f.name[1], message: f.name[2]}) : openDetail(f.by, f.name);
    }
    if (t.closest("#formSel")) return toggleFormSelect();
    if (t.closest("#domOpen")) return openFilterEditor();
    if (t.closest("#domClose") || t.id === "domModal") { $("#domModal").hidden = true; return; }
    if (t.closest("#domApply")) return applyDomain();
    if (t.closest("#domReset")) { $("#domText").value = domApplied; return checkDomain(); }
    if ((el = t.closest("#domCopy"))) return copyText($("#domText").value).then(() => { el.textContent = "Copied"; setTimeout(() => el.textContent = "Copy", 1200); });
    if (t.closest("#serClose")) { $("#histSeriesMenu").hidden = true; return; }
    if ((el = t.closest("[data-sort]"))) {
        const k = el.dataset.sort;
        state.sort = state.sort.key === k ? {key: k, dir: -state.sort.dir} : {key: k, dir: k === "operation" || k === "bottleneck" ? 1 : -1};
        return renderTable();
    }
    if ((el = t.closest("[data-toggle]"))) {
        const n = el.dataset.toggle;
        state.hidden[n] = !state.hidden[n];
        store.set("hide" + n[0].toUpperCase() + n.slice(1), state.hidden[n]);
        applyCollapsed();
        if (!state.hidden[n] && state.data) ({hist: renderHist, table: renderTable, sel: renderSelTable, issues: renderIssues})[n]();
        return;
    }
    if ((el = t.closest("[data-load-more]"))) {
        const key = el.dataset.loadMore;
        if (key.startsWith("fact:")) {                                   // one table of the details behind the grouping
            const slot = key.slice(5), f = lastFacts && lastFacts.facts.find(x => x.detail === slot);
            const box = [...document.querySelectorAll("[data-factbox]")].find(b => b.dataset.factbox === slot);
            if (!f || !box) return;
            factShown[slot] = (factShown[slot] || FACT_CHUNK) + 2 * FACT_CHUNK;
            box.outerHTML = factTable(f, lastFacts.owner);
            return;
        }
        if (!lastDetail) return;
        detailShown[key] += DETAIL_CHUNK[key];
        const section = key === "ops" ? $("#opsSection") : $("#srcSection");
        if (section) section.innerHTML = key === "ops" ? opsSection(lastDetail.d) : sourcesSection(lastDetail.d, lastDetail.by !== "operation");
        return;
    }
    if ((el = t.closest("[data-open-op]"))) {
        if (openDetail("operation", el.dataset.openOp)) return;
        const r = el.getBoundingClientRect();                       // filtered away, or outside the time interval
        showTip("<b>" + esc(el.dataset.openOp) + "</b> has no occurrence in the current selection.", r.left, r.bottom - 12);
        return setTimeout(hideTip, 1800);
    }
    if ((el = t.closest("#issuesTbl tbody tr[data-i]"))) { const r = view().issues[+el.dataset.i]; return r && openIssue(r); }
    if ((el = t.closest("[data-occ-i]"))) {
        $("#rawOcc").querySelectorAll("tr.picked").forEach(e => e.classList.remove("picked"));
        el.classList.add("picked");
        raw.sel = +el.dataset.occI;
        return renderRawCtx();
    }
    if (t.closest("#mClose")) return closeModal(true);
    if (t.closest("#mBack")) return closeModal();
    if ((el = t.closest("#ctxCopy"))) return copyText(raw.ctxText || "").then(() => { el.textContent = "Copied"; setTimeout(() => el.textContent = "Copy", 1200); });
    if ((el = t.closest("[data-copy]"))) return copyText(el.dataset.copy).then(() => { el.textContent = "Copied"; setTimeout(() => el.textContent = "Copy", 1200); });
    if (t.closest("#colsBtn")) {
        const menu = $("#colsMenu");
        if (menu.hidden) renderColsMenu();
        menu.hidden = !menu.hidden;
        return;
    }
    if (t.closest("#histSeriesBtn")) {
        const menu = $("#histSeriesMenu");
        if (menu.hidden) renderHistSeriesMenu();
        menu.hidden = !menu.hidden;
        if (!menu.hidden) $("#histFilter").focus();
        return;
    }
    if (t.closest("#histFit")) { state.histFit = !state.histFit; store.set("histFit", state.histFit); return renderHist(); }
    if (t.closest("#histFocus")) return state.focus ? clearFocus() : setFocus(state.interval);
    if (t.closest("#histFocusSel")) return setFocus(state.interval);
    if (t.closest("#clearHist") || t.closest("#clearTop")) return setInterval_(null);
    if (t.closest("#addBtn") || t.closest("#drop")) return $("#picker").click();
    if (t.closest("#importBtn")) return $("#importPicker").click();
    if (t.closest("#resetBtn")) return resetAll();
    if (t.closest("#exportBtn")) return exportSummary();
});

document.addEventListener("input", e => {
    if (e.target.id !== "ctxN") return;
    const n = Math.max(0, Math.min(MAX_CTX, parseInt(e.target.value, 10) || 0));
    issueContext = n;
    store.set(CTX_KEY, n);
    renderRawCtx();
});
document.addEventListener("change", e => {
    if (e.target.id !== "rawOrder") return;
    raw.order = e.target.value;
    store.set("rawOrder", raw.order);
    $("#rawOcc").innerHTML = `<div class="pane-card"><div class="sub loading">Finding occurrences…</div></div>`;
    loadRawLines(raw.url, raw.total, raw.timed);
});

$("#colsMenu").addEventListener("change", e => {
    const k = e.target.dataset.col;
    if (!k) return;
    if (e.target.checked) state.hiddenCols.delete(k); else state.hiddenCols.add(k);
    store.set("hiddenCols", [...state.hiddenCols]);
    renderTable();
});
$("#drop").addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#picker").click(); } });
$("#picker").addEventListener("change", e => { upload(e.target.files); e.target.value = ""; });
$("#dbScanPicker").addEventListener("change", e => { scanDbs(e.target.files); e.target.value = ""; });
$("#onlyDb").addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); e.target.blur(); }
    else if (e.key === "Escape") { e.target.value = onlyDb; e.target.blur(); }
});
$("#onlyDb").addEventListener("change", e => setOnlyDb(e.target.value));
$("#importPicker").addEventListener("change", e => { upload(e.target.files); e.target.value = ""; });
$("#tableGroup").addEventListener("change", e => { state.tableGroup = e.target.value; store.set("tableGroup", state.tableGroup); state.filter = ""; $("#filter").value = ""; renderTable(); });
$("#issueLevel").addEventListener("change", e => { state.issueLevel = e.target.value; renderIssues(); });
const debounce = (fn, ms = 120) => { let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); }; };
$("#issueFilter").addEventListener("input", debounce(e => { state.issueFilter = e.target.value; renderIssues(); }));
$("#filter").addEventListener("input", debounce(e => { state.filter = e.target.value; renderOpTable(TABLES.main, true); }));
$("#dbScanFilter").addEventListener("input", debounce(renderDbScan));
$("#histFilter").addEventListener("keydown", e => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const v = e.target.value.trim();
    if (!v) return;
    addHistTag({by: null, term: v.toLowerCase(), label: v});
    e.target.value = "";
});
$("#histSize").addEventListener("change", e => { state.histSize = e.target.value; renderHist(); });
$("#histSeriesMenu").addEventListener("change", e => {
    const t = e.target, it = t.dataset.item && itemById(t.dataset.item);
    if (!it) return;
    if (t.checked) it.cfg[t.dataset.side].add(t.dataset.s); else it.cfg[t.dataset.side].delete(t.dataset.s);
    t.closest("td").classList.toggle("on", t.checked);
    renderHist();
});
$("#tzSelect").addEventListener("change", e => {
    state.tz = e.target.value;
    store.set("tz", state.tz);
    renderTzUi();
    if (state.data && !state.hidden.hist) renderHist();
});
$("#tFrom").addEventListener("change", applyTimeInputs);
$("#tTo").addEventListener("change", applyTimeInputs);

const drop = $("#drop");
let dragDepth = 0;
addEventListener("dragover", e => e.preventDefault());
addEventListener("dragenter", e => { e.preventDefault(); dragDepth++; drop.classList.add("over"); });
addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; drop.classList.remove("over"); } });
addEventListener("drop", e => {
    e.preventDefault();
    dragDepth = 0;
    drop.classList.remove("over");
    if (e.dataTransfer && e.dataTransfer.files.length) upload(e.dataTransfer.files);
});

$("#tableGroup").value = state.tableGroup;
api("/api/only_db").then(j => { onlyDb = j.db || ""; renderOnlyDb(); renderDbs(); }).catch(() => {});
refresh().catch(e => setNotice("Could not reach the server: " + e.message, true));
