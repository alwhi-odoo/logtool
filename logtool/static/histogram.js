"use strict";

/* The requests-over-time chart: its series, bucket sizes, drawing, key, focus on one interval, and the
 * clicks on its bars that set the time interval.
 * One of the page's scripts: templates/index.html lists them in load order (see ARCHITECTURE.md). */

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
// an error / warning only has occurrences; a slow query, occurrences and the time they ran (not split into SQL / Python)
const seriesAllowed = (it, key) => it.kind === "issue" ? key === "req" : it.kind === "slowq" ? ["req", "avg_all", "tot_all"].includes(key) : true;
const seriesName = (it, s) => s.key === "req" ? (it.kind === "issue" || it.kind === "slowq" ? "occurrences" : "requests") : GROUP_LABEL_S[s.group] + COMP_LABEL[s.comp];
const unitLabel = (u, items) => u === "req" && items.length && items.every(i => i.kind === "issue" || i.kind === "slowq") ? "occurrences" : UNIT_LABEL[u];
// one hue per item; a series is a shade of it, and lines are dashed by what they measure
const SHADE = {req: [0, 0], avg_all: [0, 16], avg_py: [34, 18], avg_sql: [-34, 8], tot_all: [0, -16], tot_py: [34, -8], tot_sql: [-34, -22]};
const seriesColor = (hue, key) => `hsl(${(hue + SHADE[key][0] + 360) % 360} 60% ${60 + SHADE[key][1]}%)`;
const DASH = {avg: "", tot: "7 4", req: "2 3"};
const ITEM_HUES = [190, 20, 100, 330, 45, 215, 150, 300, 0, 75, 240, 165];
const TOTAL_HUE = 270;
const defaultSeries = kind => ({bars: new Set(["req"]), lines: new Set(kind === "issue" || kind === "slowq" ? [] : ["avg_all"])});

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
        .concat(state.issueTags.map((t, i) => ({id: "i" + i, kind: "issue", tag: t, label: t.label, cfg: t, hue: t.hue})))
        .concat(state.sqTags.map((t, i) => ({id: "q" + i, kind: "slowq", tag: t, label: t.label, cfg: t, hue: t.hue})));
}
const itemById = id => histItems().find(it => it.id === id);
const pickedCount = () => state.histTags.length + state.issueTags.length + state.sqTags.length;
function nextHue() {
    const used = new Set([...state.histTags, ...state.issueTags, ...state.sqTags].map(t => t.hue));
    return ITEM_HUES.find(h => !used.has(h)) ?? ITEM_HUES[pickedCount() % ITEM_HUES.length];
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
    // an error's own timeline: the focused one when focused, else the current view's (looked up fresh
    // each render, so it never goes stale when the interval or the exclusions change); a slow-query shape's,
    // its runs in the current view, one [minute, 1, seconds, 0, 0] each
    const issueRows = state.focus && state.focusHist ? state.focusHist.issues || [] : (state.data && view().issues) || [];
    const maps = histItems().map(it => {
        let packs;
        if (it.kind === "total") packs = [h.all];
        else if (it.kind === "tag") packs = histOps(h).filter(r => tagMatcher(it.tag)(r)).map(r => r.hist);
        else if (it.kind === "slowq") packs = [((sqData().rows.find(r => r.shape === it.tag.shape) || {}).runs || []).map(([t, s]) => [Math.floor(t / 60), 1, s, 0, 0])];
        else { const row = issueRows.find(r => sameIssue(r, it.tag)); packs = row ? [row.hist] : []; }
        return binHist(packs, size).map;
    });
    let k0 = null, k1 = null;
    if (state.focus) {                                                   // the focused span, empty buckets included
        k0 = Math.floor(state.focus.from / 60 / size) * size;
        k1 = Math.floor((Math.ceil(state.focus.to / 60) - 1) / size) * size;
    } else {                                                             // no request at all (slow-query logs alone): the items' span
        const keys = allMap.size ? [...allMap.keys()] : maps.flatMap(m => [...m.keys()]);
        if (keys.length) { k0 = Math.min(...keys); k1 = Math.max(...keys); }
    }
    const keys = [];
    if (k0 !== null) for (let k = k0; k <= k1; k += size) keys.push(k);
    const align = map => keys.map(k => [k, map.get(k) || [0, 0, 0, 0]]);
    const items = histItems().map((it, i) => Object.assign({}, it, {list: align(maps[i])}));
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
    if (!SNAP) h += `<br><span style="color:var(--dim)">${state.anchor ? "click to select the range up to this bar (this bar alone if it is the same one) · Esc to cancel" : "click to start a range here · a second click selects it"}</span>`;
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
    if (!el || !info || !state.data.window || SNAP) return;      // an imported summary / Export HTML has no raw data left to re-slice
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
