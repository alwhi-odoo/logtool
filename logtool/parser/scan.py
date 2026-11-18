"""Raw log lines, re-read on demand: the occurrences of one warning or operation, and the lines around one.

The parser keeps only aggregate counts and one first-seen sample per issue -- every other line, issue or not,
is discarded once it has been counted, so a log of any size fits in memory. Showing the file's own context
around one particular occurrence therefore means re-reading the file, on demand, right when the details popup
asks for it (never during the main parse). The scanners replay what `Profile.feed` does (claims, filters), so
the lines they list are the ones the counts count.
"""

from __future__ import annotations

import heapq
import re
from collections import deque

from .classify import classify
from .domain_eval import dom_compile
from .facts import (
    CLIENT_FACTS, FACT_OTHER, F_DUR, F_IP, F_LOGGED_REC, F_LOGGED_USER, F_SESSION, ISSUE_MORE_LINES, LOGGED_OF,
    _client_want, _detail_facts, _facts_ok, _op_filter_ok, issue_fields, issue_occurrence_facts, job_fields,
    request_facts, request_fields,
)
from .jobs import AUTO_END, AUTO_START, JOB_LOGGERS, classify_job, cron_killed_job, cron_killed_pid, cron_start
from .lines import (
    ACCESS, ACCESS_LOGGERS, ANSI, ISSUE_LEVELS, TRACE_LEVELS, TRACE_PREFIX, _DIGITS, _db_of, _parse_header_g, _tsec,
    normalize, stamp_minute,
)
from .logio import _numbered_lines, _only_db_lines


_CLASS_CACHE = {}       # (method, path) -> (family, detail); paths repeat a lot in real logs


def _wants_logged(ff):
    """Do these fact filters need what a request's own lines named (see Profile._logged_facts)?"""
    if not ff:
        return False
    if ff.get("extra"):
        return True
    return any(F_LOGGED_USER in want or F_LOGGED_REC in want
               for by in ("operation", "app", "family") for want in ff.get(by, {}).values())


def scan_issue_occurrences(path, level, logger, template, exclude_db, lo, hi, limit, ff=None, piece=None, only_db=None):
    """Every line in `path` (any codec opener() handles) matching one issue -- same level, logger, and
    normalized message template as _issue() computes -- as a list of {"line": <1-based line number>,
    "stamp": ...}, in file order, up to `limit`. `exclude_db` and the optional `lo`/`hi` (seconds, `hi`
    exclusive) narrow it the same way the issue's own count is already narrowed, so the two agree. So do the
    filters `ff` (args.fact_filter's shape), replaying what the parser does with them: the issue's own detail
    filter and the custom domain are checked once its traceback is read, and an occurrence then waits for the
    request / cron run its worker ends next (see Profile._claim): logged during a run the filters leave out,
    it is left out too; a client IP / session filter is checked against that request. `piece`: read only one
    byte range of a plain file (see _numbered_lines) -- the parser's own chunk, so the claims match it."""
    ff = ff or {}
    xp = dom_compile(ff["extra"]) if ff.get("extra") else None
    want = ff.get("issue", {}).get((level, logger, template)) or {}
    client = _client_want(ff, want)
    own = {k: v for k, v in want.items() if k not in CLIENT_FACTS}
    track = bool(ff)

    def keep(msg, more):
        have = issue_occurrence_facts(msg, more)
        return (not own or _facts_ok(own, have)) and (not xp or xp(issue_fields(level, logger, template, msg, have)))
    out = []
    held = None                                  # (occurrence, message, continuation lines, worker, seconds)
    waiting = {}                                 # worker -> [(seconds, occurrence)]: waiting for the run they were logged in
    kids, cur, cur_left = {}, None, 0            # worker -> [(seconds, facts)] for what runs inherit (Profile._logged_facts)

    def settle(lst, during=False):              # nobody claims these (any more): parentless, unless a client was asked for
        if not client and not during:
            out.extend(occ for _t, occ in lst)
    try:
        with _numbered_lines(path, piece) as numbered:
            if only_db:
                numbered = _only_db_lines(numbered, only_db)
            for i, raw in numbered:
                if len(out) >= limit and not track:
                    break
                g = _parse_header_g(raw)
                if g is None:
                    if held is not None and len(held[2]) < ISSUE_MORE_LINES:
                        held[2].append(raw)
                    if cur is not None and cur_left > 0:
                        _detail_facts(cur, raw)
                        cur_left -= 1
                    continue
                cur = None
                if held is not None:
                    if keep(held[1], held[2]):
                        if track:
                            wl = waiting.setdefault(held[3], [])
                            wl.append((held[4], held[0]))
                            if len(wl) > 200:                   # the parser keeps no more pending per worker
                                settle(wl[:100])
                                del wl[:100]
                        else:
                            out.append(held[0])
                    held = None
                date, tm, lvl, db, lg, msg = g["date"], g["time"], g["level"], _db_of(g), g["logger"], g["msg"]
                if exclude_db and db in exclude_db:        # the parser leaves these out before any claim, so does this
                    continue
                pid = g.get("proc") or "?"
                t = None
                if track:
                    try:
                        t = _tsec(stamp_minute(date, tm), tm, g.get("ms"))
                    except ValueError:
                        continue
                    lst = kids.setdefault(pid, [])
                    if lvl in ISSUE_LEVELS:
                        cur, cur_left = {}, ISSUE_MORE_LINES
                        _detail_facts(cur, msg[:2000])
                        lst.append((t, cur))
                    elif lvl in TRACE_LEVELS and (lg == "odoo.api" or lg.startswith(TRACE_PREFIX)):
                        d = {}
                        _detail_facts(d, msg)
                        if d:
                            lst.append((t, d))
                    if len(lst) > 200:
                        del lst[:100]
                    op = _line_operation(lg, msg)
                    if op is not None and (op[0] == "cron" or lg in ACCESS_LOGGERS or lg.endswith(".werkzeug")):
                        start = t - (op[3] or 0.0) - 1.0
                        logged = {}
                        for ts, facts in lst:
                            if ts >= start:
                                for mine, nm in LOGGED_OF:
                                    if facts.get(mine):
                                        logged.setdefault(nm, set()).update(v for v in facts[mine] if v != FACT_OTHER)
                        lst.clear()
                        passes = _op_filter_ok(ff, op[0], op[1], op[2], dict(op[4], **logged)) and \
                            (not xp or xp(dict(op[5], **logged)))
                        mine_waiting = waiting.pop(pid, [])
                        settle([w for w in mine_waiting if w[0] < start])
                        during = [w for w in mine_waiting if w[0] >= start]
                        if not passes:
                            settle(during, during=True)
                        else:
                            m = ACCESS.match(msg) if op[0] != "cron" else None
                            have = {}
                            if m:
                                have[F_IP] = {m.group("ip")}
                                if m.group("ident") != "-":
                                    have[F_SESSION] = {m.group("ident")}
                            if not client or _facts_ok(client, have):
                                out.extend(occ for _t, occ in during)
                if lvl != level or lg != logger:
                    continue
                if normalize(msg[:140]) != template:
                    continue
                if lo is not None:
                    try:
                        secs = stamp_minute(date, tm[:5]) * 60 + int(tm[6:8])
                    except ValueError:
                        continue
                    if secs < lo or secs >= hi:
                        continue
                occ = {"line": i, "stamp": "%sT%s" % (date, tm)}
                if track:
                    held = (occ, msg, [], pid, t)
                else:
                    out.append(occ)
            if held is not None and keep(held[1], held[2]):
                if track:
                    waiting.setdefault(held[3], []).append((held[4], held[0]))
                else:
                    out.append(held[0])
            for lst in waiting.values():
                settle(lst)
    except OSError:
        pass
    return sorted(out, key=lambda o: o["line"])[:limit]


def _line_operation(logger, msg):
    """(type, app, operation, seconds, facts) of a timed line -- an access line or a job's own message --
    grouped exactly as the parser groups it, else None. facts: what the line carries, for fact filters."""
    if logger in ACCESS_LOGGERS or logger.endswith(".werkzeug"):
        m = ACCESS.match(msg)
        if not m:
            return None
        path, method, status = m.group("path", "method", "status")
        key = (method, path, status == "404")
        cls = _CLASS_CACHE.get(key)
        if cls is None:
            if len(_CLASS_CACHE) > 200000:
                _CLASS_CACHE.clear()
            cls = _CLASS_CACHE[key] = classify(method, path, status)
        fam, app, det = cls
        qt, ot = m.group("qt", "ot")
        qt, ot = float(qt or 0), float(ot or 0)
        if status == "101" or path.startswith("/longpolling/poll"):
            ot = 0.0                                    # held open, not working: as Profile._request counts it
        collapsed = "?" in path or _DIGITS.search(path) or "<" in det or "…" in det or "/*" in det
        fields = request_fields(fam, app, det, status, method, qt, ot, int(m.group("nq") or 0), m.group("ip"), m.group("ident"), path)
        return (fam, app, det, qt + ot if m.group("qt") else None,
                request_facts(m.group("ip"), m.group("ident"), path, collapsed, qt + ot), fields)
    if logger.endswith(JOB_LOGGERS):
        job = classify_job(logger, msg)
        if job:
            return _job_operation(job)
        m = AUTO_END.match(msg) if logger.endswith("base_automation") else None
        if m:
            det = "automation: %s (time-based)" % m.group("name")
            return "automation", "base_automation", det, None, {}, job_fields("automation", "base_automation", det, 0.0, 0, ())
    return None


def _job_operation(job):
    """_line_operation()'s answer for a classify_job() answer."""
    have = {F_DUR: job[3]}
    for slot, v in job[6]:
        have.setdefault(slot, set()).add(v)
    return job[0], job[1], job[2], job[3], have, job_fields(job[0], job[1], job[2], job[3], job[4], job[6])


_TIMING_TAIL = re.compile(r'" \d{3} \S+ \d+ ([\d.]+) ([\d.]+)')       # an access line's status .. SQL s, other s


def scan_op_occurrences(path, by, name, exclude_db, lo, hi, limit, slowest=False, ff=None, piece=None, min_seconds=None,
                        only_db=None, carried=None):
    """The timed lines in `path` of one operation, app or type (`by` is "operation", "app" or "family"; `name`
    None: of every one), as [{"line", "stamp", "seconds"}]: the first `limit` in file order, or with `slowest` the `limit` slowest
    (which reads the whole file). `exclude_db` and `lo`/`hi` narrow it like scan_issue_occurrences, and
    `ff` (args.fact_filter's shape) leaves out the lines the fact filters leave out of the counts. min_seconds:
    with `slowest`, lines that took less are passed over on a cheap look at their timing, before any parsing
    (the caller knows roughly how slow the slowest `limit` are, see api_op_lines). carried: {(worker, kill time):
    (cron name, cron id, start, database)} of the runs killed in another piece than they started in (see
    pair_cron_kills), which this piece alone cannot pair."""
    col = {"family": 0, "app": 1, "operation": 2}[by]
    xp = dom_compile(ff["extra"]) if ff and ff.get("extra") else None
    track = _wants_logged(ff)        # replay what the parser does: each request inherits what its worker logged meanwhile
    quick = min_seconds if slowest and min_seconds and not track else None
    kids, cur, cur_left = {}, None, 0          # worker -> [(seconds, facts)]; the open warning's facts
    auto = {}                                    # (worker, rule) -> when its time-based run started
    crons = {}                                   # worker -> (cron name, cron id, start, database): its unfinished run
    out, heap, n = [], [], 0
    try:
        with _numbered_lines(path, piece) as numbered:
            if only_db:
                numbered = _only_db_lines(numbered, only_db)
            for i, raw in numbered:
                if not slowest and len(out) >= limit:
                    break
                logged = {}
                if quick is not None:                             # too fast to be among the slowest: not even parsed
                    tm_ = _TIMING_TAIL.search(raw)
                    if tm_ is None or float(tm_.group(1)) + float(tm_.group(2)) < quick:
                        continue
                g = _parse_header_g(raw)
                if g is None:
                    if cur is not None and cur_left > 0:          # the body / traceback of the warning above
                        _detail_facts(cur, raw)
                        cur_left -= 1
                    continue
                cur = None
                date, tm, lvl, db, lg, msg = g["date"], g["time"], g["level"], _db_of(g), g["logger"], g["msg"]
                killed = cron_killed_pid(lg, msg) if lvl == "ERROR" else None
                run = crons.pop(killed, None) if killed else None
                if run is None and killed and carried:
                    try:
                        run = carried.get((killed, _tsec(stamp_minute(date, tm), tm, g.get("ms"))))
                    except ValueError:
                        pass
                if run is None and exclude_db and db in exclude_db:   # the parser leaves these out before any claim, so does this
                    continue
                pid = g.get("proc") or "?"
                op = _line_operation(lg, msg)
                if run is not None or (lg.endswith("ir_cron") and (op is None or op[0] == "cron")):
                    try:                                          # cron runs whose worker is killed, as the parser pairs them
                        t = _tsec(stamp_minute(date, tm), tm, g.get("ms"))
                    except ValueError:
                        continue
                    if run is not None:                           # the kill line stands for the run, in its database
                        pid, db = killed, run[3]
                        op = _job_operation(cron_killed_job(run[0], run[1], max(0.0, t - run[2])))
                    elif op is not None:
                        crons.pop(pid, None)
                    else:
                        cs = cron_start(msg)
                        if cs:
                            crons[pid] = cs + (t, db)
                if lg.endswith("base_automation") and (op is None or op[3] is None):
                    try:                                          # time-based rules: "Starting" .. "done", as the parser pairs them
                        t = _tsec(stamp_minute(date, tm), tm, g.get("ms"))
                    except ValueError:
                        continue
                    ms = AUTO_START.match(msg)
                    if ms:
                        auto[(pid, ms.group("name"))] = t
                    elif op is not None:
                        t0 = auto.pop((pid, AUTO_END.match(msg).group("name")), None)
                        if t0 is None or t < t0:
                            op = None                             # unpaired: the parser does not count it either
                        else:
                            op = op[:3] + (t - t0, op[4], dict(op[5], duration=t - t0))
                if track:
                    try:
                        t = _tsec(stamp_minute(date, tm), tm, g.get("ms"))
                    except ValueError:
                        continue
                    lst = kids.setdefault(pid, [])
                    if lvl in ISSUE_LEVELS:
                        cur, cur_left = {}, ISSUE_MORE_LINES
                        _detail_facts(cur, msg[:2000])
                        lst.append((t, cur))
                    elif lvl in TRACE_LEVELS and (lg == "odoo.api" or lg.startswith(TRACE_PREFIX)):
                        d = {}
                        _detail_facts(d, msg)
                        if d:
                            lst.append((t, d))
                    if len(lst) > 200:
                        del lst[:100]
                    if op is not None and (op[0] == "cron" or lg in ACCESS_LOGGERS or lg.endswith(".werkzeug")):
                        start = t - (op[3] or 0.0) - 1.0         # what a request / cron run claims, as _claim does
                        for ts, facts in lst:
                            if ts >= start:
                                for mine, nm in LOGGED_OF:
                                    if facts.get(mine):
                                        logged.setdefault(nm, set()).update(v for v in facts[mine] if v != FACT_OTHER)
                        lst.clear()
                if op is None or (name is not None and op[col] != name):
                    continue
                if ff and (not _op_filter_ok(ff, op[0], op[1], op[2], dict(op[4], **logged)) or
                           (xp and not xp(dict(op[5], **logged)))):
                    continue
                if lo is not None:
                    try:
                        secs = stamp_minute(date, tm[:5]) * 60 + int(tm[6:8])
                    except ValueError:
                        continue
                    if secs < lo or secs >= hi:
                        continue
                occ = {"line": i, "stamp": "%sT%s" % (date, tm), "seconds": op[3], "family": op[0],
                       "operation": op[2] if by != "operation" or name is None else None, "db": db}
                if not slowest:
                    out.append(occ)
                    continue
                n += 1
                item = (op[3] or 0.0, -i, n, occ)          # -i: of equally slow lines keep the earliest
                if len(heap) < limit:
                    heapq.heappush(heap, item)
                elif item > heap[0]:
                    heapq.heapreplace(heap, item)
    except OSError:
        pass
    return out if not slowest else [e[3] for e in sorted(heap, reverse=True)]


def occurrence_context(path, lines, dbs, around):
    """{line number: [(line number, line), ...]}: each of `lines` (line numbers in `path`) with the `around` lines
    before and after it, counting only the entries (header and continuation lines) of databases `dbs` -- the other
    databases' lines are skipped, never shown."""
    want, out, before, after = set(lines), {}, deque(maxlen=around), []
    skip = True                                  # continuation lines before the first header: no database known
    with _numbered_lines(path) as numbered:
        for i, raw in numbered:
            g = _parse_header_g(raw)
            if g is not None:
                skip = _db_of(g) not in dbs
            if skip:
                continue
            line = (i, ANSI.sub("", raw).rstrip("\r\n"))
            for e in after:
                e[0].append(line)
                e[1] -= 1
            after = [e for e in after if e[1] > 0]
            if i in want:
                out[i] = list(before) + [line]
                if around:
                    after.append([out[i], around])
            before.append(line)
            if not after and len(out) == len(want):
                break
    return out


def mark_context(lines, match_at, by=None, name=None, issue=None):
    """What each line around an occurrence is to it, for the log viewer: "main" (the occurrence and its
    traceback), "same" (other occurrences of the same operation / app / type -- `by`, `name` -- or of the
    same warning -- `issue`: (level, logger, template)), "parent" (the request / cron run it ran inside: the
    next one its worker ended whose run covers it, as Profile._claim matches them), "during" (what the
    worker logged while the occurrence -- a request or job -- ran), else None. A continuation line has the
    mark of the header line above it."""
    col = {"family": 0, "app": 1, "operation": 2}.get(by)
    hdrs = []                                   # (index, worker, seconds, level, logger, msg, timed operation)
    for i, raw in enumerate(lines):
        g = _parse_header_g(raw)
        if g is None:
            continue
        try:
            t = _tsec(stamp_minute(g["date"], g["time"]), g["time"], g.get("ms"))
        except ValueError:
            continue
        hdrs.append((i, g.get("proc") or "?", t, g["level"], g["logger"], g["msg"], _line_operation(g["logger"], g["msg"])))
    main = next((h for h in hdrs if h[0] == match_at), None)
    marks = [None] * len(lines)
    if main is None:
        marks[match_at] = "main"
        return marks
    _, pid, t0, _lvl, _lg, _msg, op0 = main
    claims = lambda h: h[6] is not None and (h[6][0] == "cron" or h[4] in ACCESS_LOGGERS or h[4].endswith(".werkzeug"))   # noqa: E731
    start = t0 - (op0[3] or 0.0) - 1.0 if op0 is not None else None
    parent = None
    if not (op0 is not None and claims(main)):  # a request / cron run is no one's child
        parent = next((h for h in hdrs if h[0] > match_at and h[1] == pid and claims(h)), None)
        if parent is not None and t0 < parent[2] - (parent[6][3] or 0.0) - 1.0:
            parent = None                       # that run started after this line: not its parent
    kind = {}
    for h in hdrs:
        i, p, t, lvl, lg, msg, op = h
        if i == match_at:
            kind[i] = "main"
        elif parent is not None and i == parent[0]:
            kind[i] = "parent"
        elif (col is not None and op is not None and op[col] == name) or \
                (issue is not None and (lvl, lg) == tuple(issue[:2]) and normalize(msg[:140]) == issue[2]):
            kind[i] = "same"
        elif start is not None and p == pid and start <= t <= t0 and not claims(h):
            kind[i] = "during"
    cur, starts = None, {h[0] for h in hdrs}
    for i in range(len(lines)):
        if i in starts:
            cur = kind.get(i)
        elif i == match_at:
            cur = "main"
        marks[i] = cur
    return marks


def read_context(path, line_no, before, after, piece=None):
    """The raw lines around line number `line_no` (1-based) in `path`: up to `before` lines before it,
    the line itself, and up to `after` lines after. {"lines": [...], "match_at": <index of line_no in
    lines>}. A single early-exiting pass -- the file is never read past what was asked for. `piece`
    (start byte, None, its first line number) starts reading there instead of at the top of the file."""
    buf = deque(maxlen=before)
    out, match_at = [], 0
    try:
        with _numbered_lines(path, piece) as numbered:
            for i, raw in numbered:
                raw = raw.rstrip("\n")
                if i < line_no:
                    buf.append(raw)
                elif i == line_no:
                    match_at = len(buf)
                    out = list(buf) + [raw]
                else:
                    out.append(raw)
                    if i >= line_no + after:
                        break
    except OSError:
        pass
    return {"lines": out, "match_at": match_at}
