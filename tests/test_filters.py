"""Fact filters: picking values behind a row keeps only the occurrences that carry them; one client IP.
"""

from __future__ import annotations


from support import (
    AUTH, CONFIRM, CRON, FF_LOG, LOGGED_LOG, LOGIN, OP, _ff, _post_domain, _profile, analyze, case, detail, upload,
)

from logtool import web


@case
def test_fact_filter_keeps_only_matching_requests(c):
    upload(c, "f.log", FF_LOG)
    calls = lambda j: {r["operation"]: r["calls"] for r in j["view"]["rows"]}.get(OP, 0)
    assert calls(analyze(c)[1]) == 3
    one = _ff(("operation", OP, {"client IP": ["10.0.0.2"]}))
    j = analyze(c, one)[1]
    assert calls(j) == 1 and j["view"]["totals"]["calls"] == 1, j.get("error")
    assert calls(analyze(c, _ff(("operation", OP, {"client IP": ["10.0.0.2", "10.0.0.3"]})))[1]) == 2      # OR in a detail
    assert calls(analyze(c, _ff(("app", "stock", {"client IP": ["10.0.0.9"]})))[1]) == 0                   # an app's filter too
    s, d = detail(c, "operation", OP, one)
    ip = [f for f in d["facts"] if f["detail"] == "client IP"][0]
    assert d["found"] and ip["unfiltered"] and ip["distinct"] == 3, d          # the filtered detail still offers every value
    lines = c.get("/api/op_lines?by=operation&name=%s%s" % (OP.replace(" ", "%20"), one)).get_json()
    assert len(lines) == 1, lines
    w = analyze(c)[1]["window"]
    j = analyze(c, one + "&from=%d&to=%d" % (w[0], w[0] + 600))[1]              # a filtered interval, cut from filtered chunks
    assert calls(j) == 1, j.get("error")
    assert c.get("/api/analyze?ff=nope").status_code == 400
    assert c.get("/api/analyze?init=1" + _ff(("bogus", "x", {"a": ["b"]}))).status_code == 400


@case
def test_fact_filter_on_a_span_of_durations(c):
    from urllib.parse import quote
    upload(c, "f.log", FF_LOG)                                  # calls of 300, 400 and 500 ms
    upload(c, "g.log", CRON)                                    # a 250 ms cron run
    calls = lambda q, op=OP: {r["operation"]: r["calls"] for r in analyze(c, q)[1]["view"]["rows"]}.get(op, 0)
    assert calls(_ff(("operation", OP, {"duration ms": ["300-500"]}))) == 2          # lo included, hi not
    assert calls(_ff(("operation", OP, {"duration ms": ["0-300", "500-"]}))) == 1     # OR, open-ended
    d = detail(c, "operation", OP, _ff(("operation", OP, {"duration ms": ["500-"]})))[1]
    assert d["calls"] == 1 and d["durations"]["unfiltered"] and sum(b[2] for b in d["durations"]["bins"]) == 3, d    # every bar
    assert calls(_ff(("family", "cron", {"duration ms": ["0-200"]})), "cron: Credit Check") == 0
    assert calls(_ff(("family", "cron", {"duration ms": ["200-300"]})), "cron: Credit Check") == 1
    assert c.get("/api/analyze?init=1" + _ff(("operation", OP, {"duration ms": ["slow"]}))).status_code == 400
    text = '["|", ["operation", "!=", "%s"], ["duration ms", "in", ["300-500"]]]' % OP
    assert _post_domain(c, text)[1]["state"]["ff"][0]["facts"] == {"duration ms": ["300-500"]}
    xd = _post_domain(c, '[["duration ms", "not in", ["300-500"]]]')[1]["state"]["xd"]     # a custom condition
    assert calls("&xd=" + quote(__import__("json").dumps(xd))) == 1
    assert _post_domain(c, '[["duration ms", ">", "300-500"]]')[0] == 400


@case
def test_fact_filter_on_a_warning_reads_its_traceback(c):
    upload(c, "f.log", FF_LOG)
    key = ["WARNING", "odoo.models", "Order # is odd"]
    n = lambda j: sum(i["n"] for i in j["view"]["issues"] if [i["level"], i["logger"], i["message"]] == key)
    assert n(analyze(c)[1]) == 2
    assert n(analyze(c, _ff(("issue", key, {"Order #": ["17"]})))[1]) == 1
    rec = _ff(("issue", key, {"records": ["sale.order(18)"]}))                    # named only in the traceback
    assert n(analyze(c, rec)[1]) == 1
    q = "level=WARNING&logger=odoo.models&msg=Order%20%23%20is%20odd"
    assert len(c.get("/api/issue_lines?" + q + rec).get_json()) == 1
    d = c.get("/api/issue_detail?" + q + rec).get_json()
    assert d["found"] and any(f["detail"] == "records" and f["unfiltered"] for f in d["facts"]), d


@case
def test_one_client_ip_narrows_the_whole_report(c):
    from urllib.parse import quote
    upload(c, "l.log", LOGGED_LOG + CRON + b"2026-09-14 13:09:00,000 1 WARNING mydb odoo.modules: Startup thing\n")
    j = analyze(c)[1]
    assert {r["operation"] for r in j["view"]["rows"]} == {CONFIRM, "cron: Credit Check"} and len(j["view"]["issues"]) == 2
    for ip, calls, issues in (("10.0.0.2", 1, ["Access denied (id=#)"]), ("10.0.0.1", 1, []), ("10.9.9.9", 0, [])):
        j = analyze(c, "&ip=" + ip)[1]      # its requests and what was logged during them: no cron, no unclaimed warning
        assert {r["operation"]: r["calls"] for r in j["view"]["rows"]} == ({CONFIRM: calls} if calls else {}), (ip, j["view"]["rows"])
        assert [i["message"] for i in j["view"]["issues"]] == issues, ip
        q = "level=WARNING&logger=odoo.models&msg=" + quote("Access denied (id=#)") + "&ip=" + ip
        assert len(c.get("/api/issue_lines?" + q).get_json()) == len(issues), ip
    assert len(c.get("/api/op_lines?by=operation&name=%s&ip=10.0.0.3" % quote(CONFIRM)).get_json()) == 1
    assert c.get("/api/op_lines?by=operation&name=%s&ip=10.0.0.3" % quote("cron: Credit Check")).get_json() == []
    text = c.get("/api/domain?ip=10.0.0.2").get_json()["domain"]
    assert '["ip", "in", ["10.0.0.2"]]' in text, text
    assert _post_domain(c, text)[1]["state"]["ip"] == ["10.0.0.2"]
    assert _post_domain(c, '[["ip", "=", "10.0.0.2"], ["ip", "in", ["10.0.0.1", "10.0.0.2"]]]')[1]["state"]["ip"] == ["10.0.0.2"]
    assert _post_domain(c, '[["ip", "=", "10.0.0.2"], ["ip", "=", "10.0.0.1"]]')[0] == 400
    assert _post_domain(c, '[["ip", "!=", "10.0.0.2"]]')[0] == 400
    assert _post_domain(c, '["|", ["ip", "=", "10.0.0.2"], ["kind", "=", "job"]]')[0] == 400


@case
def test_one_client_ip_keeps_only_its_logins_and_held_connections(c):
    ws = (b'2026-09-14 13:05:30,000 4300 INFO mydb werkzeug: 10.0.0.1 - - [14/Sep/2026 13:05:30] '
          b'"GET /websocket HTTP/1.1" 101 - 2 0.01 300.0\n')
    upload(c, "a.log", LOGIN + AUTH + ws + AUTH.replace(b"10.0.0.1", b"10.0.0.2").replace(b"13:05:07,100", b"13:05:40,000"))
    assert analyze(c)[1]["view"]["stats"]["held_n"] == 1
    j = analyze(c, "&ip=10.0.0.2")[1]
    assert j["view"]["stats"]["held_n"] == 0 and j["view"]["stats"]["held_s"] == 0, j["view"]["stats"]      # the other client's WebSocket
    e = __import__("json").loads(c.get("/api/export?ip=10.0.0.2").get_data())   # jane logged in from 10.0.0.1, not from it
    assert [l["result"] for l in e["logins"]] == ["?"] and all(not cl["logins"] for cl in e["clients"]), (e["logins"], e["clients"])


@case
def test_a_long_traceback_still_names_its_exception_under_a_filter(c):
    text = ("2026-09-14 13:00:01,000 7 ERROR db odoo.models: odd thing\nTraceback (most recent call last):\n" +
            "".join('  File "x.py", line %d, in f\n' % i for i in range(70)) + "ValueError: boom\n" +
            "2026-09-14 13:00:02,000 7 INFO db werkzeug: 1.1.1.1 - - [x] \"GET /web/x HTTP/1.1\" 200 - 1 0.1 0.1\n")
    ff = {"operation": {"nothing": {"client IP": frozenset(["9.9.9.9"])}}}
    for f in (None, ff):
        p = web.selection._merge([_profile(text, f)[1].parts], frozenset())
        assert [e[4] for e in p.issues.values()] == ["ValueError"], f
