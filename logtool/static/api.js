"use strict";

/* Talking to the server: api() (in an Export HTML page: the answers built into it), the file list (refresh,
 * upload, unload, resetAll), the analysis of the selection (analyzeNow), the export downloads.
 * One of the page's scripts: templates/index.html lists them in load order (see ARCHITECTURE.md). */

/* ---------------------------------------------------------------------- *
 * API
 * ---------------------------------------------------------------------- */
// An Export HTML page (see export_html in web/export_html.py) has no server: what it built in answers instead
const SNAP = (el => el && JSON.parse(el.textContent))($("#snapshot"));
function snapApi(url) {
    // the path is read from the request itself, never resolved against the page's own address: on Windows that is
    // file:///C:/..., which would turn "/api/analyze" into "/C:/api/analyze"
    const [path, query] = url.split("?"), q = new URLSearchParams(query);
    const key = path === "/api/issue_detail" ? ["issue", q.get("level"), q.get("logger"), q.get("msg")] : path === "/api/slowq" ? ["slowq", q.get("id")] : [q.get("by"), q.get("name")];
    if (path === "/api/analyze") return structuredClone(SNAP.analyze);
    if (path === "/api/timeline" && SNAP.focus) return structuredClone(SNAP.focus);
    if (["/api/detail", "/api/issue_detail", "/api/slowq"].includes(path) && SNAP.details[key.join("\x1f")]) return SNAP.details[key.join("\x1f")];
    throw new Error("not in this HTML export, which carries the details of the busiest rows of each table and of every cron, automation and module-loading operation only");
}

async function api(url, opts) {
    if (SNAP) return snapApi(url);
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
            factFilters: [], extraDomain: null, ips: [], histTags: [], issueTags: [], sqTags: [], totalSeries: defaultSeries("total")});
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
    state.ips.forEach(ip => p.append("ip", ip));
    return p;
}

// Downloads an export of the current selection: the summary, scrubbed of database / file names and raw log
// lines (see EXPORT_NOTE in web/export_json.py), or the page itself as a standalone file (see web/export_html.py)
async function exportAs(src, fallback) {
    busy("export", "Exporting…");
    try {
        const r = await fetch(src);
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
        const name = (/filename="([^"]+)"/.exec(r.headers.get("Content-Disposition") || "") || [])[1] || fallback;
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

// Export HTML: the selection plus what the page shows on top of it (the server adds the focused timeline)
function exportHtml() {
    const p = selectionParams();
    if (pinsParam()) p.set("pins", pinsParam());
    p.set("view", JSON.stringify(viewState()));
    return exportAs("/api/export_html?" + p, "logtool-export.html");
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
        for (const t of state.sqTags) if (sqData().labels[t.shape]) t.label = sqLabel(sqData().labels[t.shape]);   // a domain names shapes by id only
        // the server picks the databases on the first load and when every selected one has gone away
        if (!state.dbInit || j.excluded_db.join("\n") !== [...sent].sort().join("\n")) state.excluded = new Set(j.excluded_db);
        const known = new Set(j.databases.map(d => d[0]));
        if (known.size) state.excluded.forEach(d => known.has(d) || state.excluded.delete(d));   // gone with their file
        state.dbInit = true;
        const note = carryNotice || fileProblems() ||
            (j.imported ? `Imported summary — read-only, no database or time-interval filtering${j.hist_key ? "" : " or histogram"}.` : "");
        carryNotice = "";
        if (SNAP && !note) setNotice(SNAP.note);
        else setNotice(note, !!note);
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
    if (!files.length || SNAP) return;
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
            anchor: null, interval: null, filter: "", histTags: [], issueTags: [], sqTags: [], histSize: "auto", histFit: true,
            totalSeries: {bars: new Set(["req"]), lines: new Set(["avg_all"])}, focus: null, focusHist: null, factFilters: [], extraDomain: null, ips: [],
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
