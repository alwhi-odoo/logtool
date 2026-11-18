"use strict";

/* The raw log lines of the open form: the occurrences to pick from, the lines around the picked one.
 * One of the page's scripts: templates/index.html lists them in load order (see ARCHITECTURE.md). */

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

// what a line around the picked occurrence is to it (see mark_context in parser/scan.py), in color order
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
