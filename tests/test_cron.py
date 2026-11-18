"""Cron runs: the requests that ran meanwhile, and runs whose worker was killed at its time limit.
"""

from __future__ import annotations

import calendar
import io
import time

from support import CRON, IMG, analyze, case, detail, facts, upload

from logtool import web
from logtool import profiler as olp


@case
def test_requests_while_a_cron_ran(c):
    at = lambda t, secs: IMG.replace(b"13:07:00", t) % (b"7", secs)
    upload(c, "f.log", IMG % (b"7", b"0.1") + at(b"13:08:00", b"0.2") + CRON + at(b"13:08:05", b"10")
           + at(b"13:09:00", b"0.1"))
    analyze(c)
    got = c.get("/api/cron_requests?name=cron:%20Credit%20Check").get_json()
    assert got["runs"] == 1 and got["requests"] == 2 and got["avg_ms"] == 5110, got   # 0.21 s and 10.01 s overlap it
    assert sum(b[2] for b in got["durations"]["bins"]) == 2, got
    got = c.get("/api/cron_requests?name=cron:%20Credit%20Check&ip=10.9.9.9").get_json()
    assert got["runs"] == 0 and got["requests"] == 0 and got["avg_ms"] is None, got   # an IP filter drops jobs
    assert c.get("/api/cron_requests?name=image").status_code == 400
    d = c.get("/api/request_durations").get_json()["durations"]
    assert sum(b[2] for b in d["bins"]) == 4 and d["bins"][-1][0] <= 10010 < d["bins"][-1][1], d   # the cron is not a request


KILLED = (b"2026-09-14 13:00:00,000 6100 INFO mydb odoo.addons.base.models.ir_cron: Job 'Procurement: run scheduler' (5) starting \n"
          b"2026-09-14 13:05:00,000 6100 WARNING mydb odoo.models: Order #9 is stuck\n"
          b"2026-09-14 13:15:00,500 100 ERROR ? odoo.service.server: WorkerCron (6100) timeout after 900s \n"
          b"2026-09-14 13:20:00,000 6200 INFO mydb odoo.addons.base.models.ir_cron: Job 'Procurement: run scheduler' (5) starting\n"
          b"2026-09-14 13:20:02,000 6200 INFO mydb odoo.addons.base.models.ir_cron: Job 'Procurement: run scheduler' (5) fully done "
          b"(#loop 1; done 3; remaining 0; duration 2.00s)\n"
          b"2026-09-14 13:40:00,000 100 ERROR ? odoo.service.server: WorkerCron (6200) timeout after 900s\n")


@case
def test_a_cron_run_killed_by_the_worker_timeout_counts(c):
    upload(c, "f.log", KILLED)
    op = "cron: Procurement: run scheduler"
    row = next(r for r in analyze(c)[1]["view"]["rows"] if r["operation"] == op)
    assert row["calls"] == 2 and row["max_s"] == 900.5, row          # the run killed after 900 s, and one that ended
    for q in ("", "&exclude_db=-"):                 # the kill line has no database: the run stays in the cron's
        s, d = detail(c, "operation", op, q)
        assert s == 200 and d["calls"] == 2, (q, d)
        f = facts(d["facts"])
        assert set(f["status"]) == {olp.CRON_KILLED_STATUS, "fully done"} and "5" in f["cron id"], f
    # what its worker logged while it ran is its, as for a run that ended; a kill of a finished run counts nothing
    w = next(i for i in analyze(c)[1]["view"]["issues"] if "stuck" in i["message"])
    assert [o[0] for o in w["ops"]] == [op] and w["unattributed"] == 0, w
    got = c.get("/api/op_lines?by=operation&name=" + op.replace(" ", "%20") + "&exclude_db=-").get_json()
    assert [(o["line"], o["seconds"]) for o in got] == [(3, 900.5), (5, 2.0)], got     # the kill line stands for the run
    s, d = detail(c, "operation", op, '&ff=[{"by":"operation","name":"%s","facts":{"status":["%s"]}}]' % (op, olp.CRON_KILLED_STATUS))
    assert d["calls"] == 1, d                                                      # an item filter on how it ended
    out = [(o["line"], o["seconds"], o["db"]) for o in olp.scan_op_occurrences(next(iter(web.state.FILES.values()))["path"], "operation", op,
                                                                            {"-"}, None, None, 10)]
    assert out == [(3, 900.5, "mydb"), (5, 2.0, "mydb")], out


START_6100 = (b"2026-09-14 22:55:00,000 6100 INFO mydb odoo.addons.base.models.ir_cron: Job 'Procurement: run scheduler' (5) "
              b"starting \n")
KILL_6100 = b"2026-09-14 23:10:00,500 100 ERROR ? odoo.service.server: WorkerCron (6100) timeout after 900s \n"
NOISE = b"2026-09-14 23:05:00,000 7 INFO mydb werkzeug: 10.0.0.1 - - [14/Sep/2026 23:05:00] \"GET /web HTTP/1.1\" 200 - 1 0.001 0.002\n"


@case
def test_a_cron_run_killed_in_the_next_log_file_counts(c):
    # a daily rotation at 23:00: the run starts in one file and its worker is killed in the next
    from urllib.parse import quote
    upload(c, "a.log", START_6100)
    upload(c, "b.log", NOISE + KILL_6100)
    op = "cron: Procurement: run scheduler"
    rows = lambda q="": {r["operation"]: r for r in analyze(c, q)[1]["view"]["rows"]}
    assert rows()[op]["calls"] == 1 and rows()[op]["max_s"] == 900.5, rows()
    ids = [f["id"] for f in c.get("/api/files").get_json()]
    assert op in rows("&exclude_file=%s" % ids[0])           # it goes with the file of its kill, like a run that ended
    assert op not in rows("&exclude_file=%s" % ids[1])
    t = lambda s: calendar.timegm(time.strptime("2026-09-14 " + s, "%Y-%m-%d %H:%M"))
    for lo, hi, n in (("23:00", "23:20", 1), ("23:05", "23:20", 1), ("22:50", "23:05", 0)):   # by when it was killed
        q = "&from=%d&to=%d" % (t(lo), t(hi))
        assert rows(q).get(op, {}).get("calls", 0) == n, (lo, hi, rows(q))
        got = c.get("/api/op_lines?by=operation&name=%s%s" % (quote(op), q)).get_json()
        assert [(o["file"], o["seconds"]) for o in got] == ([(ids[1], 900.5)] if n else []), (lo, got)
    ff = '&ff=[{"by":"operation","name":"%s","facts":{"status":["%s"]}}]' % (op, olp.CRON_KILLED_STATUS)
    assert detail(c, "operation", op, ff)[1]["calls"] == 1
    assert detail(c, "operation", op, ff.replace("killed (timeout)", "fully done"))[1]["found"] is False
    assert op not in rows("&ip=10.0.0.1")                    # a job has no client
    assert detail(c, "operation", op, "&exclude_db=-")[1]["calls"] == 1


@case
def test_a_cron_run_killed_after_an_interval_starts_counts_in_it(c):
    # one file, its run started before the interval: the interval's edge is read from its start on
    upload(c, "f.log", START_6100 + NOISE + KILL_6100)
    op = "cron: Procurement: run scheduler"
    t = lambda s: calendar.timegm(time.strptime("2026-09-14 " + s, "%Y-%m-%d %H:%M"))
    for lo, hi, n in (("23:00", "23:20", 1), ("22:50", "23:20", 1), ("22:50", "23:08", 0)):
        got = {r["operation"]: r["calls"] for r in analyze(c, "&from=%d&to=%d" % (t(lo), t(hi)))[1]["view"]["rows"]}
        assert got.get(op, 0) == n, (lo, hi, got)


@case
def test_killed_runs_pair_across_pieces_only_when_nothing_came_between(c):
    def piece(text):
        p = olp.Profile(web.state._args())
        p.feed(io.StringIO(text))
        return olp.cron_marks(p)
    start, kill = START_6100.decode(), KILL_6100.decode()
    end = ("2026-09-14 23:00:00,000 6100 INFO mydb odoo.addons.base.models.ir_cron: Job 'Procurement: run scheduler' (5) "
           "fully done (#loop 1; done 3; remaining 0; duration 300.00s) \n")
    pairs = olp.pair_cron_kills([("b", piece(kill)), ("a", piece(start))])          # in any order
    assert [(k, r[0], r[1], r[2], r[5] - r[4]) for k, r in pairs] == [("b", "6100", "mydb", "Procurement: run scheduler", 900.5)]
    assert olp.pair_cron_kills([("a", piece(start)), ("m", piece(end)), ("b", piece(kill))]) == []   # it ended
    assert olp.pair_cron_kills([("a", piece(start + end)), ("b", piece(kill))]) == []
    assert olp.pair_cron_kills([("b", piece(kill))]) == [] and olp.pair_cron_kills([("a", piece(start))]) == []
    assert olp.pair_cron_kills([("a", piece(start + kill))]) == []                   # paired within the piece already


@case
def test_slowest_first_reads_a_jobs_lines_once(c):
    # the quick look that passes over fast lines only reads access lines: on a job with many runs it passed over
    # every line, found nothing and read the files a second time
    secs = [(i % 7) + (i % 100) / 100.0 for i in range(500)]
    upload(c, "cron.log", "".join(
        "2026-09-14 13:%02d:%02d,000 6000 INFO mydb odoo.addons.base.models.ir_cron: Job 'Credit Check' (17) fully done "
        "(#loop 1; done 1; remaining 0; duration %.2fs)\n" % (i // 60, i % 60, s) for i, s in enumerate(secs)).encode())
    analyze(c)
    scans, real = [], olp.scan_pieces
    olp.scan_pieces = lambda *a, **kw: scans.append(1) or real(*a, **kw)
    try:
        out = c.get("/api/op_lines?order=slow&by=operation&name=cron:%20Credit%20Check").get_json()
    finally:
        olp.scan_pieces = real
    assert [o["seconds"] for o in out] == sorted((round(s, 2) for s in secs), reverse=True)[:200], out[:3]
    assert len(scans) == 1, len(scans)
