"""Profile: the accumulator. `feed()` reads log lines and counts every request, job, warning and error.

One Profile per parsed piece (with args.split_db, one child per database in `.parts`); `merge()` folds pieces
together. `feed()` is the hot loop of the whole tool: every name it uses is a local or a global of this module
(imported by name below, never looked up through another module). Keep it that way.
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict

from .bucket import Bucket
from .classify import classify, event_family
from .domain_eval import extra_pred
from .facts import (
    CLIENT_FACTS, CLIENT_KEEP, CLIENT_OTHER, FACT_LEN, FACT_OTHER, F_DUR, F_IP, F_SESSION, F_URL, ISSUE_MORE_LINES,
    LOGGED_OF, _client_want, _detail_facts, _fact, _fact_time, _facts_ok, _num_facts, _op_filter_ok, issue_fields,
    issue_occurrence_facts, job_fields, merge_fact_times, merge_facts, request_facts, request_fields,
)
from .jobs import (
    AUTO_END, AUTO_START, CRON_KILLED_LOGGER, JOB_LOGGERS, classify_job, cron_killed_job, cron_killed_pid, cron_start,
)
from .lines import (
    ACCESS, ACCESS_LOGGERS, ANSI, DATEISH, EXC_LINE, HEADERS, ISSUE_LEVELS, LOGIN_MSG, SERVER_ACTION, TRACE_CALL,
    TRACE_KEEP, TRACE_LEVELS, TRACE_LINE, TRACE_PREFIX, _DIGITS, _norm_head, _tsec, normalize, stamp_minute,
)
from .report import as_rows, write_report


_REQ_CACHE = {}         # (method, path, 404?) -> (type, app, operation, collapsed): an access line's grouping


class Profile:
    def __init__(self, args):
        self.a = args
        self._ff = getattr(args, "fact_filter", None) or None    # item filters / custom domain (see _facts_ok), read per request
        self.rng = random.Random(12345)
        self.fam = defaultdict(Bucket)           # by type   (ui.read, api.write, cron, ...)
        self.app = defaultdict(Bucket)           # by app    (sale, stock, website_sale, ...)
        self.det = defaultdict(Bucket)           # by operation
        self.det_fam = {}                        # operation -> type
        self.det_app = {}                        # operation -> app
        self.events = defaultdict(Counter)       # family -> Counter[template]
        self.event_level = {}
        self.exceptions = Counter()
        self.server_actions = Counter()
        self.logins = Counter()                  # (login, ip, result) -> n
        self.status = Counter()
        self.hours = defaultdict(Bucket)
        self.unclassified = Counter()
        self.header_hits = Counter()
        self.dbs = Counter()                     # database name -> log lines
        self.lines = 0
        self.header_ok = 0
        self.continuation = 0
        self.access_ok = 0
        self.access_untimed = 0
        self.access_bad = 0
        self.held_n = 0                          # connections held open (WebSocket upgrades): not work
        self.held_s = 0.0
        self.filtered = 0
        self.t_min = None
        self.t_max = None
        self._pending_login = {}                 # pid -> (result, login)
        self._expect_exc = None                  # the profile whose ERROR line waits for its traceback's exception class
        self.index = {}                          # profile_files: path -> [(byte offset, minute)]
        self.parts = {}                          # split_db: database -> child Profile
        self._cur = None                         # split_db: child that owns continuation lines
        self._skip = bool(args.since or args.until)   # the last header was outside the time window (lines before the first one are)
        self.formats = Counter()                 # which Odoo log-format variants were recognised
        self._starts = {}                        # (worker, rule) -> start time, for time-based automation pairing
        self._cron_runs = {}                     # worker -> (Profile, cron name, cron id, start time, database): its unfinished run
        # what pairs a killed run across pieces (chunks, files; see cron_marks / pair_cron_kills):
        self._cron_first = {}                    # worker -> when it logged its first cron line (start, end, kill) here
        self._cron_kills = []                    # (worker, time, raw, stamp): kills of a run started before this piece
        self._cron_done = []                     # (worker, database, name, id, start, time, raw, stamp): runs killed here
        self.levels = Counter()                  # WARNING / ERROR / CRITICAL -> log lines
        self.issues = {}                         # (level, logger, template) -> [n, first, last, sample, exception,
                                                  #  src, hist]; hist is {minute: [n, 0.0, 0.0, 0.0]}, same shape
                                                  # as a Bucket's (see hist_add) so _rebin() reads either kind
        self._last_issue = None                  # ERROR record still waiting for its traceback's exception class
        self.issue_ops = {}                      # issue key -> {operation: count}: which request was running when it was logged
        self._pend = {}                          # worker -> [(time, profile, issue key)] logged since that worker's last request ended
        self._lpend = {}                         # worker -> [(time, profile, message, line)]: untimed lines, same idea
        self.src = {}                            # operation -> {line shape: [n, example line, runs, seconds]}: untimed lines
                                                  # logged while it ran; runs / seconds: the requests / jobs that logged it
        self.op_facts = {}                       # operation -> facts (see _fact): client IPs, sessions, raw URLs, cron ids, ...
        self.op_logged = {}                      # operation -> facts of the lines logged while it ran (see _logged_facts):
                                                  # how many of its requests named each user / record
        self.op_facts_s = {}                     # operation -> twin of op_facts / op_logged (see _fact_time): the seconds
        self.op_logged_s = {}                    # of the requests / jobs behind each value, for their average
        self._iocc = None                        # facts of the warning being read, still growing with its traceback
        self.issue_facts = {}                    # issue key -> facts: the numbers behind '#', users, records, clients
        self._ifact = None                       # (profile, issue key, lines left): continuation lines still searched for facts
        self._ihold = None                       # a warning / error held back by a fact filter until its continuation lines are read
        self.clients = {}                        # client -> {minute: [requests, seconds]}: who was active when
        self.login_seen = {}                     # (login, ip, result) -> [first stamp, last stamp]
        self.all_levels = Counter()              # every level (DEBUG, INFO, ...) -> header lines: what the log can show at all

    # -- helpers ---------------------------------------------------------- #
    def _part(self, db):
        p = self.parts.get(db)
        if p is None:
            p = self.parts[db] = Profile(self.a)
        return p

    def _in_window(self, stamp):
        if self.a.since and stamp < self.a.since:
            return False
        if self.a.until and stamp > self.a.until:
            return False
        return True

    def finalize(self):
        """Roll the operation buckets up into types and apps and total the HTTP statuses. Doing it once
        here, instead of on every request, is what keeps parsing fast. Call after the last feed / merge."""
        by_type, by_app = defaultdict(list), defaultdict(list)
        status = Counter()
        for op, b in self.det.items():
            by_type[self.det_fam.get(op, "other")].append(b)
            by_app[self.det_app.get(op, "other")].append(b)
            status.update(b.st)
        self.fam = defaultdict(Bucket, {k: Bucket.combine(v, self.rng) for k, v in by_type.items()})
        self.app = defaultdict(Bucket, {k: Bucket.combine(v, self.rng) for k, v in by_app.items()})
        self.status = status
        return self

    # -- main loop -------------------------------------------------------- #
    def merge(self, o, heavy=True):
        """Fold another Profile (e.g. from a parallel worker) into this one. heavy=False leaves out what only an
        item's details and the export read (op_facts, op_logged, src, clients): see merge_heavy."""
        for mine, theirs in ((self.det, o.det), (self.hours, o.hours)):
            for k, b in theirs.items():
                mine[k].merge(b, self.rng)
        self.det_fam.update(o.det_fam)
        self.det_app.update(o.det_app)
        for name in ("exceptions", "server_actions", "logins", "unclassified",
                     "header_hits", "dbs", "levels", "formats", "all_levels"):
            getattr(self, name).update(getattr(o, name))
        for fam, c in o.events.items():
            self.events[fam].update(c)
        self.event_level.update(o.event_level)
        for name in ("lines", "header_ok", "continuation", "access_ok", "access_untimed",
                     "access_bad", "held_n", "held_s", "filtered"):
            setattr(self, name, getattr(self, name) + getattr(o, name))
        if o.t_min is not None and (self.t_min is None or o.t_min < self.t_min):
            self.t_min = o.t_min
        if o.t_max is not None and (self.t_max is None or o.t_max > self.t_max):
            self.t_max = o.t_max
        for k, d in o.issue_ops.items():
            mine = self.issue_ops.setdefault(k, {})
            for op, n in d.items():
                mine[op] = mine.get(op, 0) + n
        for k, f in o.issue_facts.items():
            merge_facts(self.issue_facts.setdefault(k, {}), f)
        if heavy:
            self.merge_heavy(o)
        for k, (t0, t1) in o.login_seen.items():
            e = self.login_seen.get(k)
            if e is None:
                self.login_seen[k] = [t0, t1]
            else:
                e[0], e[1] = min(e[0], t0), max(e[1], t1)
        for k, e in o.issues.items():
            mine = self.issues.get(k)
            if mine is None:
                self.issues[k] = list(e[:6]) + [{m: list(v) for m, v in e[6].items()}]   # deep: mutated in place below
            else:
                mine[0] += e[0]
                if e[1] < mine[1]:
                    mine[1] = e[1]
                if e[2] > mine[2]:
                    mine[2] = e[2]
                mine[4] = mine[4] or e[4]
                mine[5] = mine[5] or e[5]
                for m, v in e[6].items():
                    h = mine[6].get(m)
                    if h is None:
                        mine[6][m] = list(v)
                    else:
                        h[0] += v[0]
        for db, child in o.parts.items():          # never adopt `child` itself: it may be cached and must stay as it is
            self._part(db).merge(child, heavy)

    def merge_heavy(self, o):
        """The part of merge() that only an item's details and the export read: what was collapsed behind each
        operation, what its lines named, the lines logged while it ran, and who was active when."""
        for op, d in o.src.items():
            mine = self.src.setdefault(op, {})
            for k, e in d.items():
                cur = mine.get(k)
                if cur is None:
                    mine[k] = list(e)
                else:
                    cur[0] += e[0]
                    cur[1] = cur[1] or e[1]
                    cur[2] += e[2]
                    cur[3] += e[3]
        for mine_d, theirs_d, mine_t, theirs_t in ((self.op_facts, o.op_facts, self.op_facts_s, o.op_facts_s),
                                                   (self.op_logged, o.op_logged, self.op_logged_s, o.op_logged_s)):
            for k, f in theirs_d.items():
                merge_facts(mine_d.setdefault(k, {}), f)
                merge_fact_times(mine_d[k], mine_t.setdefault(k, {}), theirs_t.get(k) or {})
        for c, h in o.clients.items():
            mine = self.clients.setdefault(c, {})
            for m, v in h.items():
                e = mine.get(m)
                if e is None:
                    mine[m] = list(v)
                else:
                    e[0] += v[0]
                    e[1] += v[1]

    def feed(self, fh):
        """Read log lines. With args.split_db every database gets its own child Profile
        in self.parts, so any subset of databases can be merged later without re-reading."""
        a = self.a
        want_events = a.events or a.logins      # event lines are only ever reported with these
        want_hours = a.hours
        exclude = getattr(a, "exclude_db", None)
        only = getattr(a, "only_db", None)
        split = getattr(a, "split_db", False)
        pend = self._pend
        lpend = self._lpend
        ff_issue = (getattr(a, "fact_filter", None) or {}).get("issue")
        # with any filter, a warning is decided with its traceback and with the request it was logged during: one
        # logged while a filtered-out request ran goes with it (see _claim / _drop_claims)
        hold_all = bool(getattr(a, "fact_filter", None))
        for raw in fh:
            self.lines += 1
            if "\x1b" in raw:
                raw = ANSI.sub("", raw)
            rec = None
            hname = None
            # Cheap reject for traceback/continuation lines (often >half of a real log):
            # every header format needs a leading digit or a timestamp somewhere in the line.
            if raw[:1].isdigit() or DATEISH.search(raw, 0, 200):
                for name, pat in HEADERS:
                    m = pat.match(raw)
                    if m:
                        rec = m
                        hname = name
                        break
                if rec is not None:
                    try:
                        minute = stamp_minute(rec.group("date"), rec.group("time"))
                    except ValueError:                 # 2026-02-31 and friends: not a real timestamp
                        rec = None
            if rec is None:
                # traceback / continuation / blank line
                if self._skip:                     # belongs to a header outside the time window
                    continue
                if split:                          # belongs to the database of the last header
                    c = self._cur or self._part("-")
                    c.lines += 1
                    c.continuation += 1
                else:
                    self.continuation += 1
                if self._ihold is not None:
                    hold = self._ihold
                    if len(hold[7]) < ISSUE_MORE_LINES:
                        hold[7].append(raw)
                    if hold[1] != "WARNING":           # what names an ERROR can come later than that
                        found = hold[8]
                        if not found["sa"] and "ir.actions.server" in raw:
                            sa = SERVER_ACTION.search(raw)
                            if sa:
                                found["sa"] = "server action #%s" % (sa.group(1) or sa.group(2))
                        if not found["exc"]:
                            em = EXC_LINE.match(raw.strip())
                            if em:
                                found["exc"] = em.group("exc")
                if self._ifact is not None:            # the body / traceback of a warning or error: users, records
                    t, k, left = self._ifact
                    _detail_facts(t.issue_facts.setdefault(k, {}), raw)
                    if self._iocc is not None:
                        _detail_facts(self._iocc, raw)
                    self._ifact = (t, k, left - 1) if left > 1 else None
                if self._last_issue is not None and not self._last_issue[5] and "ir.actions.server" in raw:
                    sa = SERVER_ACTION.search(raw)         # traceback frame: File "ir.actions.server(12,)"
                    if sa:
                        self._last_issue[5] = "server action #%s" % (sa.group(1) or sa.group(2))
                if self._expect_exc is not None or self._last_issue is not None:
                    em = EXC_LINE.match(raw.strip())
                    if em:
                        if self._expect_exc is not None:
                            self._expect_exc.exceptions[em.group("exc")] += 1
                            self._expect_exc = None
                        if self._last_issue is not None:       # name the ERROR after its exception
                            self._last_issue[4] = self._last_issue[4] or em.group("exc")
                            self._last_issue = None
                continue

            if hname == "odoo-nodb":
                date, tm, ms, proc, level, logger, msg = rec.groups()
                db = None
            else:
                date, tm, ms, proc, level, db, logger, msg = rec.groups()
            self._last_issue = self._ifact = self._iocc = self._expect_exc = None
            if self._ihold is not None:
                self._release_issue()
            db = db or "-"
            if db == "?" or db == "None":          # Odoo prints these when a line has no database
                db = "-"
            if only and db != only and db != "-":  # another database: this line and its continuation lines are not read at all
                self._skip = True
                self.filtered += 1
                continue
            tgt = self
            if split:
                tgt = self._cur = self._part(db)
            stamp = date + "T" + tm
            self._skip = False
            if a.since or a.until:
                if not self._in_window(stamp):
                    tgt.filtered += 1
                    self._skip = True                  # its continuation lines are outside the window too
                    continue
            if split:
                tgt.lines += 1
            tgt.header_ok += 1
            hh = tgt.header_hits
            hh[hname] = hh.get(hname, 0) + 1
            if db != "-":
                tgt.dbs[db] += 1
            if logger == CRON_KILLED_LOGGER and level == "ERROR":
                self._cron_killed(logger, msg, minute, tm, ms, raw, stamp)
            if (a.db and db != a.db) or (exclude and db in exclude):
                tgt.filtered += 1
                continue
            if tgt.t_min is None or stamp < tgt.t_min:
                tgt.t_min = stamp
            if tgt.t_max is None or stamp > tgt.t_max:
                tgt.t_max = stamp
            pid = proc or "?"
            al = tgt.all_levels
            al[level] = al.get(level, 0) + 1
            if level in ISSUE_LEVELS and (hold_all or ff_issue and (level, logger, _norm_head(msg)) in ff_issue):
                # filtered: whether it counts depends on its traceback too, so decide at the next header
                self._ihold = (tgt, level, logger, msg, stamp, pid, _tsec(minute, tm, ms), [], {"exc": "", "sa": ""})
            elif level in ISSUE_LEVELS:
                ikey, e = tgt._issue(level, logger, msg, stamp)
                self._ifact = (tgt, ikey, ISSUE_MORE_LINES)
                if level != "WARNING":
                    self._last_issue = e
                # Remember who logged it and when: the access line of the request this worker was running
                # is logged when the request ends, so it can claim everything logged during its lifetime.
                occ = self._iocc = {}                       # what this one names: the request it ran in inherits it
                _detail_facts(occ, msg[:2000])
                pl = pend.setdefault(pid, [])
                pl.append((_tsec(minute, tm, ms), tgt, ikey, occ))
                if len(pl) > 200:
                    del pl[:100]

            if logger in ACCESS_LOGGERS or logger.endswith(".werkzeug"):
                f = tgt.formats
                fk = "access: odoo.http.server (19.4+)" if logger == "odoo.http.server" else "access: werkzeug (17.0-19.0)"
                f[fk] = f.get(fk, 0) + 1
                tgt._request(msg, tm[:2] if want_hours else None, pid, minute,
                             _tsec(minute, tm, ms) if pend.get(pid) or lpend.get(pid) else 0.0, pend, lpend, raw, stamp)
                continue

            if logger.endswith(JOB_LOGGERS):                # cron / automation / module loading
                job = classify_job(logger, msg)
                if job and job[0] == "cron":
                    self._cron_runs.pop(pid, None)          # ended: its worker was not killed
                    self._cron_first.setdefault(pid, _tsec(minute, tm, ms))
                elif not job and logger.endswith("ir_cron"):
                    cs = cron_start(msg)
                    if cs:
                        t = _tsec(minute, tm, ms)
                        self._cron_runs[pid] = (tgt,) + cs + (t, db)
                        self._cron_first.setdefault(pid, t)
                if job:
                    tgt.formats[job[5]] += 1
                    tgt._job(job[:5], minute, pid, _tsec(minute, tm, ms) if pend.get(pid) or lpend.get(pid) else 0.0,
                             pend, lpend, raw, stamp, job[6])
                    continue
                if logger.endswith("base_automation"):      # time-based rules: pair "Starting" with "done"
                    tgt._automation_pair(msg, pid, _tsec(minute, tm, ms), minute)

            if level in TRACE_LEVELS and (logger == "odoo.api" or logger.startswith(TRACE_PREFIX)):
                pl = lpend.setdefault(pid, [])                 # waits for the request / job this worker is running
                pl.append((_tsec(minute, tm, ms), tgt, msg, raw))
                if len(pl) > 100:
                    del pl[:50]

            if want_events:
                tgt._event(logger, level, msg, pid)
                if level == "ERROR":
                    self._expect_exc = tgt
            elif msg.startswith("Login ") and logger.endswith("res_users"):   # pairs the account with the access line's IP
                lm = LOGIN_MSG.match(msg)
                if lm:
                    tgt._pending_login[pid] = (lm.group("res"), lm.group("login"))
        if self._ihold is not None:
            self._release_issue()
        for lst in pend.values():                   # never claimed: logged outside any request of this piece
            for e in lst:
                self._settle(e, False)
            lst.clear()

    def _settle(self, e, during):
        """A deferred warning (pending entry with no key yet) that no request will keep or drop any more:
        `during` False -- logged outside the run that ended now, so counted with no parent unless it is filtered
        by its client (then it has none, and is left out)."""
        if e[2] is None:
            level, logger, msg, stamp, more, client, found = e[4]
            if client or during:
                e[1].filtered += 1
            else:
                e[1]._count_issue(level, logger, msg, stamp, more, found)

    def _release_issue(self):
        """A warning / error held back by a fact filter, now that its continuation lines are read: counted
        exactly as it would have been without the filter if its facts pass, else dropped."""
        tgt, level, logger, msg, stamp, pid, t, more, found = self._ihold
        self._ihold = None
        tpl = _norm_head(msg)
        want = self.a.fact_filter.get("issue", {}).get((level, logger, tpl)) or {}
        client = _client_want(self.a.fact_filter, want)
        own = {k: v for k, v in want.items() if k not in CLIENT_FACTS}
        xp = extra_pred(self.a)
        have = issue_occurrence_facts(msg, more)
        pl = self._pend.setdefault(pid, [])
        if (own and not _facts_ok(own, have)) or (xp and not xp(issue_fields(level, logger, tpl, msg, have))):
            tgt.filtered += 1
            pl.append((t, tgt, False, have))            # not counted, but its request still logged what it names
        else:
            pl.append((t, tgt, None, have, (level, logger, msg, stamp, more, client, found)))   # decided by its request (_claim)
        if len(pl) > 200:
            for e in pl[:100]:
                self._settle(e, False)
            del pl[:100]

    def _count_issue(self, level, logger, msg, stamp, more, found=None):
        """Count one warning / error whose continuation lines `more` are known; returns its key. found: the
        exception class / server action already read from a traceback longer than `more`."""
        key, e = self._issue(level, logger, msg, stamp)
        if found:
            e[4] = e[4] or found["exc"]
            e[5] = e[5] or found["sa"]
        f = self.issue_facts.setdefault(key, {})
        for raw in more:
            _detail_facts(f, raw)
            if level != "WARNING" and not e[5] and "ir.actions.server" in raw:
                sa = SERVER_ACTION.search(raw)
                if sa:
                    e[5] = "server action #%s" % (sa.group(1) or sa.group(2))
        if level != "WARNING" and not e[4]:
            for raw in more:
                em = EXC_LINE.match(raw.strip())
                if em:
                    e[4] = em.group("exc")
                    break
        return key

    def _logged_facts(self, pid, t_end, span, pend, lpend):
        """The users and records named in what `pid` logged during the last `span` seconds -- the warnings
        (with their tracebacks) and untimed lines (ORM calls, add-on messages) the request / job ending now
        is about to claim (see _claim) -- as {F_LOGGED_USER | F_LOGGED_REC: set}."""
        out = {}
        lo = t_end - span - 1.0
        for e in (pend.get(pid) or ()) if pend else ():
            if e[0] >= lo and e[3]:
                for mine, name in LOGGED_OF:
                    vals = e[3].get(mine)
                    if vals:
                        out.setdefault(name, set()).update(v for v in vals if v != FACT_OTHER)
        for ts, _c, msg, _raw in (lpend.get(pid) or ()) if lpend else ():
            if ts >= lo and ("id" in msg or "(" in msg or "user" in msg or "User" in msg):
                d = {}
                _detail_facts(d, msg)
                for mine, name in LOGGED_OF:
                    if d.get(mine):
                        out.setdefault(name, set()).update(d[mine])
        return out

    def _count_logged(self, det, logged, seconds):
        """Count what the lines logged during one request / job of `det` named (see _logged_facts). In sorted
        order: the values are sets of strings, whose own order changes from one process to the next, and the
        order they are counted in decides how equally frequent values are listed (and which ones are kept once
        a detail is full, see FACT_KEEP)."""
        if logged:
            f, t = self.op_logged.setdefault(det, {}), self.op_logged_s.setdefault(det, {})
            for slot, vals in logged.items():
                for v in sorted(vals):
                    _fact_time(f, t, slot, v, seconds)

    def _drop_claims(self, pid, pend, lpend, t_end=0.0, span=0.0):
        """A request / job left out by a filter: what its worker logged while running it goes with it (and is
        not claimed by the worker's next request instead); what was logged before it is counted parentless."""
        lo = t_end - span - 1.0
        for e in (pend.get(pid) or ()) if pend else ():
            self._settle(e, e[0] >= lo)
        for d in (pend, lpend):
            lst = d.get(pid) if d else None
            if lst:
                lst.clear()

    # -- cron jobs, automation rules, module loading --------------------------- #
    def _automation_pair(self, msg, pid, t, minute):
        m = AUTO_START.match(msg)
        if m:
            self._starts[(pid, m.group("name"))] = t
            return
        m = AUTO_END.match(msg)
        if m:
            start = self._starts.pop((pid, m.group("name")), None)
            if start is not None and t >= start:
                self.formats["automation 17-19 (start/done paired)"] += 1
                self._job(("automation", "base_automation", "automation: %s (time-based)" % m.group("name"),
                           t - start, 0), minute)

    def _cron_killed(self, logger, msg, minute, tm, ms, raw, stamp):
        """The main process killed a cron worker (see CRON_KILLED): the run it started last counts, in its own
        database, as a run that lasted until then, with status CRON_KILLED_STATUS."""
        wpid = cron_killed_pid(logger, msg)
        if not wpid:
            return
        t = _tsec(minute, tm, ms)
        run = self._cron_runs.pop(wpid, None)
        if run is None:
            if wpid not in self._cron_first:       # its run started before this piece: see pair_cron_kills
                self._cron_kills.append((wpid, t, raw, stamp))
            self._cron_first.setdefault(wpid, t)
            return
        tgt, name, cid, t0, db = run
        self._cron_done.append((wpid, db, name, cid, t0, t, raw, stamp))
        job = cron_killed_job(name, cid, max(0.0, t - t0))
        tgt.formats[job[5]] += 1
        tgt._job(job[:5], minute, wpid, t, self._pend, self._lpend, raw, stamp, job[6])

    def _claim(self, pid, det, t_end, span, pend, lpend=None, ip=None, session="-", seconds=None):
        """Attribute the warnings / errors and the untimed log lines `pid` logged during the last `span`
        seconds to operation `det` (and, for a request, to its client). seconds: the time booked for that
        request / job (span when not given), counted once per line shape it logged."""
        lst = pend.get(pid) if pend else None
        if lst:
            lo = t_end - span - 1.0
            for e in lst:
                ts, child, ikey = e[0], e[1], e[2]
                if ikey is False:                   # left out by a filter: only named things (see _logged_facts)
                    continue
                if ikey is None:                    # held by a filter: this request decides it now
                    if ts < lo:
                        self._settle(e, False)
                        continue
                    level, logger, msg, stamp, more, client, found = e[4]
                    have = {F_IP: {ip}} if ip else {}
                    if ip and session != "-":
                        have[F_SESSION] = {session}
                    if client and not _facts_ok(client, have):
                        child.filtered += 1
                        continue
                    ikey = child._count_issue(level, logger, msg, stamp, more, found)
                if ts >= lo:
                    ops = child.issue_ops.setdefault(ikey, {})
                    ops[det] = ops.get(det, 0) + 1
                    if ip:
                        f = child.issue_facts.setdefault(ikey, {})
                        _fact(f, F_IP, ip)
                        if session != "-":
                            _fact(f, F_SESSION, session)
            lst.clear()
        lst = lpend.get(pid) if lpend else None
        if lst:
            lo = t_end - span - 1.0
            seen = set()
            for ts, child, msg, raw in lst:
                if ts >= lo:
                    e = child._trace(det, msg, raw)
                    if id(e) not in seen:
                        seen.add(id(e))
                        e[2] += 1
                        e[3] += span if seconds is None else seconds
            lst.clear()

    def _trace(self, det, msg, raw):
        """Count one untimed log line under operation `det`. ORM calls group by model and method
        ('stock.valuation.layer().search_read'), everything else by its normalized text."""
        m = TRACE_CALL.match(msg)
        key = "%s().%s" % (m.group(1), m.group(2)) if m else normalize(msg, 100)
        d = self.src.get(det)
        if d is None:
            d = self.src[det] = {}
        e = d.get(key)
        if e is None:
            if len(d) >= TRACE_KEEP:                       # bound memory: further shapes only add to one counter
                key = "(other lines)"
                e = d.get(key)
            if e is None:
                e = d[key] = [0, "" if key == "(other lines)" else raw.rstrip()[:TRACE_LINE], 0, 0.0]
        e[0] += 1
        return e

    def _job(self, job, minute, pid=None, t_end=0.0, pend=None, lpend=None, raw=None, stamp=None, facts=()):
        fam, app, det, seconds, nq = job
        ff = getattr(self.a, "fact_filter", None)
        logged = self._logged_facts(pid, t_end, seconds, pend, lpend) if fam == "cron" else {}
        if ff:
            have = dict(logged, **{F_DUR: seconds})
            for slot, v in facts:
                have.setdefault(slot, set()).add(v)
            xp = extra_pred(self.a)
            if not _op_filter_ok(ff, fam, app, det, have) or (xp and not xp(job_fields(fam, app, det, seconds, nq, facts, logged))):
                self.filtered += 1
                if fam == "cron":
                    self._drop_claims(pid, pend, lpend, t_end, seconds)
                return
        if fam == "cron":            # cron runs in its own worker; automation rules and module loading run *inside*
            self._count_logged(det, logged, seconds)
            self._claim(pid, det, t_end, seconds, pend, lpend)   # a request, which must keep what they log
        if facts:
            f, t = self.op_facts.setdefault(det, {}), self.op_facts_s.setdefault(det, {})
            for slot, v in facts:
                _fact_time(f, t, slot, v, seconds)
        b = self.det[det]
        b.add(0.0, 0.0, nq, False, self.rng, seconds)
        if raw is not None:
            b.note(stamp, raw, seconds, False)
        self.det[det].hist_add(minute, 0.0, 0.0, seconds)      # one run, same as any other operation's occurrence
        self.det_fam[det] = fam
        self.det_app[det] = app

    # -- warnings / errors -------------------------------------------------- #
    def _issue(self, level, logger, msg, stamp):
        """Count one WARNING / ERROR / CRITICAL line under its normalized message."""
        head = msg[:140]
        key = (level, logger, _norm_head(msg))
        e = self.issues.get(key)
        if e is None:
            if len(self.issues) >= 20000:              # bound memory on logs with unbounded variety
                key = (level, logger, "(more distinct messages)")
                e = self.issues.get(key)
            if e is None:
                e = self.issues[key] = [0, stamp, stamp, msg[:1500], "", "", {}]
        e[0] += 1
        if stamp < e[1]:
            e[1] = stamp
        if stamp > e[2]:
            e[2] = stamp
        m = stamp_minute(stamp[:10], stamp[11:16])
        h = e[6].get(m)
        if h is None:
            e[6][m] = [1, 0.0, 0.0, 0.0]
        else:
            h[0] += 1
        if not e[5] and ("server action" in msg or "ir.actions.server" in msg):
            sa = SERVER_ACTION.search(msg)
            if sa:
                e[5] = "server action #%s" % (sa.group(1) or sa.group(2))
        f = self.issue_facts.get(key)
        if f is None:
            f = self.issue_facts[key] = {}
        _num_facts(f, head)
        _detail_facts(f, msg[:2000])
        self.levels[level] += 1
        return key, e

    # -- request line ----------------------------------------------------- #
    def _request(self, msg, hour, pid, minute, t_end=0.0, pend=None, lpend=None, raw=None, stamp=None):
        m = ACCESS.match(msg)
        if not m:
            self.access_bad += 1
            self.unclassified["ACCESS-LINE-UNPARSED: " + normalize(msg, 80)] += 1
            return
        path, method, status, nq, qt, ot, mode = m.group("path", "method", "status", "nq", "qt", "ot", "mode")
        if mode == "ro->rw":
            self.formats["read-only requests retried read-write"] += 1
        if nq is None:
            self.access_untimed += 1          # older Odoo: no timing triplet
            nq, qt, ot = 0, 0.0, 0.0
        else:
            self.access_ok += 1
            nq, qt, ot = int(nq), float(qt), float(ot)
        if self.a.grep and self.a.grep not in path:
            self.filtered += 1
            return
        span = qt + ot                                      # how long the request ran, before any held time is removed
        held = None
        if status == "101" or path.startswith("/longpolling/poll"):
            # A WebSocket upgrade / long poll keeps the connection open for minutes and Odoo books that
            # whole wall-clock time as "other time". It is waiting, not working: keep the SQL part
            # (the handshake's real queries) and report the held time separately (once the filters keep it).
            held, ot = ot, 0.0
        key = (method, path, status == "404")
        cls = _REQ_CACHE.get(key)
        if cls is None:
            if len(_REQ_CACHE) > 200000:
                _REQ_CACHE.clear()
            fam, app, det = classify(method, path, status)
            collapsed = bool("?" in path or _DIGITS.search(path) or "<" in det or "…" in det or "/*" in det)   # ids / tokens / a query
            cls = _REQ_CACHE[key] = (fam, app, det, collapsed)
        fam, app, det, collapsed = cls
        is_err = status[0] == "5"
        ip, session = m.group("ip", "ident")
        ff = self._ff
        logged = self._logged_facts(pid, t_end, span, pend, lpend) if t_end else {}
        if ff:
            xp = extra_pred(self.a)
            if not _op_filter_ok(ff, fam, app, det, dict(request_facts(ip, session, path, collapsed, qt + ot), **logged)) or \
                    (xp and not xp(request_fields(fam, app, det, status, method, qt, ot, nq, ip, session, path, logged))):
                self.filtered += 1
                self._drop_claims(pid, pend, lpend, t_end, span)
                self._pending_login.pop(pid, None)          # its login line goes with it, not to the worker's next login
                return
        if held is not None:
            self.held_n += 1
            self.held_s += held
        f = self.op_facts.get(det)
        if f is None:
            f = self.op_facts[det] = {}
        t, secs = self.op_facts_s.setdefault(det, {}), qt + ot
        _fact_time(f, t, F_IP, ip, secs)
        if session != "-":                                  # 19.4+: a short session id per request
            _fact_time(f, t, F_SESSION, session, secs)
        if collapsed:
            _fact_time(f, t, F_URL, path[:FACT_LEN], secs)
        if fam not in ("assets", "probe"):
            self._client(ip if session == "-" else ip + " " + session, minute, qt + ot)
        d = self.det[det]
        d.add(qt, ot, nq, is_err, self.rng)
        if raw is not None:
            d.note(stamp, raw, qt + ot, is_err)
        d.hist_add(minute, qt, ot)
        d.st[status] = d.st.get(status, 0) + 1
        if det not in self.det_fam:
            self.det_fam[det] = fam
            self.det_app[det] = app
        if logged:
            self._count_logged(det, logged, qt + ot)
        if t_end:                                           # (0: this worker logged nothing since its last request)
            self._claim(pid, det, t_end, span, pend, lpend, ip, session, qt + ot)    # warnings / errors / untimed lines it logged while running it
        if hour is not None:
            self.hours[hour].add(qt, ot, nq, is_err, self.rng)
        if fam == "other":
            self.unclassified[det] += 1

        if fam in ("auth", "api.auth"):
            # Odoo logs "Login successful for login:X from n/a" with no IP (RPC has no
            # request object at that point), and the access line has the IP but not the
            # account. They are emitted back to back by the same worker, so the pid
            # joins them. GETs of /web/login are page views, not login attempts.
            got = self._pending_login.pop(pid, None)
            if got:
                lk = (got[1], ip, got[0])
                self.logins[lk] += 1
                seen = self.login_seen.get(lk)
                if stamp and seen is None:
                    self.login_seen[lk] = [stamp, stamp]
                elif stamp:
                    seen[0], seen[1] = min(seen[0], stamp), max(seen[1], stamp)
            elif method != "GET":
                self.logins[("(no login line in this file)", m.group("ip"), "?")] += 1

    def _client(self, client, minute, secs):
        """One request of `client` in `minute`: who was working when."""
        c = self.clients.get(client)
        if c is None:
            if len(self.clients) >= CLIENT_KEEP:
                client = CLIENT_OTHER
                c = self.clients.get(client)
            if c is None:
                c = self.clients[client] = {}
        e = c.get(minute)
        if e is None:
            c[minute] = [1, secs]
        else:
            e[0] += 1
            e[1] += secs

    # -- non-request line -------------------------------------------------- #
    def _event(self, logger, level, msg, pid):
        fam = event_family(logger)
        tpl = normalize(msg)
        key = "%-7s %s" % (level, tpl)
        self.events[fam][key] += 1
        self.event_level[(fam, key)] = level

        if fam == "auth":
            lm = LOGIN_MSG.match(msg)
            if lm:
                self._pending_login[pid] = (lm.group("res"), lm.group("login"))
        for sa in SERVER_ACTION.findall(msg):
            self.server_actions[sa[0] or sa[1]] += 1

    # -- report ------------------------------------------------------------ #
    def report(self, out):
        """Write the text report to `out` (see report.write_report)."""
        write_report(self, out)

    def as_rows(self, scopes=("family", "app", "operation"), limit=None):
        """One row per type, per app and per operation (see report.as_rows)."""
        return as_rows(self, scopes, limit)
