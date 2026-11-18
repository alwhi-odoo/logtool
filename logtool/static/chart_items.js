"use strict";

/* Chart items: search tags, pinned errors / warnings and slow queries, the "Chart items & series" menu,
 * the click on a table row that opens its details.
 * One of the page's scripts: templates/index.html lists them in load order (see ARCHITECTURE.md). */

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

// a slow-query shape as a chart item: its runs over time
const sqLabel = text => text.length > 40 ? text.slice(0, 39) + "…" : text;
const isSqPicked = shape => state.sqTags.some(t => t.shape === shape);
function toggleSqTag(shape) {
    const before = pickedCount(), i = state.sqTags.findIndex(t => t.shape === shape);
    if (i >= 0) state.sqTags.splice(i, 1);
    else {
        state.sqTags.push(Object.assign({shape, label: sqLabel(sqData().labels[shape] || shape)}, defaultSeries("slowq"), {hue: nextHue()}));
    }
    itemsChanged(before);
    if (state.data && !state.hidden.table && state.tableGroup === "slowq") renderTable(true);
}

function removeItem(id) {
    if (id[0] === "t") return removeHistTag(+id.slice(1));
    if (id[0] === "i") return removeIssueTag(+id.slice(1));
    if (id[0] === "q") return toggleSqTag(state.sqTags[+id.slice(1)].shape);
}

function removeAllItems() {
    const before = pickedCount();
    state.histTags = [];
    state.issueTags = [];
    state.sqTags = [];
    markPicked();
    itemsChanged(before);
    if (state.data && !state.hidden.table) renderTable();
    if (state.data && !state.hidden.issues) renderIssues();
}

// ---- "Chart items & series": one row per item, one checkbox per series it can plot as bars or lines ---------
const ITEM_KIND = {total: "everything selected", tag: "", issue: "error / warning", slowq: "slow query"};
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
        if (!seriesAllowed(it, key)) return `<td class="na" data-tip="${it.kind === "slowq" ? "A slow query's time is not split into SQL and Python." : "An error or warning only has occurrences, no processing time."}">–</td>`;
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
    const sq = e.target.closest("#tbl tbody tr[data-sq]");
    if (sq) return openSlowq(sq.dataset.sq);
    const sel = e.target.closest("#selTbl tbody tr[data-sel-item]"), it = sel && itemById(sel.dataset.selItem);
    if (!it) return;
    if (it.kind === "issue") return openIssue(it.tag);
    if (it.kind === "slowq") {                                          // its longest run
        const r = sqData().rows.find(x => x.shape === it.tag.shape);
        return r && openSlowq(r.id);
    }
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
