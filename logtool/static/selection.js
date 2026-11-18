"use strict";

/* The filter row: files, databases, the time interval, the histogram's timezone, the notice line, the
 * database to read (only_db) and the scan of a file for its database names.
 * One of the page's scripts: templates/index.html lists them in load order (see ARCHITECTURE.md). */

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
    if (!state.data || SNAP) return;
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
    const none = !state.files.length && !SNAP;
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

// Export text holds one database and the lines without one (see export_text in web/export_text.py)
const TXT_TIP = "Downloads the current selection as a plain-text report: the requests histogram, every operation with " +
    "its calls, average time and the details behind it, then each occurrence's log line with the 3 lines before and after it, oldest first. " +
    "Its raw lines are the selected database's own: lines of other databases and lines without one are never in it.";
function renderExportTxt() {
    const b = $("#exportTxtBtn"), dbs = state.data ? state.data.databases : [];
    b.hidden = !state.data || state.data.imported;
    const many = dbs.filter(d => d[0] !== "-" && !state.excluded.has(d[0])).length > 1;
    b.disabled = many;
    b.dataset.tip = many ? "Export text holds one database (and the lines without one): select only one database to export." : TXT_TIP;
}

function renderDbs() {
    renderExportTxt();
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
            `title="${esc(n === "-" ? "Log lines that carry no database name" : n)} · ${nf0.format(c)} log lines${SNAP ? "" : ` - click to ${off ? "include" : "exclude"}`}">${esc(dbLabel(n))}</button>` +
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
