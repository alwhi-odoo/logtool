"use strict";

/* Loaded last: the right-clicks that pin a row to the chart, every click / input / change / drag-and-drop
 * handler, then the start of the page.
 * One of the page's scripts: templates/index.html lists them in load order (see ARCHITECTURE.md). */

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

document.addEventListener("contextmenu", e => {                          // right-click a slow query: pin the runs of its shape
    const tr = e.target.closest("#tbl tbody tr[data-sq]"), r = tr && sqData().rows.find(x => x.id === tr.dataset.sq);
    if (!r || !state.data.hist_key) return;
    e.preventDefault();
    const was = isSqPicked(r.shape);
    toggleSqTag(r.shape);
    showTip(`${was ? "Removed" : "Added"} the runs of this query (${nf0.format(r.n)}) ${was ? "from" : "to"} the histogram`, e.clientX, e.clientY);
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
    if ((el = t.closest("[data-only-ip]"))) return setIps([el.dataset.onlyIp]);
    if (t.closest("#clearIp")) return setIps([]);
    if ((el = t.closest("tr[data-fact]"))) {
        const o = formOwner();
        if (o && !getSelection().toString()) toggleFact(o.by, o.name, el.dataset.fact, el.dataset.v);
        return;
    }
    if ((el = t.closest("[data-dur]"))) { const o = formOwner(); return o && toggleFact(o.by, o.name, DUR_FACT, el.dataset.dur); }
    if ((el = t.closest("[data-ff-rm]"))) { const o = formOwner(); return o && toggleFact(o.by, o.name, el.dataset.ffRm, el.dataset.v); }
    if (t.closest("#ffClearForm")) { const o = formOwner(); return o && clearFacts(o.by, o.name); }
    if (t.closest("#clearFacts")) return openFfModal();
    if (t.closest("#ffClose") || t.id === "ffModal") { $("#ffModal").hidden = true; return; }
    if (t.closest("#reqHistClose") || t.id === "reqHistModal") { $("#reqHistModal").hidden = true; return; }
    if (t.closest("#reqHistOpen")) return openReqHist();
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
    if ((el = t.closest("[data-sqsort]"))) {
        const k = el.dataset.sqsort;
        state.sqSort = state.sqSort.key === k ? {key: k, dir: -state.sqSort.dir} : {key: k, dir: k === "db" || k === "head" ? 1 : -1};
        return renderTable();
    }
    if (t.closest("#sqOpen")) {
        $("#tableGroup").value = state.tableGroup = "slowq";
        state.filter = $("#filter").value = "";
        renderTable();
        return $("#tablePanel").scrollIntoView({behavior: "smooth", block: "start"});
    }
    if ((el = t.closest("[data-sq-open]"))) return openSlowq(el.dataset.sqOpen);
    if ((el = t.closest("[data-run-op]"))) return openDetail("operation", el.dataset.runOp);
    if ((el = t.closest("[data-sql-mode]"))) {
        form.layout = el.dataset.sqlMode === "1";
        return renderSqlPane();
    }
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
    if (t.closest("#exportBtn")) return exportAs("/api/export?" + selectionParams(), "logtool-export.json");
    if (t.closest("#exportHtmlBtn")) return exportHtml();
    if (t.closest("#exportTxtBtn")) return exportAs("/api/export_text?" + selectionParams(), "logtool-export.txt");
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
if (SNAP) {                                       // an Export HTML page: open as the page was when it was exported
    applyView(SNAP.view);
    const iv = SNAP.analyze.interval;
    state.interval = iv && {from: iv[0], to: iv[1]};
    state.dbInit = true;
    $("#tFrom").readOnly = $("#tTo").readOnly = true;
    analyzeNow();
} else {
    api("/api/only_db").then(j => { onlyDb = j.db || ""; renderOnlyDb(); renderDbs(); }).catch(() => {});
    refresh().catch(e => setNotice("Could not reach the server: " + e.message, true));
}
