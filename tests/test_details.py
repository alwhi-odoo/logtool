"""The details popup: example lines, untimed lines matched to an operation, facts, raw lines, the log viewer.
"""

from __future__ import annotations

import io
import random

from support import (
    AUTH, CONFIRM, CRON, DENIED, IMG, LOGGED_LOG, LOGIN, OP, RPC, _ff, _post_domain, _profile, analyze, case, detail,
    facts, upload,
)

from logtool import web
from logtool import profiler as olp


CALL = (b"2026-09-14 13:05:08,900 4242 DEBUG mydb odoo.api: call stock.valuation.layer().search_read("
        b"[['create_date', '>', '12/31/2024']], fields=['create_date', 'product_id'])\n")


@case
def test_detail_matches_untimed_lines_to_the_request_that_followed(c):
    other = CALL.replace(b"4242", b"777")                                   # another worker's line must not be claimed
    upload(c, "d.log", CALL + other + RPC % b"0.2" + CALL + RPC % b"0.9")
    analyze(c)
    status, d = detail(c, "operation", OP)
    assert status == 200 and d["found"], d
    src = d["sources"]
    assert src["total"] == 2 and src["items"][0]["key"] == "stock.valuation.layer().search_read", src
    assert src["items"][0]["n"] == 2 and src["items"][0]["per_call"] == 1.0, src
    assert "fields=['create_date'" in src["items"][0]["sample"], src
    kinds = {x["kind"]: x for x in d["examples"]}
    assert kinds["slowest"]["seconds"] == 1.0 and "0.9" in kinds["slowest"]["line"], d["examples"]
    assert "first seen" in kinds, d["examples"]
    for by, name in (("app", "stock"), ("family", "ui.read")):
        status, g = detail(c, by, name)
        assert status == 200 and g["found"] and g["top_ops"][0]["operation"] == OP and g["sources"]["total"] == 2, (by, g)


@case
def test_detail_unknown_and_bad_requests(c):
    upload(c, "d.log", CALL + RPC % b"0.2")
    analyze(c)
    assert detail(c, "operation", "nope")[1] == {"found": False}
    assert detail(c, "operation", OP)[1]["sources"]["total"] == 1
    assert c.get("/api/detail?by=bogus&name=x").status_code == 400
    assert c.get("/api/detail?by=app").status_code == 400
    assert c.get("/api/detail?by=app&name=x&from=9&to=3").status_code == 400
    w = analyze(c)[1]["window"]
    status, d = detail(c, "operation", OP, "&from=%d&to=%d" % (w[0], w[1]))            # whole window = no interval
    assert status == 200 and d["found"], d


@case
def test_detail_follows_the_time_interval(c):
    upload(c, "d.log", CALL + RPC % b"0.2" + CALL.replace(b"13:05:08,900", b"13:25:08,900") +
           RPC.replace(b"13:05:09,123", b"13:25:09,123") % b"0.2")
    w = analyze(c)[1]["window"]
    assert detail(c, "operation", OP)[1]["sources"]["total"] == 2
    d = detail(c, "operation", OP, "&from=%d&to=%d" % (w[0], w[0] + 600))[1]
    assert d["found"] and d["sources"]["total"] == 1, d


@case
def test_facts_keep_what_grouping_collapses(c):
    upload(c, "f.log", LOGIN + AUTH + DENIED + IMG % (b"7", b"0.1") + IMG % (b"8", b"0.3") + CRON)
    v = analyze(c)[1]["view"]
    i = next(i for i in v["issues"] if "top-secret" in i["message"])
    f = facts(i["facts"])
    assert "69" in f["user id"] and "sale.order(42)" in f["records"], f          # from the message's continuation lines
    assert "10.0.0.7" in f["client IP"] and "BK1AC94d" in f["session"], f        # the request its worker was running
    d = detail(c, "operation", "image res.partner.avatar_128")[1]
    f = facts(d["facts"])
    assert set(f["URL"]) == {"/web/image/res.partner/7/avatar_128", "/web/image/res.partner/8/avatar_128"}, f
    assert f["client IP"]["10.0.0.2"][1] == 2 and d["first"] == "2026-09-14T13:07", d
    f = facts(detail(c, "operation", "login /web/session/authenticate")[1]["facts"])
    assert f["client IP"]["10.0.0.1"][2] == ["jane@example.com"], f             # who logged in from that IP
    f = facts(detail(c, "operation", "cron: Credit Check")[1]["facts"])
    assert "17" in f["cron id"] and "fully done" in f["status"], f


@case
def test_details_count_calls_per_duration(c):
    upload(c, "f.log", IMG % (b"7", b"0.001") + IMG % (b"8", b"0.3") + IMG % (b"9", b"0.31") + CRON)
    analyze(c)
    d = detail(c, "operation", "image res.partner.avatar_128")[1]["durations"]
    assert [b[2] for b in d["bins"]] == [1] + [0] * (len(d["bins"]) - 2) + [2] and not d["estimated"], d
    assert d["bins"][0][0] <= 11 < d["bins"][0][1] and d["bins"][-1][0] <= 311 < d["bins"][-1][1], d
    d = detail(c, "family", "cron")[1]["durations"]
    assert sum(b[2] for b in d["bins"]) == 1 and d["bins"][0][0] <= 250 < d["bins"][0][1], d


@case
def test_op_lines_lists_an_operations_own_lines(c):
    upload(c, "f.log", IMG % (b"7", b"0.1") + IMG % (b"8", b"0.3") + IMG % (b"9", b"0.2") + CRON)
    analyze(c)
    q = "/api/op_lines?by=operation&name=image%20res.partner.avatar_128"
    got = c.get(q).get_json()
    assert [o["line"] for o in got] == [1, 2, 3] and got[1]["seconds"] == 0.31, got
    got = c.get(q + "&order=slow").get_json()
    assert [o["line"] for o in got] == [2, 3, 1], got
    got = c.get("/api/op_lines?by=app&name=base_automation").get_json()
    assert got == [], got
    got = c.get("/api/op_lines?by=family&name=cron").get_json()
    assert len(got) == 1 and got[0]["operation"] == "cron: Credit Check", got
    ctx = c.get("/api/issue_context?file=%s&line=%d&before=1&after=0" % (got[0]["file"], got[0]["line"])).get_json()
    assert ctx["match_at"] == 1 and "Credit Check" in ctx["lines"][1], ctx
    assert c.get("/api/op_lines?by=bogus&name=x").status_code == 400


@case
def test_logged_lines_name_the_records_a_request_touched(c):
    from urllib.parse import quote
    upload(c, "l.log", LOGGED_LOG)
    calls = lambda j: {r["operation"]: r["calls"] for r in j["view"]["rows"]}.get(CONFIRM, 0)
    assert calls(analyze(c)[1]) == 3
    s, d = detail(c, "operation", CONFIRM)
    lf = {f["detail"]: dict((v[0], v[1]) for v in f["values"]) for f in d["logged_facts"]}
    assert lf == {"logged records": {"sale.order(42)": 1, "sale.order(43)": 1}, "logged user id": {"7": 1}}, lf
    # which client confirmed order 42? filter the operation by it, then read its client IPs
    q = _ff(("operation", CONFIRM, {"logged records": ["sale.order(42)"]}))
    assert calls(analyze(c, q)[1]) == 1
    s, d = detail(c, "operation", CONFIRM, q)
    ip = [f for f in d["facts"] if f["detail"] == "client IP"][0]
    assert [v[0] for v in ip["values"]] == ["10.0.0.1"], ip
    assert [f for f in d["logged_facts"] if f["detail"] == "logged records"][0]["unfiltered"]
    assert len(c.get("/api/op_lines?by=operation&name=%s%s" % (quote(CONFIRM), q)).get_json()) == 1
    xd = "&xd=" + quote('[["logged user id", "=", "7"]]')
    assert calls(analyze(c, xd)[1]) == 1
    assert len(c.get("/api/op_lines?by=operation&name=%s%s" % (quote(CONFIRM), xd)).get_json()) == 1
    text = c.get("/api/domain?" + q[1:]).get_json()["domain"]
    assert '"|", ["operation", "!=", "%s"], ["logged records", "in", ["sale.order(42)"]]' % CONFIRM in text, text
    assert _post_domain(c, text)[1]["state"]["ff"][0]["facts"] == {"logged records": ["sale.order(42)"]}


@case
def test_a_warning_traces_back_to_its_client(c):
    from urllib.parse import quote
    upload(c, "l.log", LOGGED_LOG)
    key = ["WARNING", "odoo.models", "Access denied (id=#)"]
    n = lambda j: sum(i["n"] for i in j["view"]["issues"] if [i["level"], i["logger"], i["message"]] == key)
    assert n(analyze(c)[1]) == 1
    q = "level=WARNING&logger=odoo.models&msg=" + quote(key[2])
    d = c.get("/api/issue_detail?" + q).get_json()
    assert any(f["detail"] == "client IP" and f["values"][0][0] == "10.0.0.2" for f in d["facts"]), d
    for ip, want in (("10.0.0.2", 1), ("10.0.0.1", 0)):                # its request's client, decided at the claim
        ff = _ff(("issue", key, {"client IP": [ip]}))
        assert n(analyze(c, ff)[1]) == want, ip
        assert len(c.get("/api/issue_lines?" + q + ff).get_json()) == want, ip
    text = c.get("/api/domain?" + _ff(("issue", key, {"client IP": ["10.0.0.2"]}))[1:]).get_json()["domain"]
    assert _post_domain(c, text)[1]["state"]["ff"][0]["facts"] == {"client IP": ["10.0.0.2"]}, text


@case
def test_warnings_follow_the_requests_they_were_logged_during(c):
    upload(c, "l.log", LOGGED_LOG)
    key = ["WARNING", "odoo.models", "Access denied (id=#)"]
    q = "level=WARNING&logger=odoo.models&msg=Access%20denied%20%28id%3D%23%29"
    row = lambda j: next((i for i in j["view"]["issues"] if [i["level"], i["logger"], i["message"]] == key), None)
    keep = _ff(("operation", CONFIRM, {"client IP": ["10.0.0.2"]}))      # the request it was logged during stays
    r = row(analyze(c, keep)[1])
    assert r and r["n"] == 1 and r["ops"] == [[CONFIRM, 1]], r
    drop = _ff(("operation", CONFIRM, {"client IP": ["10.0.0.1"]}))      # ... or is filtered out, and takes it along
    assert row(analyze(c, drop)[1]) is None
    assert len(c.get("/api/issue_lines?" + q + keep).get_json()) == 1
    assert c.get("/api/issue_lines?" + q + drop).get_json() == []


@case
def test_the_log_viewer_marks_parent_during_and_same_lines(c):
    lines = LOGGED_LOG.decode().splitlines()
    # the warning (line 3): its traceback is main too, the access line that claims it is its parent
    m = olp.mark_context(lines, 2, issue=("WARNING", "odoo.models", "Access denied (id=#)"))
    assert m[2:6] == ["main", "main", "main", "parent"], m
    # a request (line 2): the ORM call before it is "during", the other confirmations are "same"
    m = olp.mark_context(lines, 1, by="operation", name=CONFIRM)
    assert m[0] == "during" and m[1] == "main" and m[5] == "same" and m[6] == "same" and m[2] is None, m


@case
def test_slowest_first_skips_fast_lines_but_finds_the_same_ones(c):
    r = random.Random(3)
    lines = [(b'2026-09-14 13:%02d:%02d,000 42 INFO db werkzeug: 10.0.0.1 - - [14/Sep/2026 13:00:00] '
              b'"POST /web/dataset/call_kw/sale.order/read HTTP/1.1" 200 - 5 %.3f %.3f\n'
              % (i // 60 % 60, i % 60, r.expovariate(20), r.expovariate(5))) for i in range(3000)]
    upload(c, "s.log", b"".join(lines))
    op = analyze(c)[1]["view"]["rows"][0]["operation"]
    got = c.get("/api/op_lines?by=operation&name=%s&order=slow" % op.replace(" ", "%20")).get_json()
    path = web.state.FILES[web.state.ORDER[-1]]["path"]
    want = olp.scan_op_occurrences(path, "operation", op, set(), None, None, 200, slowest=True)
    assert len(got) == 200 and [o["line"] for o in got] == [o["line"] for o in want]


@case
def test_merged_percentiles_weigh_each_side_by_its_calls(c):
    import random as _r
    rng = _r.Random(1)
    a, b = olp.Bucket(), olp.Bucket()
    for _ in range(100000):
        a.add(0.5, 0.5, 1, False, rng)
    for _ in range(3000):
        b.add(5.0, 5.0, 1, False, rng)
    a.merge(b, rng)
    assert a.pct(95) == 1.0 and a.seen == 103000, a.pct(95)


@case
def test_exception_classes_are_counted_per_database_and_per_entry(c):
    log = ("2026-09-14 13:00:00,000 7 ERROR db odoo.sql_db: bad query\n"
           "2026-09-14 13:00:01,000 7 WARNING db py.warnings: something\n"
           "UserWarning: unrelated\n"
           "2026-09-14 13:00:02,000 7 ERROR db odoo.http: boom\n"
           "Traceback (most recent call last):\n"
           "ValueError: x\n")
    a = web.state._args()
    a.events = True
    p = olp.Profile(a)
    p.feed(io.StringIO(log))
    assert dict(p.parts["db"].exceptions) == {"ValueError": 1}, dict(p.parts["db"].exceptions)


@case
def test_logged_details_are_counted_in_a_fixed_order(c):
    # what the lines of a request name is a set of strings, whose own order differs from one process (a parser
    # worker) to the next: counted as it comes, equally frequent values listed differently on every run
    text = "".join("2026-09-14 13:05:09,%03d 4242 INFO mydb odoo.api: call sale.order(%d,).read()\n" % (i, 97 * i % 31)
                   for i in range(30)) + (RPC % b"0.2").decode()
    recs = list(_profile(text)[1].parts["mydb"].op_logged[OP]["logged records"])
    assert len(recs) == 30 and recs == sorted(recs), recs


@case
def test_equally_slow_lines_list_in_the_order_the_files_were_loaded(c):
    names = ["first.log", "second.log", "third.log", "fourth.log"]
    for name in names:
        upload(c, name, RPC % b"0.2")
    analyze(c)
    out = c.get("/api/op_lines?order=slow&by=operation&name=" + OP.replace(" ", "%20")).get_json()
    assert [o["file_name"] for o in out] == names, out                    # (not in the order of their random ids)
