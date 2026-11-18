"use strict";

/* The page's state and what every other script uses: $ / esc / store, the number and time formats,
 * the table columns (COLS), the tooltip, the busy indicator.
 * One of the page's scripts: templates/index.html lists them in load order (see ARCHITECTURE.md). */

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
    sqTags: [],                          // pinned slow-query shapes [{shape, label, bars, lines, hue}]
    totalSeries: {bars: new Set(["req"]), lines: new Set(["avg_all"])},   // what the "total" item plots
    histSize: "auto",                   // bucket size in minutes; "auto" = 10 min (or ~200 bars when focused)
    focus: null,                         // "Focus interval": {from, to} in unix seconds, the only span the chart shows
    focusHist: null,                     // ... and its timeline, at the finer resolution the server sends for it
    factFilters: [],                     // values picked in the details behind a row (see toggleFact)
    extraDomain: null,                   // the rest of the filter editor's domain: a prefix list the server evaluates per occurrence
    ips: [],                             // client addresses the whole report is narrowed to (the domain's "ip", see setIps)
    tableGroup: ["operation", "app", "family", "slowq"].includes(store.get("tableGroup")) ? store.get("tableGroup") : "operation",
    sqSort: {key: "total_s", dir: -1},   // the slow-query table's own sort (its columns are not the operations')
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

// what a table or a chart item groups rows by
const GROUP_LABEL = {family: "type", app: "app", operation: "operation", slowq: "slow query"};
const GROUP_UNIT = {family: "types", app: "apps", operation: "operations", slowq: "distinct slow queries"};
const groupName = (r, by) => by === "family" ? r.family : by === "app" ? r.app : r.operation;
