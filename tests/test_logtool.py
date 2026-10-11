#!/usr/bin/env python3
"""
Edge-case and consistency tests for the log tool. No test framework needed:

    python tests/test_logtool.py            # everything
    python tests/test_logtool.py gz chunk   # only the cases whose name contains one of these words

The HTTP cases go through Flask's test client (no network). The consistency case checks that a time
interval answered from cached chunks equals a straight re-parse of the same window.
"""

from __future__ import annotations

import bz2
import gzip
import io
import os
import random
import sys
import threading

os.environ.setdefault("LOGTOOL_PARSE_CACHE", "0")       # every case parses for real (one case turns the cache on)
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from logtool import app as A                      # noqa: E402
from logtool import profiler as olp                # noqa: E402

SAMPLE = os.path.join(HERE, "data", "sample-19.0.log")
LINE = (b'2026-09-14 13:05:09,123 4242 INFO mydb werkzeug: 10.0.0.1 - - [14/Sep/2026 13:05:09] '
        b'"GET /web/x HTTP/1.1" 200 - 5 0.1 0.2\n')

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def fresh():
    c = A.app.test_client()
    assert c.post("/api/reset").status_code == 200
    return c


def upload(c, name, data):
    return c.post("/api/files", data={"files": (io.BytesIO(data), name)}, content_type="multipart/form-data")


def analyze(c, query=""):
    r = c.get("/api/analyze?init=1" + query)
    return r.status_code, r.get_json() or {}


def sample():
    with open(SAMPLE, "rb") as f:
        return f.read()


# --------------------------------------------------------------------------- #
# files that are not what they should be
# --------------------------------------------------------------------------- #

@case
def test_good_log_loads(c):
    upload(c, "a.log", sample())
    status, j = analyze(c)
    assert status == 200 and j["view"]["stats"]["requests"] > 0, (status, j)


@case
def test_empty_binary_and_text_files_are_reported_not_fatal(c):
    for name, data in (("e.log", b""), ("b.log", os.urandom(100000)), ("t.log", b"hello\n" * 500)):
        c = fresh()
        upload(c, name, data)
        status, j = analyze(c)
        assert status == 400 and "Odoo log lines" in j["error"], (name, status, j)
        assert c.get("/api/files").get_json()[0]["problem"], name


@case
def test_corrupt_gz_does_not_break_the_good_file(c):
    upload(c, "bad.gz", b"this is not gzip data")
    upload(c, "a.log", sample())
    status, j = analyze(c)
    assert status == 200, j
    assert [bool(f["problem"]) for f in j["files"]] == [True, False], j["files"]


@case
def test_truncated_gz_keeps_what_was_read(c):
    upload(c, "t.log.gz", gzip.compress(sample())[:20000])
    status, j = analyze(c)
    assert status == 200 and j["view"]["stats"]["requests"] > 0, (status, j)
    assert "EOFError" in j["files"][0]["problem"], j["files"]


@case
def test_gz_and_bz2_read_like_plain(c):
    data = sample()
    plain = analyze(upload(c, "a.log", data) and c)[1]["view"]["stats"]["requests"]
    c = fresh()
    upload(c, "a.log.gz", gzip.compress(data))
    upload(c, "b.log.bz2", bz2.compress(data))
    assert analyze(c)[1]["view"]["stats"]["requests"] == 2 * plain


@case
def test_utf8_in_gz_is_not_decoded_with_the_locale_codec(c):
    data = LINE + "2026-09-14 13:05:10,000 1 WARNING mydb odoo.x: café €\n".encode() + LINE
    upload(c, "u.log", data)
    upload(c, "u.log.gz", gzip.compress(data))
    a, b = (c.get("/api/analyze?init=1&exclude_file=" + f["id"]).get_json()["view"]["issues"][0]["message"]
            for f in c.get("/api/files").get_json())
    assert a == b, (a, b)


@case
def test_crlf_and_latin1_bytes(c):
    upload(c, "crlf.log", LINE.replace(b"\n", b"\r\n") * 50)
    assert analyze(c)[1]["view"]["stats"]["requests"] == 50
    c = fresh()
    upload(c, "l.log", LINE + "2026-09-14 13:05:10,000 1 WARNING mydb odoo.x: caf\xe9 \xff\n".encode("latin-1") + LINE)
    assert analyze(c)[0] == 200


@case
def test_very_long_line(c):
    upload(c, "long.log", LINE + b"x" * (2 << 20) + b"\n" + LINE)
    assert analyze(c)[1]["view"]["stats"]["requests"] == 2


@case
def test_many_distinct_operations(c):
    rnd = random.Random(1)
    lines = [b'2026-09-14 13:%02d:%02d,100 42 INFO mydb werkzeug: 10.0.0.1 - - [14/Sep/2026 13:05:09] '
             b'"GET /web/dataset/call_kw/x_model%d/meth%d HTTP/1.1" 200 - 5 0.1 %0.3f\n' % (i // 60 % 60, i % 60, i, i, rnd.random())
             for i in range(20000)]
    upload(c, "flood.log", b"".join(lines))
    status, j = analyze(c)
    assert status == 200 and j["view"]["stats"]["operations"] == 20000 and len(j["view"]["rows"]) == A.MAX_ROWS


# --------------------------------------------------------------------------- #
# header parsing
# --------------------------------------------------------------------------- #

@case
def test_no_db_line_with_a_colon_in_the_message_has_no_database(c):
    upload(c, "n.log", b"2026-09-14 13:00:00,100 77 INFO odoo.modules.loading: Loading: base\n" * 5 + LINE)
    dbs = [d for d, _ in analyze(c)[1]["databases"]]
    assert "odoo.modules.loading:" not in dbs and "mydb" in dbs, dbs


@case
def test_impossible_timestamps_are_not_fatal(c):
    upload(c, "d.log", LINE + b"2026-02-31 13:00:00,100 4242 INFO mydb werkzeug: x\n"
                       b"2026-09-14 25:00:00,100 4242 INFO mydb odoo.x: hello\n" + LINE)
    status, j = analyze(c)
    assert status == 200 and j["view"]["stats"]["requests"] == 2, (status, j)


# --------------------------------------------------------------------------- #
# the API
# --------------------------------------------------------------------------- #

@case
def test_bad_intervals_are_answered_not_crashed(c):
    upload(c, "a.log", sample())
    w = analyze(c)[1]["window"]
    for q, want in (("&from=abc&to=5", 400), ("&from=5", 200), ("&from=9&to=3", 400), ("&from=-5&to=0", 400),
                    ("&from=%d&to=%d" % (w[1] + 5, w[1] + 99), 400),
                    ("&from=%d&to=%d" % (w[0] - 10 ** 12, w[1] + 10 ** 12), 200),
                    ("&from=%d&to=%d" % (w[0], w[0] + 1), 200)):
        r = c.get("/api/analyze?" + q)
        assert r.status_code == want, (q, r.status_code, r.get_data()[:200])


@case
def test_unknown_ids_and_exclusions(c):
    upload(c, "a.log", sample())
    fid = c.get("/api/files").get_json()[0]["id"]
    for q in ("&exclude_file=nope", "&exclude_db=zzz"):
        assert c.get("/api/analyze?" + q).status_code == 200, q
    assert c.get("/api/analyze?exclude_file=" + fid).status_code == 400          # nothing left to show
    assert c.delete("/api/files/nope").status_code == 404
    assert upload(c, "", b"x").status_code == 200


@case
def test_every_database_excluded_shows_everything(c):
    upload(c, "a.log", sample())
    dbs = [d for d, _ in analyze(c)[1]["databases"]]
    r = c.get("/api/analyze?" + "&".join("exclude_db=" + d for d in dbs))
    assert r.status_code == 200 and r.get_json()["excluded_db"] == [], r.get_json()["excluded_db"]


@case
def test_cross_origin_writes_and_foreign_hosts_are_refused(c):
    assert c.post("/api/reset", headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/reset", headers={"Origin": "http://localhost"}).status_code == 200
    A.app.config["LOOPBACK_ONLY"] = True
    try:
        assert c.get("/api/files", headers={"Host": "evil.example"}).status_code == 403
        assert c.get("/api/files", headers={"Host": "localhost:5000"}).status_code == 200
    finally:
        A.app.config["LOOPBACK_ONLY"] = False


@case
def test_unloading_frees_every_cache(c):
    for i in range(4):
        upload(c, "a%d.log" % i, sample()[:150000])
    analyze(c)
    for f in c.get("/api/files").get_json():
        c.delete("/api/files/" + f["id"])
    caches = {n: len(getattr(A, n)) for n in ("CHUNKS", "INDEX", "SLICES", "BASE", "FULL", "CUTS")}
    assert not any(caches.values()), caches


@case
def test_concurrent_analyses_agree(c):
    upload(c, "a.log", sample())
    w = analyze(c)[1]["window"]
    got = {}

    def hit(i):
        lo = w[0] + (i % 4) * 600
        r = A.app.test_client().get("/api/analyze?from=%d&to=%d" % (lo, lo + 3600))
        got[i] = (lo, r.status_code, r.get_json()["view"]["stats"]["requests"])

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert all(s == 200 for _, s, _ in got.values()), got
    by_start = {}
    for lo, _, n in got.values():
        assert by_start.setdefault(lo, n) == n, got               # same interval, same answer


# --------------------------------------------------------------------------- #
# details popup: example lines and untimed lines matched to an operation
# --------------------------------------------------------------------------- #

CALL = (b"2026-09-14 13:05:08,900 4242 DEBUG mydb odoo.api: call stock.valuation.layer().search_read("
        b"[['create_date', '>', '12/31/2024']], fields=['create_date', 'product_id'])\n")
RPC = (b'2026-09-14 13:05:09,123 4242 INFO mydb werkzeug: 10.0.0.1 - - [14/Sep/2026 13:05:09] '
       b'"POST /web/dataset/call_kw/stock.valuation.layer/search_read HTTP/1.1" 200 - 5 0.1 %s\n')
OP = "call_kw stock.valuation.layer.search_read"


def detail(c, by, name, query=""):
    r = c.get("/api/detail?by=%s&name=%s%s" % (by, name.replace(" ", "%20"), query))
    return r.status_code, r.get_json() or {}


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


# --------------------------------------------------------------------------- #
# what grouping collapses (users, records, clients), raw lines of an operation, the export
# --------------------------------------------------------------------------- #

LOGIN = b"2026-09-14 13:05:07,000 4242 INFO mydb odoo.addons.base.models.res_users: Login successful for login:jane@example.com from n/a\n"
AUTH = (b'2026-09-14 13:05:07,100 4242 INFO mydb werkzeug: 10.0.0.1 - - [14/Sep/2026 13:05:07] '
        b'"POST /web/session/authenticate HTTP/1.1" 200 - 5 0.01 0.02\n')
DENIED = (b"2026-09-14 13:06:00,000 5151 WARNING mydb odoo.http: Uh-oh! Looks like you have stumbled upon some top-secret records.\n"
          b"\n"
          b"Sorry, Jane (id=69) doesn't have 'read' access to:\n"
          b"- Sales Order, S00042 (sale.order: 42)\n"
          b'2026-09-14 13:06:00,100 5151 INFO mydb odoo.http.server: 10.0.0.7 BK1AC94d - [14/Sep/2026 13:06:00] '
          b'"POST /mail/data HTTP/1.1" 200 7454 3 0.01 0.02 rw\n')
IMG = (b'2026-09-14 13:07:00,000 4343 INFO mydb werkzeug: 10.0.0.2 - - [14/Sep/2026 13:07:00] '
       b'"GET /web/image/res.partner/%s/avatar_128 HTTP/1.1" 200 - 2 0.01 %s\n')
CRON = (b"2026-09-14 13:08:00,000 6000 INFO mydb odoo.addons.base.models.ir_cron: Job 'Credit Check' (17) fully done "
        b"(#loop 1; done 3; remaining 0; duration 0.25s)\n")


def facts(lst):
    return {f["detail"]: {v[0]: v for v in f["values"]} for f in lst}


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
def test_export_pseudonymizes_and_explains(c):
    many = b"".join(b"2026-09-14 13:09:%02d,000 7 WARNING mydb odoo.x: distinct message %s\n" % (i, b"x" * (i + 1))
                    for i in range(59)) + b"2026-09-14 13:09:59,000 7 WARNING mydb odoo.y: another one\n" * 3
    upload(c, "f.log", LOGIN + AUTH + DENIED + IMG % (b"7", b"0.1") + CRON + many + b"2026-09-14 13:10:00,000 7 ERROR mydb odoo.z: rare\n")
    analyze(c)
    e = c.get("/api/export").get_json()
    text = str(e)
    assert "10.0.0." not in text and "jane@" not in text and "BK1AC94d" not in text and "mydb" not in text, text[:2000]
    assert e["issues_distinct"] == len(e["issues"]) > A.MAX_ISSUES, (e["issues_distinct"], len(e["issues"]))
    i = next(i for i in e["issues"] if "top-secret" in i["message"])
    f = facts(i["facts"])
    assert "69" in f["user id"] and [k[:3] for k in f["client IP"]] == ["ip-"], f
    assert any(l["login"] == "user-1" and l["result"] == "successful" and l["first"] for l in e["logins"]), e["logins"]
    assert any(cl["logins"] == ["user-1"] for cl in e["clients"]), e["clients"]
    assert any(o["operation"] == "cron: Credit Check" for o in e["hist"]["ops"]), e["hist"]["ops"]
    row = next(r for r in e["rows"] if r["operation"] == "cron: Credit Check")
    assert row["first_seen"] == "2026-09-14T13:08" and row["active_minutes"] == 1 and facts(row["facts"])["cron id"], row
    assert e["log_levels"]["INFO"] and any("DEBUG" in l for l in e["limits"]), e["limits"]
    assert all(k in e["fields"] for k in ("facts", "clients", "limits")), e["fields"]
    c.post("/api/reset")                                              # an export imports again, facts included
    upload(c, "e.json", str.encode(__import__("json").dumps(e)))
    j = analyze(c)[1]
    assert j.get("imported") and j["view"]["rows"][0].get("facts") is not None, j.get("error")


# --------------------------------------------------------------------------- #
# fact filters: picking values behind a row keeps only the occurrences that carry them
# --------------------------------------------------------------------------- #

def _ff(*items):
    from urllib.parse import quote
    return "&ff=" + quote(__import__("json").dumps([{"by": b, "name": n, "facts": f} for b, n, f in items]))


FF_LOG = (RPC % b"0.2" + RPC.replace(b"10.0.0.1", b"10.0.0.2") % b"0.3" +
          RPC.replace(b"10.0.0.1", b"10.0.0.3").replace(b"13:05:09", b"13:25:09") % b"0.4" +
          b"2026-09-14 13:30:00,000 4242 WARNING mydb odoo.models: Order 17 is odd\n" +
          b"2026-09-14 13:30:01,000 4242 WARNING mydb odoo.models: Order 18 is odd\nTraceback x\n  sale.order(18,)\n")


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
def test_focused_timeline_has_minute_buckets(c):
    upload(c, "f.log", FF_LOG)
    w = analyze(c)[1]["window"]
    t = c.get("/api/timeline?from=%d&to=%d&issues=%s" % (w[0], w[0] + 1800, "[[%22WARNING%22,%22odoo.models%22,%22Order%20%23%20is%20odd%22]]")).get_json()
    assert t["base"] == 1 and sum(e[1] for e in t["all"]) == 3, t
    assert len(t["issues"]) == 1 and sum(e[1] for e in t["issues"][0]["hist"]) == 2, t
    assert c.get("/api/timeline?issues=zz").status_code == 400


# --------------------------------------------------------------------------- #
# what a request's own lines name (users, records): details of the request, filters
# --------------------------------------------------------------------------- #

CONFIRM = "call_button sale.order.action_confirm"
LOGGED_LOG = (
    b"2026-09-14 13:05:08,100 4242 DEBUG mydb odoo.api: call sale.order(42,).action_confirm()\n"
    b"2026-09-14 13:05:09,123 4242 INFO mydb werkzeug: 10.0.0.1 - - [14/Sep/2026 13:05:09] "
    b'"POST /web/dataset/call_button/sale.order/action_confirm HTTP/1.1" 200 - 5 0.1 0.2\n'
    b"2026-09-14 13:06:08,100 4243 WARNING mydb odoo.models: Access denied (id=7)\n"
    b"Traceback x\n  sale.order(43,)\n"
    b"2026-09-14 13:06:09,123 4243 INFO mydb werkzeug: 10.0.0.2 - - [14/Sep/2026 13:06:09] "
    b'"POST /web/dataset/call_button/sale.order/action_confirm HTTP/1.1" 200 - 5 0.1 0.2\n'
    b"2026-09-14 13:07:09,123 4244 INFO mydb werkzeug: 10.0.0.3 - - [14/Sep/2026 13:07:09] "
    b'"POST /web/dataset/call_button/sale.order/action_confirm HTTP/1.1" 200 - 5 0.1 0.2\n')


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
def test_another_instance_keeps_a_running_ones_files(c):
    import tempfile
    base = tempfile.mkdtemp()
    live, dead, old = (os.path.join(base, n) for n in ("logtool_live", "logtool_dead", "logtool_old"))
    for d, pid in ((live, os.getppid()), (dead, 2 ** 22 + 12345)):          # the parent: another live process
        os.makedirs(d)
        with open(os.path.join(d, A.OWNER_FILE), "w") as fh:
            fh.write(str(pid))
    os.makedirs(old)
    os.utime(old, (0, 0))
    assert not A._abandoned(live) and A._abandoned(dead) and A._abandoned(old)
    # and a file that disappeared says so, instead of "no occurrence found"
    path = os.path.join(base, "gone.log")
    with open(path, "wb") as fh:
        fh.write(RPC % b"0.2")
    rec = A._add_file(path, "gone.log", False)
    analyze(c)
    os.remove(rec["path"])
    r = c.get("/api/op_lines?by=operation&name=%s" % OP.replace(" ", "%20"))
    assert r.status_code == 400 and "no longer on disk" in r.get_json()["error"], r.get_json()


# --------------------------------------------------------------------------- #
# the global filter as a domain
# --------------------------------------------------------------------------- #

def _post_domain(c, text):
    r = c.post("/api/domain", json={"domain": text})
    return r.status_code, r.get_json() or {}


@case
def test_domain_round_trips_the_apps_filters(c):
    upload(c, "f.log", FF_LOG)
    fid = analyze(c)[1]["files"][0]["id"]
    w = analyze(c)[1]["window"]
    q = ("?exclude_db=-&from=%d&to=%d" % (w[0], w[0] + 600) +
         _ff(("operation", OP, {"client IP": ["10.0.0.2"]}), ("issue", ["WARNING", "odoo.models", "Order # is odd"], {"Order #": ["17"]})))
    text = c.get("/api/domain" + q + "&xd=" + __import__("urllib.parse").parse.quote('[["http_status", ">=", 500]]')).get_json()["domain"]
    assert '"|", ["operation", "!=", "%s"], ["client IP", "in", ["10.0.0.2"]]' % OP in text, text
    assert '["database", "not in", [false]]' in text and '["http_status", ">=", 500]' in text, text
    s, d = _post_domain(c, text)
    st = d["state"]
    assert s == 200 and st["exclude_db"] == [] and st["interval"] == [w[0], w[0] + 600], d
    assert st["xd"] == [["http_status", ">=", 500]] and len(st["ff"]) == 2, st
    assert _post_domain(c, '[["database", "=", "other"]]')[1]["error"].startswith("no database is named")
    assert st["exclude_file"] == [] and _post_domain(c, '[["file", "not in", ["f.log"]]]')[0] == 400    # not every file
    assert _post_domain(c, '[["file", "=", "nope.log"]]')[1]["error"].startswith("no file is named")
    assert _post_domain(c, '["|", ["time", ">", "2026-01-01 00:00"], ["kind", "=", "job"]]')[0] == 400
    assert _post_domain(c, '[["duration", ">", 1], ["bogus", "=", 1]]')[1]["error"].startswith("unknown field")
    assert _post_domain(c, '("&", ("duration", ">", 1), ("kind", "=", "request"))')[0] == 200         # a Python literal too
    st = _post_domain(c, '[["operation", "not in", ["%s"]], ["panels.hidden", "=", ["selected"]]]' % OP)[1]["state"]
    assert st["xd"] == [["operation", "not in", [OP]]] and st["view"]["panels_hidden"] == ["selected"], st   # no exclusion feature: a condition
    j = analyze(c, "&xd=" + __import__("urllib.parse").parse.quote(__import__("json").dumps(st["xd"])))[1]
    assert OP not in {r["operation"] for r in j["view"]["rows"]}
    empty = _post_domain(c, "[]")[1]["state"]
    assert {k: v for k, v in empty.items() if k != "view"} == {"exclude_file": [], "exclude_db": [], "interval": None,
                                                              "ff": [], "xd": None}
    assert empty["view"]["total"] == {"bars": ["requests"], "lines": ["avg_ms"]} and empty["view"]["bucket"] == "auto"


@case
def test_domain_carries_the_whole_view(c):
    from urllib.parse import quote
    upload(c, "f.log", FF_LOG)
    view = {"items": [{"operation": OP, "bars": ["requests", "total_ms.sql"], "lines": ["avg_ms.py"]},
                      {"search": "Stock", "bars": [], "lines": ["requests"]},
                      {"issue": ["WARNING", "odoo.models", "Order # is odd"], "bars": ["requests"], "lines": []}],
            "total": {"bars": ["total_ms"], "lines": []}, "bucket": 5, "fit": False, "focus": [1789391100, 1789392900],
            "timezone": "Europe/Brussels", "table": {"group": "app", "search": "stock", "sort": ["calls", "asc"], "hidden_columns": ["p95_s"]},
            "errors": {"level": "warnings", "search": "odd"}, "panels_hidden": ["errors"]}
    r = c.get("/api/domain?view=" + quote(__import__("json").dumps(view))).get_json()
    text = r["domain"]
    assert '["chart.bucket", "=", 5]' in text and '["chart.focus", ">=", ' in text and '["panels.hidden", "=", ["errors"]]' in text, text
    back = _post_domain(c, text)[1]["state"]["view"]
    assert back == view, back
    default = _post_domain(c, c.get("/api/domain").get_json()["domain"])[1]["state"]["view"]
    assert default["table"]["group"] == "operation" and default["items"] == [] and default["fit"] is True
    items_only = _post_domain(c, '[["chart.item", "=", {"app": "stock"}]]')[1]["state"]["view"]
    assert items_only["total"] == {"bars": [], "lines": []} and items_only["items"][0]["lines"] == ["avg_ms"]
    for bad in ('[["chart.item", "=", {"issue": ["W", "l", "m"], "lines": ["avg_ms"]}]]', '[["chart.bucket", "=", 7]]',
                '["|", ["chart.fit", "=", true], ["kind", "=", "job"]]', '[["chart.focus", ">=", "2026-09-14 13:00:00"]]',
                '[["table.sort", "=", ["nope", "asc"]]]', '[["chart.fit", "in", [true]]]'):
        s, d = _post_domain(c, bad)
        assert s == 400, (bad, d)
    assert c.get("/api/domain?view=" + quote('{"bucket": 7}')).status_code == 400


@case
def test_domain_conditions_filter_every_occurrence(c):
    from urllib.parse import quote
    upload(c, "f.log", FF_LOG)
    calls = lambda j: {r["operation"]: r["calls"] for r in j["view"]["rows"]}.get(OP, 0)
    issues = lambda j: sum(i["n"] for i in j["view"]["issues"])
    xd = lambda d: "&xd=" + quote(d)
    j = analyze(c, xd('[["python_s", ">", 0.25]]'))[1]                # requests with 0.3 and 0.4 s of Python
    assert calls(j) == 2 and issues(j) == 2, (calls(j), issues(j))     # warnings have no python_s: kept
    j = analyze(c, xd('[["kind", "!=", "issue"]]'))[1]
    assert calls(j) == 3 and issues(j) == 0
    j = analyze(c, xd('[["records", "ilike", "sale.order(18%"]]'))[1]   # a record named only in the traceback
    assert issues(j) == 1 and calls(j) == 3
    j = analyze(c, xd('["!", ["client IP", "in", ["10.0.0.1", "10.0.0.3"]]]'))[1]
    assert calls(j) == 1
    q = "level=WARNING&logger=odoo.models&msg=Order%20%23%20is%20odd" + xd('[["text", "like", "17"]]')
    assert len(c.get("/api/issue_lines?" + q).get_json()) == 1
    assert len(c.get("/api/op_lines?by=operation&name=%s%s" % (OP.replace(" ", "%20"), xd('[["python_s", ">", 0.25]]'))).get_json()) == 2
    assert c.get("/api/analyze?xd=" + quote('[["time", ">", "2026-01-01 00:00"]]')).status_code == 400


# --------------------------------------------------------------------------- #
# a time interval read from cached chunks equals a re-parse of that window
# --------------------------------------------------------------------------- #

@case
def test_interval_from_chunks_matches_a_straight_parse(c):
    saved = olp.PAR_MIN_TOTAL, olp.PAR_MIN_CHUNK, olp.PAR_MAX_CHUNK, olp._cpus
    olp.PAR_MIN_TOTAL, olp.PAR_MIN_CHUNK, olp.PAR_MAX_CHUNK, olp._cpus = 1 << 16, 1 << 15, 1 << 17, lambda: 4   # force several chunks
    try:
        src = SAMPLE
        rec = A._add_file(src, "sample.log", False)
        fid = rec["id"]
        window = analyze(c)[1]["window"]
        assert len(A.CHUNKS[fid]) > 3, len(A.CHUNKS[fid])
        rnd = random.Random(7)

        def rows(prof):
            return {r["operation"]: (r["calls"], r["total_s"], r["sql_s"], r["python_s"], r["queries"], r["max_s"])
                    for r in A._op_rows(prof)}

        for _ in range(25):
            lo = rnd.randrange(window[0], window[1] - 1)
            hi = min(window[1] - 1, lo + rnd.choice([1, 45, 600, 3600, 20000, 10 ** 6]))
            if hi <= lo:
                continue
            got = A.analyze(set(), set(), (lo, hi), False)
            a = A._args()
            a.since, a.until = A._iso(lo), A._iso(hi - 1)
            want = A._merge([olp.profile_files(a, [src], jobs=1).parts], frozenset())
            have = A.CUT_BASE[((fid,), frozenset(), lo, hi, None)]
            assert rows(have) == rows(want), (lo, hi)
            assert got["view"]["stats"]["requests"] == A._summary(want, [])["stats"]["requests"], (lo, hi)
    finally:
        olp.PAR_MIN_TOTAL, olp.PAR_MIN_CHUNK, olp.PAR_MAX_CHUNK, olp._cpus = saved
        olp.shutdown_pool()



@case
def test_filtered_interval_from_chunks_matches_a_straight_parse(c):
    saved = olp.PAR_MIN_TOTAL, olp.PAR_MIN_CHUNK, olp.PAR_MAX_CHUNK, olp._cpus
    olp.PAR_MIN_TOTAL, olp.PAR_MIN_CHUNK, olp.PAR_MAX_CHUNK, olp._cpus = 1 << 16, 1 << 15, 1 << 17, lambda: 4
    try:
        rec = A._add_file(SAMPLE, "sample.log", False)
        fid = rec["id"]
        window = analyze(c)[1]["window"]
        ff = {"operation": {"static web": {"URL": frozenset(["/web/static/lib/odoo_ui_icons/fonts/odoo_ui_icons.woff2"])}}}
        flt = ("k", ff)
        rnd = random.Random(11)
        for _ in range(8):
            lo = rnd.randrange(window[0], window[1] - 1)
            hi = min(window[1] - 1, lo + rnd.choice([45, 600, 3600, 10 ** 6]))
            if hi <= lo:
                continue
            have = A._cut([rec], (fid,), frozenset(), lo, hi, flt)
            a = A._args(ff)
            a.since, a.until = A._iso(lo), A._iso(hi - 1)
            want = A._merge([olp.profile_files(a, [SAMPLE], jobs=1).parts], frozenset())
            got = {r["operation"]: r["calls"] for r in A._op_rows(have)}
            assert got == {r["operation"]: r["calls"] for r in A._op_rows(want)}, (lo, hi)
    finally:
        olp.PAR_MIN_TOTAL, olp.PAR_MIN_CHUNK, olp.PAR_MAX_CHUNK, olp._cpus = saved
        olp.shutdown_pool()


@case
def test_parallel_raw_line_scans_match_a_whole_file_scan(c):
    saved = olp.PAR_MIN_TOTAL, olp.PAR_MIN_CHUNK, olp.PAR_MAX_CHUNK, olp._cpus
    olp.PAR_MIN_TOTAL, olp.PAR_MIN_CHUNK, olp.PAR_MAX_CHUNK, olp._cpus = 1 << 16, 1 << 15, 1 << 17, lambda: 4
    try:
        rec = A._add_file(SAMPLE, "sample.log", False)
        fid = rec["id"]
        j = analyze(c)[1]
        assert len(A.CHUNKS[fid]) > 3
        with open(SAMPLE, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().split("\n")
        for row in j["view"]["rows"][:6]:
            name = row["operation"]
            got = c.get("/api/op_lines?by=operation&name=%s" % name.replace(" ", "%20").replace("#", "%23")).get_json()
            whole = olp.scan_op_occurrences(SAMPLE, "operation", name, set(j["excluded_db"]), None, None, 200)
            assert [o["line"] for o in got] == [o["line"] for o in whole], name
            for o in got[:20]:                                               # line numbers point at the right line
                assert lines[o["line"] - 1].startswith(o["stamp"].replace("T", " ")), (name, o)
        for row in j["view"]["issues"][:4]:
            q = "level=%s&logger=%s&msg=%s" % tuple(__import__("urllib.parse").parse.quote(x) for x in (row["level"], row["logger"], row["message"]))
            got = c.get("/api/issue_lines?" + q).get_json()
            assert len(got) == min(200, row["n"]), (row["message"], len(got), row["n"])
        o = got[-1]
        ctx = c.get("/api/issue_context?file=%s&line=%d&before=2&after=2" % (fid, o["line"])).get_json()
        assert ctx["lines"][ctx["match_at"]] == lines[o["line"] - 1].rstrip("\r"), ctx
    finally:
        olp.PAR_MIN_TOTAL, olp.PAR_MIN_CHUNK, olp.PAR_MAX_CHUNK, olp._cpus = saved
        olp.shutdown_pool()


# --------------------------------------------------------------------------- #
# regressions found in review
# --------------------------------------------------------------------------- #

def _profile(text, ff=None, start=0, end=None):
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "x.log")
    with open(path, "w") as fh:
        fh.write(text)
    return path, olp._work(A._args(ff), path, start, os.path.getsize(path) if end is None else end)[0]


@case
def test_merging_does_not_change_the_cached_pieces(c):
    w = "2026-09-14 13:00:01,000 7 WARNING db odoo.models: odd thing\n"
    _, a = _profile(w)
    _, b = _profile(w)
    for _ in range(3):
        m = A._merge([a.parts, b.parts], frozenset())
    key = ("WARNING", "odoo.models", "odd thing")
    assert list(a.parts["db"].issues[key][6].values()) == [[1, 0.0, 0.0, 0.0]]
    assert sum(v[0] for v in m.issues[key][6].values()) == m.issues[key][0] == 2


@case
def test_a_long_traceback_still_names_its_exception_under_a_filter(c):
    text = ("2026-09-14 13:00:01,000 7 ERROR db odoo.models: odd thing\nTraceback (most recent call last):\n" +
            "".join('  File "x.py", line %d, in f\n' % i for i in range(70)) + "ValueError: boom\n" +
            "2026-09-14 13:00:02,000 7 INFO db werkzeug: 1.1.1.1 - - [x] \"GET /web/x HTTP/1.1\" 200 - 1 0.1 0.1\n")
    ff = {"operation": {"nothing": {"client IP": frozenset(["9.9.9.9"])}}}
    for f in (None, ff):
        p = A._merge([_profile(text, f)[1].parts], frozenset())
        assert [e[4] for e in p.issues.values()] == ["ValueError"], f


@case
def test_chunks_are_cut_at_log_entries(c):
    import tempfile
    entry = ("2026-09-14 13:%02d:01,000 7 ERROR db odoo.models: odd thing\nTraceback (most recent call last):\n" +
             "".join('  File "x.py", line %d, in f\n' % i for i in range(40)) + "ValueError: boom\n" +
             "2026-09-14 13:%02d:02,000 7 INFO db werkzeug: 1.1.1.1 - - [x] \"GET /web/x HTTP/1.1\" 200 - 1 0.1 0.1\n")
    path = os.path.join(tempfile.mkdtemp(), "c.log")
    with open(path, "w") as fh:
        fh.write("".join(entry % (m, m) for m in range(60)))
    saved = olp.PAR_MIN_TOTAL, olp.PAR_MIN_CHUNK, olp.PAR_MAX_CHUNK, olp._cpus
    olp.PAR_MIN_TOTAL, olp.PAR_MIN_CHUNK, olp.PAR_MAX_CHUNK, olp._cpus = 1 << 10, 1 << 12, 1 << 13, lambda: 4
    try:
        chunks = {}
        olp.profile_files(A._args(), [path], chunks=chunks)
        parts = chunks[path]
        assert len(parts) > 5
        m = A._merge([ch.parts for ch in parts], frozenset())
        (key, e), = m.issues.items()
        assert e[0] == 60 and e[4] == "ValueError" and sorted(p for ch in parts for p in ch.parts) == ["db"] * len(parts), (e, [list(ch.parts) for ch in parts])
    finally:
        olp.PAR_MIN_TOTAL, olp.PAR_MIN_CHUNK, olp.PAR_MAX_CHUNK, olp._cpus = saved
        olp.shutdown_pool()


@case
def test_the_scanners_count_like_the_parser(c):
    text = ("2026-09-14 13:00:01,000 7 WARNING ? odoo.models: no database here\n"
            "2026-09-14 13:00:02,000 7 INFO db werkzeug: 1.1.1.1 - - [x] \"GET /websocket HTTP/1.1\" 101 - 1 0.01 300.0\n"
            "2026-09-14 13:01:03,000 8 INFO db odoo.addons.base_automation.models.base_automation: Starting time-based automation rule `N`.\n"
            "2026-09-14 13:01:08,000 8 INFO db odoo.addons.base_automation.models.base_automation: Time-based automation rule `N` done.\n")
    path, _p = _profile(text)
    assert olp.scan_issue_occurrences(path, "WARNING", "odoo.models", "no database here", {"-"}, None, None, 10) == []
    wsop = olp._line_operation("werkzeug", text.splitlines()[1].split("werkzeug: ")[1])[2]
    ws = olp.scan_op_occurrences(path, "operation", wsop, set(), None, None, 10)
    assert len(ws) == 1 and ws[0]["seconds"] < 1, ws                     # held open, not working
    auto = olp.scan_op_occurrences(path, "operation", "automation: N (time-based)", set(), None, None, 10)
    assert [o["seconds"] for o in auto] == [5.0], auto
    hold = {"extra": [["duration", ">", 10]]}
    assert olp.scan_op_occurrences(path, "operation", wsop, set(), None, None, 10, ff=hold) == []


@case
def test_a_detail_given_twice_in_a_domain_means_both(c):
    node = olp.dom_tree(olp.dom_parse_text('["|", ["operation", "!=", "X"], ["client IP", "=", "1.1.1.1"], '
                                           '"|", ["operation", "!=", "X"], ["client IP", "=", "2.2.2.2"]]'))
    parts = olp.dom_split(node)
    assert parts["facts"] == {("operation", "X"): {"client IP": {"1.1.1.1"}}} and parts["extra"] is not None, parts


@case
def test_malformed_domain_input_is_a_400(c):
    from urllib.parse import quote
    upload(c, "f.log", FF_LOG)
    deep = "[" + '"!", ' * 5000 + '["kind", "=", "job"]]'
    for r in (c.get("/api/analyze?xd=" + quote(deep)), c.get("/api/domain?view=" + quote('{"table": "x"}')),
              c.get("/api/domain?view=" + quote('{"items": 5}')), c.post("/api/domain", json={"domain": "{[1]: 2}"}),
              c.get("/api/analyze?ff=" + quote("[" * 5000))):
        assert r.status_code == 400, (r.status_code, r.get_data()[:200])


@case
def test_a_pinned_error_outside_the_top_60_is_still_sent(c):
    from urllib.parse import quote
    lines = []
    for i in range(70):                                   # 70 distinct messages; the rarest is not in the top 60
        lines += ["2026-09-14 13:%02d:01,000 7 WARNING db odoo.models: message number%s happened" % (i % 60, "x" * i)] * (100 - i)
    upload(c, "p.log", ("\n".join(lines) + "\n").encode())
    rare = ["WARNING", "odoo.models", "message number%s happened" % ("x" * 69)]
    j = analyze(c)[1]
    assert rare not in [[i["level"], i["logger"], i["message"]] for i in j["view"]["issues"]]
    j = analyze(c, "&pins=" + quote(__import__("json").dumps({"issues": [rare], "ops": ["nope"]})))[1]
    row = [i for i in j["view"]["issues"] if [i["level"], i["logger"], i["message"]] == rare]
    assert row and row[0]["n"] == 31 and row[0]["pinned"] and j["pinned_ops"] == [], row
    assert c.get("/api/analyze?pins=" + quote('{"issues": 5}')).status_code == 400


@case
def test_loading_the_same_logs_again_reads_the_parse_cache(c):
    import tempfile
    d = tempfile.mkdtemp()
    saved = A.PARSE_CACHE, A._parsed_dir, olp.profile_files
    A.PARSE_CACHE, A._parsed_dir = True, lambda: d
    try:
        upload(c, "a.log", sample())
        first = analyze(c)[1]
        assert len([n for n in os.listdir(d) if n.endswith(".pickle")]) == 1
        c.post("/api/reset")

        def no_parse(*a, **k):
            raise AssertionError("parsed again instead of reading the cache")
        olp.profile_files = no_parse
        upload(c, "again.log", sample())                       # same content, another name and path
        second = analyze(c)[1]
        assert second["view"]["rows"] == first["view"]["rows"] and second["view"]["stats"] == first["view"]["stats"]
        olp.profile_files = saved[2]
        upload(c, "b.log", sample() + LINE)                     # changed content: parsed (and cached) anew
        analyze(c)
        assert len([n for n in os.listdir(d) if n.endswith(".pickle")]) == 2
    finally:
        A.PARSE_CACHE, A._parsed_dir, olp.profile_files = saved


@case
def test_slowest_first_skips_fast_lines_but_finds_the_same_ones(c):
    r = random.Random(3)
    lines = [(b'2026-09-14 13:%02d:%02d,000 42 INFO db werkzeug: 10.0.0.1 - - [14/Sep/2026 13:00:00] '
              b'"POST /web/dataset/call_kw/sale.order/read HTTP/1.1" 200 - 5 %.3f %.3f\n'
              % (i // 60 % 60, i % 60, r.expovariate(20), r.expovariate(5))) for i in range(3000)]
    upload(c, "s.log", b"".join(lines))
    op = analyze(c)[1]["view"]["rows"][0]["operation"]
    got = c.get("/api/op_lines?by=operation&name=%s&order=slow" % op.replace(" ", "%20")).get_json()
    path = A.FILES[A.ORDER[-1]]["path"]
    want = olp.scan_op_occurrences(path, "operation", op, set(), None, None, 200, slowest=True)
    assert len(got) == 200 and [o["line"] for o in got] == [o["line"] for o in want]

@case
def test_only_db_reads_one_database_and_the_lines_without_one(c):
    other = (LINE.replace(b" mydb ", b" otherdb ") +
             b"2026-09-14 13:05:10,000 4243 ERROR otherdb odoo.http: Exception during request handling.\n"
             b"Traceback (most recent call last):\nValueError: boom\n")
    nodb = LINE.replace(b" mydb ", b" ? ")
    try:
        assert c.post("/api/only_db", json={"db": "two words"}).status_code == 400
        r = c.post("/api/only_db", json={"db": "mydb"}).get_json()
        assert r == {"db": "mydb", "changed": True}, r
        upload(c, "a.log", (LINE + other + nodb) * 50)
        st, j = analyze(c)
        assert st == 200 and [d for d, _ in j["databases"]] == ["mydb", "-"], j["databases"]
        assert not j["view"]["issues"], j["view"]["issues"]       # the other database's error and traceback are skipped
        alone = A._args()
        alone.only_db = None
        want = olp.Profile(alone)
        want.feed(io.StringIO(((LINE + nodb) * 50).decode()))
        assert sum(ch.header_ok for c_ in A.CHUNKS[A.ORDER[-1]] for ch in c_.parts.values()) == \
            sum(p.header_ok for p in want.parts.values())
        r = c.post("/api/only_db", json={"db": ""}).get_json()   # every database again: the loaded log is read again
        assert r == {"db": None, "changed": True}, r
        st, j = analyze(c)
        assert st == 200 and {d for d, _ in j["databases"]} == {"mydb", "otherdb", "-"}, j["databases"]
        assert c.get("/api/only_db").get_json() == {"db": None}
    finally:
        A.set_only_db(None)


# --------------------------------------------------------------------------- #
# regressions found while preparing the 1.2.0 release
# --------------------------------------------------------------------------- #

def _gz(data):
    return gzip.compress(data)


@case
def test_export_keeps_common_words_and_blanks_real_names(c):
    msg = (b"2026-09-14 13:00:00,000 7 WARNING acme-prod odoo.http: request from 2001:db8::7 hit acme-prod "
           b"(see acme-prod.example.com, not acme-production)\n")
    upload(c, "odoo.log", msg + LINE.replace(b" mydb ", b" acme-prod "))
    analyze(c)
    text = str(c.get("/api/export").get_json())
    assert "odoo.http" in text, "a file named odoo.log must not blank out every 'odoo'"
    assert "acme-prod " not in text and "acme-prod)" not in text and "2001:db8" not in text, text[:1500]
    assert "acme-production" in text, "only whole names are blanked"
    assert A._secret_names(set(), [{"name": "acme-prod.log.gz"}, {"name": "odoo.log"}, {"name": "db.log"}]) == ["acme-prod"]
    assert A._scrub("at 13:05:09 from fe80::1%eth0", []) == "at 13:05:09 from [ip]"


@case
def test_parse_key_covers_the_tail_of_files_between_1_and_2_mib(c):
    import tempfile
    d = tempfile.mkdtemp()
    head = LINE * ((1 << 20) // len(LINE) + 1)
    keys = []
    for tail in (b"/web/x", b"/web/y"):
        path = os.path.join(d, tail.decode()[-1] + ".log")
        with open(path, "wb") as fh:
            fh.write(head + LINE.replace(b"/web/x", tail) * 2000)
        keys.append(A._parse_key(path))
    was = A.PARSE_CACHE
    assert keys[0] != keys[1] or not was, "two different 1.x MiB logs must not share a parse-cache entry"


@case
def test_parse_cache_refuses_a_directory_others_can_write(c):
    import tempfile
    old, d = tempfile.tempdir, tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "logtool-parsed"))
    os.chmod(os.path.join(d, "logtool-parsed"), 0o777)
    tempfile.tempdir = d
    try:
        assert A._parsed_dir() is None and A._cache_load("0" * 40) is None
        os.chmod(os.path.join(d, "logtool-parsed"), 0o700)
        assert A._parsed_dir() == os.path.join(d, "logtool-parsed")
    finally:
        tempfile.tempdir = old


@case
def test_json_bodies_that_are_not_objects_are_a_400(c):
    for url in ("/api/only_db", "/api/domain"):
        for body in ("[1]", '"x"', "5", "null"):
            r = c.post(url, data=body, content_type="application/json")
            assert r.status_code == 400, (url, body, r.status_code)


@case
def test_unloading_a_log_while_it_decompresses_leaves_no_copy_behind(c):
    fid = upload(c, "a.log.gz", _gz(LINE * 100)).get_json()[0]["id"]
    real = A._decompress_once

    def unload_meanwhile(path, reserved=0):
        out = real(path, reserved)
        A.app.test_client().delete("/api/files/" + fid)
        return out
    A._decompress_once = unload_meanwhile
    try:
        c.get("/api/analyze?init=1")
    finally:
        A._decompress_once = real
    left = [n for n in os.listdir(A.UPLOAD_DIR) if n != A.OWNER_FILE]
    assert not A.FILES and not left, left


@case
def test_a_summary_with_a_bad_file_count_is_not_a_summary(c):
    import json
    bad = dict({k: [] for k in A.SUMMARY_KEYS}, totals={}, stats={}, groups={}, files_n="x")
    upload(c, "s.json", json.dumps(bad).encode())
    st, j = analyze(c)
    assert st == 400 and A.FILES[A.ORDER[-1]]["problem"] == "not a logtool export", (st, j)


@case
def test_ipv6_loopback_host_without_a_port_is_allowed(c):
    A.app.config["LOOPBACK_ONLY"] = True
    try:
        assert c.get("/api/files", headers={"Host": "[::1]"}).status_code == 200
        assert c.get("/api/files", headers={"Host": "[::1]:5000"}).status_code == 200
        assert c.get("/api/files", headers={"Host": "[2001:db8::1]:5000"}).status_code == 403
    finally:
        A.app.config["LOOPBACK_ONLY"] = False


@case
def test_reset_reads_every_database_again(c):
    try:
        c.post("/api/only_db", json={"db": "mydb"})
        c.post("/api/reset")
        assert c.get("/api/only_db").get_json() == {"db": None}
    finally:
        A.set_only_db(None)


@case
def test_rows_are_sent_again_after_the_database_to_read_changes(c):
    try:
        upload(c, "a.log", LINE + LINE.replace(b" mydb ", b" other "))
        j = analyze(c)[1]
        key = j["view"]["rows_key"]
        c.post("/api/only_db", json={"db": "other"})
        st, j = analyze(c, "&rows=" + key + "&hist=" + key)
        assert st == 200 and j["view"]["rows"] is not None and j["hist"]["ops"] is not None
    finally:
        A.set_only_db(None)


@case
def test_colored_logs_are_scanned_as_they_are_parsed(c):
    color = ("2026-09-14 13:00:00,000 7 \x1b[1;33m\x1b[1;49mWARNING\x1b[0m db odoo.models: odd thing\n"
             "2026-09-14 13:00:01,000 7 \x1b[1;32m\x1b[1;49mINFO\x1b[0m other odoo.models: elsewhere\n")
    path, p = _profile(color)
    assert p.parts["db"].issues, "the parser counts the colored warning"
    got = olp.scan_issue_occurrences(path, "WARNING", "odoo.models", "odd thing", set(), None, None, 10)
    assert [o["line"] for o in got] == [1], got
    with olp._numbered_lines(path) as numbered:
        assert [i for i, _ in olp._only_db_lines(numbered, "other")] == [2]


@case
def test_an_impossible_timestamp_is_never_a_header(c):
    assert olp._parse_header_g("2026-02-31 13:00:00,000 7 INFO db x.y: not a real date\n") is None
    assert olp._parse_header("2026-09-14 25:00:00,000 7 INFO db x.y: not a real hour\n") is None
    assert olp._parse_header("2026-09-14 13:00:00,000 7 INFO db x.y: fine\n")[3] == "db"


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
def test_job_details_and_bare_numbers_make_valid_domains(c):
    text = olp.dom_render([], [], None, [("operation", "automation: Notify (on_write)",
                                          {"rule id": {"12"}, "records processed": {"3/3"}, "modules": {"7"}}),
                                         ("issue", ("WARNING", "x.y", "# records could not be processed"), {"#": {"3"}})],
                          None)
    assert olp.dom_tree(olp.dom_parse_text(text)) is not None
    d = {}
    olp._num_facts(d, "3 records could not be processed")
    assert olp.dom_is_field(next(iter(d)))


@case
def test_numeric_fields_need_numbers(c):
    for bad in (["duration", ">", "x"], ["http_status", ">=", "five hundred"]):
        try:
            olp.dom_tree([bad])
        except olp.DomainError:
            continue
        raise AssertionError("accepted %r" % (bad,))
    assert olp.dom_tree([["duration", ">", "2"]]) is not None


@case
def test_exception_classes_are_counted_per_database_and_per_entry(c):
    log = ("2026-09-14 13:00:00,000 7 ERROR db odoo.sql_db: bad query\n"
           "2026-09-14 13:00:01,000 7 WARNING db py.warnings: something\n"
           "UserWarning: unrelated\n"
           "2026-09-14 13:00:02,000 7 ERROR db odoo.http: boom\n"
           "Traceback (most recent call last):\n"
           "ValueError: x\n")
    a = A._args()
    a.events = True
    p = olp.Profile(a)
    p.feed(io.StringIO(log))
    assert dict(p.parts["db"].exceptions) == {"ValueError": 1}, dict(p.parts["db"].exceptions)


@case
def test_report_cli_time_window_filters_and_stdin(c):
    import contextlib
    import tempfile
    d = tempfile.mkdtemp()
    path, out = os.path.join(d, "one.log"), os.path.join(d, "out.txt")
    with open(path, "wb") as fh:
        fh.write(LINE)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert olp.main([path, "--since", "2026-09-14 13:00", "--until", "2026-09-14 13:05", "-o", out]) == 0
        assert olp.main([path, "--until", "2026-09-14", "-o", out]) == 0          # a date is the whole day
        assert olp.main([path, "--only-db", "nosuch", "-o", out]) == 2
        assert "matched" in err.getvalue(), err.getvalue()
        old = sys.stdin
        sys.stdin = io.StringIO(LINE.decode())
        try:
            assert olp.main(["-", "-", "-o", out]) == 0                          # stdin twice is read once
        finally:
            sys.stdin = old
        try:
            olp.main([path, "--since", "yesterday"])
        except SystemExit as e:
            assert e.code == 2
        else:
            raise AssertionError("--since yesterday was accepted")


@case
def test_stopping_the_server_with_sigterm_stops_its_workers(c):
    import signal
    import subprocess
    import time

    def children(pid):
        out = []
        for d in os.listdir("/proc"):
            try:
                if d.isdigit() and int(open("/proc/%s/stat" % d).read().rsplit(")", 1)[1].split()[1]) == pid:
                    out += [int(d)] + children(int(d))
            except (OSError, IndexError, ValueError):
                pass
        return out
    p = subprocess.Popen([sys.executable, "-m", "logtool", "--port", "0", "--no-browser"], cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 20
        while len(children(p.pid)) < 3 and time.time() < deadline:      # the worker pool starts with the server
            time.sleep(0.2)
        kids = children(p.pid)
        p.send_signal(signal.SIGTERM)
        assert p.wait(20) == 0
        time.sleep(1)
        left = [k for k in kids if os.path.exists("/proc/%d" % k)
                and open("/proc/%d/stat" % k).read().rsplit(")", 1)[1].split()[0] != "Z"]
        assert kids and not left, (len(kids), left)
    finally:
        if p.poll() is None:
            p.kill()

# pytest runs the same cases: `pytest tests/` (each case gets a freshly reset app as `c`)
try:
    import pytest
except ImportError:                                       # the plain runner below needs no pytest
    pytest = None
if pytest is not None:
    @pytest.fixture
    def c():
        yield fresh()
        fresh()


def main(argv):
    only = argv[1:]
    failed = 0
    ran = 0
    for fn in CASES:
        if only and not any(o in fn.__name__ for o in only):
            continue
        ran += 1
        try:
            fn(fresh())
            print("ok    " + fn.__name__)
        except Exception as e:                            # noqa: BLE001 - report every failure, keep going
            failed += 1
            print("FAIL  %s: %s: %s" % (fn.__name__, type(e).__name__, e))
    fresh()
    print("\n%d tests, %d failed" % (ran, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
