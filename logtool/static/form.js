"use strict";

/* The full-screen form every details popup opens in: title, sheet, tabs, Back, Escape.
 * One of the page's scripts: templates/index.html lists them in load order (see ARCHITECTURE.md). */

/* ---- full-screen form of one row (operation / app / type, or a warning / error), in tabs ---------- */
let form = null;                 // what is open: {kind: "detail", by, name} | {kind: "issue", key: [level, logger, message]}
let formTab = "details";         // the open tab, kept while moving from one form to another when the next one has it
let formStack = [];              // forms opened before this one (Back returns to them): [{...form, tab}]
let restoringForm = false;       // re-opening a form (Back, or new data after a filter change): not a new visit
let detailSeq = 0;

const formOwner = () => !form || form.kind === "slowq" ? null : form.kind === "issue" ? {by: "issue", name: form.key} : {by: form.by, name: form.name};

// sheet: the row's own numbers, always shown above the tabs (like an Odoo form's sheet)
// one icon per kind of row, in the form's title
const FORM_ICON = {
    operation: `<path d="M13 2L4 14h7l-1 8 9-12h-7z" fill="none" stroke="#6ea0ff" stroke-width="2" stroke-linejoin="round"/>`,
    app: `<g fill="none" stroke="#b083e8" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></g>`,
    family: `<g fill="none" stroke="#6ec1c9" stroke-width="2" stroke-linejoin="round"><path d="M3 12V4a1 1 0 0 1 1-1h8l9 9-9 9z"/><circle cx="8" cy="8" r="1.6"/></g>`,
    WARNING: `<g fill="none" stroke="#e6c15a" stroke-width="2" stroke-linejoin="round"><path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18v.5" stroke-linecap="round"/></g>`,
    ERROR: `<g fill="none" stroke="#ef5350" stroke-width="2"><circle cx="12" cy="12" r="9.5"/><path d="M8.5 8.5l7 7M15.5 8.5l-7 7" stroke-linecap="round"/></g>`,
    slowq: `<g fill="none" stroke="#ef8a5a" stroke-width="2"><ellipse cx="12" cy="5.5" rx="8" ry="3"/><path d="M4 5.5v13c0 1.7 3.6 3 8 3s8-1.3 8-3v-13M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/></g>`,
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
    try { return f.kind === "issue" ? openIssue({level: f.key[0], logger: f.key[1], message: f.key[2]}) : f.kind === "slowq" ? openSlowq(f.id) : openDetail(f.by, f.name); }
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
    if (!$("#reqHistModal").hidden) { $("#reqHistModal").hidden = true; return; }
    if (!$("#histSeriesMenu").hidden) { $("#histSeriesMenu").hidden = true; return; }
    if (!$("#modal").hidden) closeModal();
});
