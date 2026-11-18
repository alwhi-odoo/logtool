"""The HTTP API: bad input is answered, not crashed; foreign origins are refused; caches are freed.
"""

from __future__ import annotations

import threading

from support import FF_LOG, OP, analyze, case, sample, upload

from logtool import web


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
    web.app.config["LOOPBACK_ONLY"] = True
    try:
        assert c.get("/api/files", headers={"Host": "evil.example"}).status_code == 403
        assert c.get("/api/files", headers={"Host": "localhost:5000"}).status_code == 200
    finally:
        web.app.config["LOOPBACK_ONLY"] = False


@case
def test_unloading_frees_every_cache(c):
    for i in range(4):
        upload(c, "a%d.log" % i, sample()[:150000])
    analyze(c)
    for f in c.get("/api/files").get_json():
        c.delete("/api/files/" + f["id"])
    caches = {n: len(getattr(web.state, n)) for n in ("CHUNKS", "INDEX", "SLICES", "BASE", "FULL", "CUTS")}
    assert not any(caches.values()), caches


@case
def test_concurrent_analyses_agree(c):
    upload(c, "a.log", sample())
    w = analyze(c)[1]["window"]
    got = {}

    def hit(i):
        lo = w[0] + (i % 4) * 600
        r = web.app.test_client().get("/api/analyze?from=%d&to=%d" % (lo, lo + 3600))
        got[i] = (lo, r.status_code, r.get_json()["view"]["stats"]["requests"])

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert all(s == 200 for _, s, _ in got.values()), got
    by_start = {}
    for lo, _, n in got.values():
        assert by_start.setdefault(lo, n) == n, got               # same interval, same answer


@case
def test_a_pinned_warning_outside_the_interval_keeps_the_operations(c):
    from urllib.parse import quote
    upload(c, "f.log", FF_LOG)
    w = analyze(c)[1]["window"]
    pins = "&pins=" + quote(__import__("json").dumps({"ops": [OP], "issues": [["WARNING", "odoo.models", "Order # is odd"]]}))
    j = analyze(c, pins + "&from=%d&to=%d" % (w[0], w[0] + 600))[1]          # the warning is logged later
    assert [r["operation"] for r in j["view"]["rows"]] == [OP], j.get("error") or j["view"]["rows"]


@case
def test_focused_timeline_has_minute_buckets(c):
    upload(c, "f.log", FF_LOG)
    w = analyze(c)[1]["window"]
    t = c.get("/api/timeline?from=%d&to=%d&issues=%s" % (w[0], w[0] + 1800, "[[%22WARNING%22,%22odoo.models%22,%22Order%20%23%20is%20odd%22]]")).get_json()
    assert t["base"] == 1 and sum(e[1] for e in t["all"]) == 3, t
    assert len(t["issues"]) == 1 and sum(e[1] for e in t["issues"][0]["hist"]) == 2, t
    assert c.get("/api/timeline?issues=zz").status_code == 400


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
def test_json_bodies_that_are_not_objects_are_a_400(c):
    for url in ("/api/only_db", "/api/domain"):
        for body in ("[1]", '"x"', "5", "null"):
            r = c.post(url, data=body, content_type="application/json")
            assert r.status_code == 400, (url, body, r.status_code)


@case
def test_ipv6_loopback_host_without_a_port_is_allowed(c):
    web.app.config["LOOPBACK_ONLY"] = True
    try:
        assert c.get("/api/files", headers={"Host": "[::1]"}).status_code == 200
        assert c.get("/api/files", headers={"Host": "[::1]:5000"}).status_code == 200
        assert c.get("/api/files", headers={"Host": "[2001:db8::1]:5000"}).status_code == 403
    finally:
        web.app.config["LOOPBACK_ONLY"] = False
