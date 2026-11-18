"use strict";

/* The filter editor: the Filter card, the page's view settings as the server reads and writes them
 * (viewState, applyView), the domain popup (check, apply).
 * One of the page's scripts: templates/index.html lists them in load order (see ARCHITECTURE.md). */

/* ---- filter editor: the whole global filter as an Odoo-style domain (see FILTERS.md) ---------------- */
const plural = (n, one, many) => `${nf0.format(n)} ${n === 1 ? one : many || one + "s"}`;
const domTerms = d => (d || []).filter(x => Array.isArray(x)).length;
function filterParts() {
    const parts = [], nf = state.factFilters.reduce((n, f) => n + Object.values(f.facts).reduce((m, v) => m + v.length, 0), 0);
    if (state.excludedFiles.size) parts.push(plural(state.excludedFiles.size, "file") + " left out");
    if (state.excluded.size) parts.push(plural(state.excluded.size, "database") + " left out");
    if (state.interval) parts.push("time interval");
    if (state.ips.length) parts.push("client IP " + state.ips.join(", "));
    if (nf) parts.push(plural(nf, "item filter"));
    if (state.extraDomain) parts.push(plural(domTerms(state.extraDomain), "custom condition"));
    return parts;
}
const viewParts = () => [pickedCount() && plural(pickedCount(), "chart item"), state.focus && "focused"].filter(Boolean);

function filterCard() {
    if (SNAP) return `<div class="stat filtercard"><span class="k">Filter</span><span class="s">${esc(SNAP.filter || "the whole selection")}</span></div>`;
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
const pubCol = k => k === "family" ? "type" : k === "slowq" ? "slow_query" : k;
const intCol = k => k === "type" ? "family" : k === "slow_query" ? "slowq" : k;

// everything the page shows besides the filter itself, in the shape the server writes as view settings
function viewState() {
    const cols = new Set(COLS.filter(c => c.cls !== "op").map(c => c.key));
    return {
        items: state.histTags.map(t => Object.assign(t.by ? {[pubCol(t.by)]: t.term} : {search: t.label}, {bars: pubSeries(t.bars), lines: pubSeries(t.lines)}))
            .concat(state.issueTags.map(t => ({issue: [t.level, t.logger, t.message], bars: pubSeries(t.bars).filter(x => x === "requests"), lines: pubSeries(t.lines).filter(x => x === "requests")})))
            .concat(state.sqTags.map(t => ({slow_query: t.shape, bars: pubSeries(t.bars), lines: pubSeries(t.lines)}))),
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
    state.sqTags = [];
    for (const it of v.items) {
        const series = {bars: intSeries(it.bars), lines: intSeries(it.lines)};
        if (it.slow_query) {
            if (!isSqPicked(it.slow_query))
                state.sqTags.push(Object.assign({shape: it.slow_query, label: sqLabel(sqData().labels[it.slow_query] || it.slow_query)}, series, {hue: nextHue()}));
            continue;
        }
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
    if (st.ip.length) parts.push("client IP " + st.ip.join(", "));
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
    state.ips = st.ip;
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
