"""Background work that is logged as messages instead of access lines: cron jobs, automation rules, module loading.

`classify_job()` reads a timed message of any supported Odoo version; the `cron_*` helpers pair a cron run whose
worker was killed at its time limit with the kill line (within one parsed piece, or across pieces: chunks, files).
"""

from __future__ import annotations

import bisect
import re


# Odoo logs no timing for server actions themselves (they run inside a request, a cron job
# or an automation rule and are already counted there), but it does log:
#   cron jobs         Job 'Mail: Email Queue Manager' (8) fully done (#loop 1; done 20; remaining 0; duration 0.62s)
#   automation rules  Automation Rule 'Notify' (12) done (on_write; 3/3 records; duration 0.532s)
#                     (at INFO only when slower than 0.1 s, WARNING above 1 s)
#   module loading    Module sale loaded in 1.20s, 2400 queries (+300 extra)
# cron, by version:
#   17.0        Starting job `Name`.            Job done: `Name` (0.007s).
#   18.0        Job 'Name' (8) starting         Job 'Name' (8) done in 0.017s   (once per loop iteration)
#   19.0 - 20.0 Job 'Name' (8) starting         Job 'Name' (8) fully done (#loop 1; done 20; remaining 0; duration 0.62s)
#               (status is "fully done", "partially done" or "failed")
CRON_17 = re.compile(r"^Job done: `(?P<name>.*)` \((?P<dur>[\d.]+)s\)")
CRON_18 = re.compile(r"^Job (?P<q>['\"])(?P<name>.*)(?P=q) \((?P<id>\d+)\) done in (?P<dur>[\d.]+)s")
CRON_19 = re.compile(r"^Job (?P<q>['\"])(?P<name>.*)(?P=q) \((?P<id>\d+)\) (?P<status>[a-z ]+?) "
                     r"\(#loop (?P<loops>\d+); done (?P<done>\d+); remaining (?P<rem>\d+); duration (?P<dur>[\d.]+)s\)")
# automation rules: 20.0 logs a duration (INFO above 0.1 s, WARNING above 1 s); 17.0 - 19.0 only log
# the start and the end of time-based rules, so those are paired by worker and rule name.
AUTOMATION_DONE = re.compile(r"^(?P<what>.+?) (?P<q>['\"])(?P<name>.*)(?P=q) \((?P<id>\d+)\) (?P<status>\w+) "
                             r"\((?P<trigger>[^;()]+); (?P<done>\d+)/(?P<total>\d+) records; duration (?P<dur>[\d.]+)s\)")
AUTO_START = re.compile(r"^Starting time-based automation rule `(?P<name>.*)`\.")
AUTO_END = re.compile(r"^Time-based automation rule `(?P<name>.*)` done\.")
# module loading: "762 modules loaded in 7.38s, 0 queries (+0 extra)" is logged by every version;
# "Module sale loaded in 1.20s, 2400 queries" only exists at DEBUG level and is not used (it would double count).
MODULES_LOADED = re.compile(r"^(?P<n>\d+) modules loaded in (?P<dur>[\d.]+)s, (?P<nq>\d+) queries")
JOB_KINDS = ("cron", "automation", "module-load")
JOB_LOGGERS = ("ir_cron", "base_automation", "modules.loading")
# a cron run whose worker is killed never logs its end: the main process logs (with no database)
#   WorkerCron (1234) timeout after 900s
# and the run is the one that worker started last ("Job 'Name' (8) starting", 17.0: "Starting job `Name`.");
# Odoo ends these messages with a space
CRON_START = re.compile(r"^Job (?P<q>['\"])(?P<name>.*)(?P=q) \((?P<id>\d+)\) starting\s*$")
CRON_START_17 = re.compile(r"^Starting job `(?P<name>.*)`\.\s*$")
CRON_KILLED = re.compile(r"^WorkerCron \((?P<pid>\d+)\) timeout after (?P<limit>\d+)s")
CRON_KILLED_LOGGER = "odoo.service.server"
CRON_KILLED_STATUS = "killed (timeout)"


def _app_from_label(name):
    """'Google Calendar: synchronization' -> 'google_calendar' (Odoo names crons '<App>: <what>')."""
    head = name.split(":", 1)[0] if ":" in name else ""
    return re.sub(r"[^a-z0-9]+", "_", head.strip().lower()).strip("_") or "cron"


def cron_start(msg):
    """(name, cron id or None) of a cron run's "starting" line, else None."""
    m = CRON_START.match(msg) or CRON_START_17.match(msg)
    return (m.group("name"), m.groupdict().get("id")) if m else None


def cron_killed_pid(logger, msg):
    """The worker of a "WorkerCron (pid) timeout after Ns" line, else None."""
    m = CRON_KILLED.match(msg) if logger == CRON_KILLED_LOGGER else None
    return m.group("pid") if m else None


def cron_killed_job(name, cid, seconds):
    """classify_job()'s answer for the run of cron `name` whose worker was killed after `seconds`."""
    facts = ([("cron id", cid)] if cid else []) + [("status", CRON_KILLED_STATUS)]
    return "cron", _app_from_label(name), "cron: %s" % name, seconds, 0, "cron killed (worker timeout)", facts


def cron_marks(prof):
    """What a parsed piece (chunk or file) tells about cron runs killed across pieces: (open, kills, first, done).
    open: [(worker, database, name, id, start)] runs not ended at the piece's end; kills: [(worker, time, raw,
    stamp)] kills whose run started before the piece; first: {worker: time of its first cron line}; done: the
    runs killed within the piece, (worker, database, name, id, start, kill time, raw, stamp). Small: only cron
    workers are in it."""
    return ([(w, r[4], r[1], r[2], r[3]) for w, r in prof._cron_runs.items()], prof._cron_kills,
            prof._cron_first, prof._cron_done)


def pair_cron_kills(pieces):
    """The cron runs started in one piece and killed in a later one (other chunk, other file), from the pieces'
    cron_marks: [(key, (worker, database, name, id, start, kill time, raw, stamp))], `key` being the kill's
    piece's. pieces: [(key, marks)] in any order. A kill pairs with its worker's last run started before it,
    unless the worker logged another cron line in between (that run ended, or this pid is another worker)."""
    opens, firsts = {}, {}
    for key, (op, _kills, first, _done) in pieces:
        for w, db, name, cid, t0 in op:
            opens.setdefault(w, []).append((t0, db, name, cid))
        for w, t in first.items():
            firsts.setdefault(w, []).append(t)
    for lst in list(opens.values()) + list(firsts.values()):
        lst.sort()
    out, used = [], set()
    for key, (_op, kills, _first, _done) in pieces:
        for w, t, raw, stamp in kills:
            lst = opens.get(w)
            k = bisect.bisect_left(lst, (t,)) - 1 if lst else -1
            if k < 0 or (w, k) in used:
                continue
            t0, db, name, cid = lst[k]
            f = firsts.get(w, ())
            i = bisect.bisect_right(f, t0)
            if i < len(f) and f[i] < t:                # the worker did something else meanwhile
                continue
            used.add((w, k))
            out.append((key, (w, db, name, cid, t0, t, raw, stamp)))
    return out


def book_cron_kills(runs, part):
    """Count killed runs found outside the parse (see pair_cron_kills) as the parser counts the ones it pairs
    itself: `part(database)` is the Profile each goes to (its arguments' filters apply). What the worker logged
    while it ran is not claimed: those lines were counted with their own piece."""
    for w, db, name, cid, t0, t, raw, stamp in runs:
        tgt = part(db)
        job = cron_killed_job(name, cid, max(0.0, t - t0))
        tgt.formats[job[5]] += 1
        tgt._job(job[:5], int(t // 60), w, t, None, None, raw, stamp, job[6])


def classify_job(logger, msg):
    """(type, app, operation, seconds, queries, format, facts) for a timed background-work message, else None.
    facts: the (detail, value) pairs the operation name leaves out, such as the cron id and how the run ended."""
    if logger.endswith("ir_cron"):
        for rx, fmt in ((CRON_19, "cron 19+"), (CRON_18, "cron 18"), (CRON_17, "cron 17")):
            m = rx.match(msg)
            if m:
                name, g = m.group("name"), m.groupdict()
                facts = [("cron id", g["id"])] if g.get("id") else []
                if g.get("status"):
                    facts += [("status", g["status"]), ("remaining", g["rem"])]
                return "cron", _app_from_label(name), "cron: %s" % name, float(m.group("dur")), 0, fmt, facts
    elif logger.endswith("base_automation"):
        m = AUTOMATION_DONE.match(msg)
        if m:
            return ("automation", "base_automation", "automation: %s (%s)" % (m.group("name"), m.group("trigger").strip()),
                    float(m.group("dur")), 0, "automation 20 (duration)",
                    [("rule id", m.group("id")), ("status", m.group("status")),
                     ("records processed", "%s/%s" % (m.group("done"), m.group("total")))])
    elif logger.endswith("modules.loading"):
        m = MODULES_LOADED.match(msg)
        if m:
            return ("module-load", "base", "load modules", float(m.group("dur")), int(m.group("nq")), "modules loaded",
                    [("modules", m.group("n"))])
    return None
