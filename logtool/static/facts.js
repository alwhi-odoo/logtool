"use strict";

/* Item filters (values picked in a form's details: toggleFact, the Item filters popup, one client IP)
 * and the tables of what grouping collapsed (factTable, factsSection).
 * One of the page's scripts: templates/index.html lists them in load order (see ARCHITECTURE.md). */

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

// the whole report narrowed to client addresses: only their requests and the warnings logged during them
function setIps(ips) {
    state.ips = ips;
    factsChanged();
}

function renderFfButton() {
    const ip = $("#clearIp");
    ip.hidden = !state.ips.length || !!(state.data && state.data.imported);
    ip.innerHTML = `Client IP: ${esc(state.ips.join(", "))} <span class="tag-x">×</span>`;
    ip.dataset.tip = "The whole report shows only the requests from this client and the warnings / errors logged during them " +
        "(no cron jobs, no warnings logged outside a request). Click to show every client again.";
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
    const can = !!owner && !state.data.imported && !SNAP && !(owner.by === "issue" && ISSUE_UNFILTERABLE.includes(f.detail));
    const logins = f.values.some(v => v[2]), sig = !!(f.sig && f.sig.base), avg = !!f.avg_ms;
    const only = f.detail === "client IP" && !state.data.imported && !SNAP;
    const rows = f.values.slice(0, shown).map(([v, n, who], i) => {
        const ok = can && v !== FACT_OTHER;
        return `<tr${ok ? ` class="link${picked.has(v) ? " picked" : ""}" data-fact="${esc(f.detail)}" data-v="${esc(v)}"` : ""}>` +
            `<td class="l val">${esc(v)}</td>${logins ? `<td class="l dim">${who ? esc(who.join(", ")) : ""}</td>` : ""}` +
            `<td style="color:${TONE.calls}">${nf0.format(n)}</td><td>${fPct(f.total ? 100 * n / f.total : 0)}</td>` +
            (sig ? `<td class="sig">${sigText(n, f.sig)}</td>` : "") +
            (avg ? `<td style="color:${TONE.avg_ms}">${f.avg_ms[i] == null ? "" : nf0.format(f.avg_ms[i])}</td>` : "") +
            (only ? `<td>${v === FACT_OTHER ? "" : `<button class="btn ghost" data-only-ip="${esc(v)}" data-tip="Narrow the whole report to this client: its requests and the warnings logged during them.">only this IP</button>`}</td>` : "") + `</tr>`;
    }).join("");
    const notes = (f.distinct > f.values.length ? `<div class="sub">…and ${nf0.format(f.distinct - f.values.length)} more values not kept</div>` : "") +
        (f.other_n ? `<div class="sub">${nf0.format(f.other_n)} more not told apart (too many distinct values)</div>` : "");
    const why = !owner || state.data.imported || SNAP ? "" : !can ? " · a warning has no lines of its own, so it can't filter by these"
        : f.unfiltered ? " · every value listed, not only the picked ones" : "";
    return `<div class="factbox" data-factbox="${esc(f.detail)}"><div class="facthead"><b>${esc(f.detail)}</b>` +
        `<span class="sub">${nf0.format(f.distinct)}${f.other_n ? "+" : ""} distinct · ${nf0.format(f.total)} in all${why}</span></div>` +
        `<div class="table-wrap factwrap"><table class="facttbl static"><thead><tr><th class="l">value</th>${logins ? `<th class="l">accounts logged in from it</th>` : ""}` +
        `<th>count</th><th data-tip="Count divided by all the values of this detail.">share</th>` +
        (sig ? `<th data-tip="${esc(sigTip(f.sig))}">of ${esc(f.sig.unit)}</th>` : "") +
        (avg ? `<th data-tip="${esc(AVG_TIP)}">avg ms</th>` : "") + (only ? `<th></th>` : "") + `</tr></thead><tbody>${rows}</tbody></table></div>` +
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
    const can = owner && !state.data.imported && !SNAP;
    return `<div class="sub srcnote">${note || `The ${what} collapses these into one row; most frequent first.`}` +
        (can ? ` <b>Click to filter.</b>` : "") + `</div><div class="factgrid">` + facts.map(f => factTable(f, owner)).join("") + `</div>`;
}
