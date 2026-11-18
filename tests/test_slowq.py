"""PostgreSQL slow-query logs: their own parser, table, form, chart items, "logged during" and exports.
"""

from __future__ import annotations

import os

from support import FF_LOG, OP, SAMPLE, _post_domain, analyze, case, upload

from logtool import web
from logtool import profiler as olp


SQ_RULE = b"-" * 80 + b"\n"
SQ = (SQ_RULE + b"2026-09-14 13:05:20.500 UTC mydb: approximate duration: 15 seconds\n\n"
      b"SELECT id FROM res_partner WHERE name ilike '%o''brien%'  -- a 'comment'\n  AND id IN (1, 2, 3)\n\n" +
      SQ_RULE + b"2026-09-14 13:20:00.000 UTC mydb: approximate duration: 41 seconds\n\n"
      b"SELECT id FROM res_partner WHERE name ilike '%acme%'\n  AND id IN (7)\n\n" +
      SQ_RULE + b"2026-09-14 13:21:00.000 UTC other_db: approximate duration: 3 seconds\n\n"
      b"UPDATE stock_quant SET quantity = 5 WHERE id = 9\n")


def _sq_file(data=SQ, name="pg.log"):
    path = os.path.join(web.storage._upload_dir(), "sq-" + name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


@case
def test_slow_query_parser_groups_runs_by_shape(c):
    qs, shapes = olp.sq_parse(_sq_file())
    assert [(q.db, q.seconds, q.cut) for q in qs] == [("mydb", 15.0, False), ("mydb", 41.0, False), ("other_db", 3.0, False)]
    assert qs[0].end == olp.stamp_minute("2026-09-14", "13:05") * 60 + 20.5 and "\n  AND id IN (1, 2, 3)" in qs[0].sql
    assert qs[0].shape == qs[1].shape != qs[2].shape, qs                        # other values, same query
    assert shapes[qs[0].shape] == "SELECT id FROM res_partner WHERE name ilike ? AND id IN (?)", shapes
    assert "brien" not in olp.sq_censor(qs[0].sql) and "comment" not in olp.sq_censor(qs[0].sql)
    assert [q.db for q in olp.sq_parse(_sq_file(), only_db="other_db")[0]] == ["other_db"]
    big = olp.sq_parse(_sq_file(SQ_RULE + b"2026-09-14 13:00:00 UTC : approximate duration: 2 seconds\n\nSELECT " +
                                b"x, " * 60000 + b"y\n"))[0]
    assert big[0].cut and len(big[0].sql) <= olp.SQ_SQL_MAX + 1 and big[0].db is None, (big[0].cut, len(big[0].sql))
    assert olp.sq_sniff(_sq_file(name="anything.log")) and not olp.sq_sniff(SAMPLE)    # by content, or by name
    assert olp.sq_sniff(SAMPLE, "pg_slow_queries.log.gz")
    odd = olp.sq_parse(_sq_file(SQ_RULE + b"2026-02-31 10:00:00 UTC db: approximate duration: 3 seconds\n\nSELECT 1\n" +
                                SQ_RULE + b"2026-02-10 25:99:00 UTC db: approximate duration: 3 seconds\n\nSELECT 2\n" + SQ))[0]
    assert len(odd) == 3 and "SELECT 1" not in odd[0].sql and "SELECT 2" not in odd[0].sql, odd   # impossible stamps: skipped


@case
def test_slow_queries_alone_and_beside_a_log(c):
    up = upload(c, "pg_slow_queries.log", SQ).get_json()
    s, j = analyze(c)
    assert j["files"][0]["kind"] == "slowq" and not j["files"][0]["problem"], j["files"]       # known once parsed
    assert s == 200 and j["databases"] == [["mydb", 2], ["other_db", 1]] and j["excluded_db"] == ["other_db"], j
    sq = j["slowq"]
    assert sq["n"] == 2 and sq["max_s"] == 41 and sq["shapes"] == 1 and len(sq["rows"]) == 1, sq     # one row per shape
    r = sq["rows"][0]
    assert (r["n"], r["total_s"], r["avg_s"], r["max_s"], r["dbs"]) == (2, 56, 28, 41, ["mydb"]), r
    assert [s_ for _, s_ in r["runs"]] == [15, 41] and r["head"] == "SELECT id FROM res_partner WHERE name ilike ? AND id IN (?)", r
    assert sq["labels"][r["shape"]] == r["head"]
    assert j["window"] and j["view"]["stats"]["window"][0] == "2026-09-14T13:05:20", j["view"]["stats"]   # their own span
    s, j = analyze(c, "&from=%d&to=%d" % (j["window"][0], j["window"][0] + 600))
    assert s == 200 and j["slowq"]["n"] == 1, j.get("error")                   # logged in the interval
    upload(c, "odoo.log", FF_LOG)
    j = analyze(c)[1]
    assert j["slowq"]["n"] == 2 and j["databases"][0] == ["mydb", 7], j["databases"]
    assert c.get("/api/analyze?exclude_db=mydb&exclude_db=-").get_json()["slowq"]["n"] == 1   # follows the databases box
    assert c.get("/api/analyze?init=1&exclude_file=" + up[0]["id"]).get_json()["slowq"]["n"] == 0


@case
def test_slow_query_form_and_what_ran_meanwhile(c):
    upload(c, "f.log", FF_LOG)
    upload(c, "pg_slow_queries.log", SQ)
    shape = analyze(c)[1]["slowq"]["rows"][0]
    d = c.get("/api/slowq?id=" + shape["id"]).get_json()                     # a shape's row opens its longest run
    assert d["s"] == 41 and d["db"] == "mydb" and d["same"]["n"] == 2 and d["same"]["total_s"] == 56, d
    assert sum(b[2] for b in d["same"]["durations"]["bins"]) == 2 and [x["s"] for x in d["same"]["items"]] == [15, 41], d["same"]
    row = d["same"]["items"][0]
    assert "o''brien" in c.get("/api/slowq?id=" + row["id"]).get_json()["sql"]
    rn = c.get("/api/slowq?running=1&id=" + row["id"]).get_json()          # 13:05:05.5 - 13:05:20.5: the two 13:05:09 calls
    assert rn["n"] == 2 and {o["operation"] for o in rn["items"]} == {OP}, rn
    sd = c.get("/api/op_slowq?by=operation&name=" + OP.replace(" ", "%20")).get_json()
    it = sd["items"][0]                                                          # one item per shape
    assert (sd["n"], sd["runs"], it["id"], it["n"], it["calls"], it["dbs"]) == (1, 1, row["id"], 1, 2, ["mydb"]), sd
    assert it["head"] == "SELECT id FROM res_partner WHERE name ilike ? AND id IN (?)", it
    assert c.get("/api/op_slowq?by=app&name=stock").get_json()["n"] == 1
    assert c.get("/api/slowq?id=nope:1").status_code == 400 and c.get("/api/op_slowq?by=x&name=y").status_code == 400


@case
def test_slow_queries_in_the_domain_and_the_exports(c):
    from urllib.parse import quote
    import json
    upload(c, "f.log", FF_LOG)
    upload(c, "pg_slow_queries.log", SQ)
    analyze(c)

    def n(text):
        st = _post_domain(c, text)[1]["state"]
        q = ("&xd=" + quote(json.dumps(st["xd"])) if st["xd"] else "") + "".join("&ip=" + ip for ip in st["ip"])
        return analyze(c, q)[1]["slowq"]["n"]
    assert n('[["kind", "=", "slow_query"], ["duration", ">", 20]]') == 1
    assert n('[["sql", "ilike", "%brien%"]]') == 1 and n('[["kind", "=", "request"]]') == 0 and n('[["http_status", ">=", 500]]') == 2
    assert n('[["ip", "=", "10.0.0.1"]]') == 0                                  # a slow query has no client
    st = _post_domain(c, '[["chart.item", "=", {"slow_query": "abc", "lines": ["total_ms"]}], ["table.group", "=", "slow_query"]]')[1]
    assert st["state"]["view"]["items"] == [{"slow_query": "abc", "bars": ["requests"], "lines": ["total_ms"]}], st
    assert st["state"]["view"]["table"]["group"] == "slow_query"
    assert "a slow query only has" in _post_domain(c, '[["chart.item", "=", {"slow_query": "abc", "bars": ["avg_ms.py"]}]]')[1]["error"]
    ex = c.get("/api/export?exclude_db=other_db").get_data(as_text=True)
    sq = json.loads(ex)["slow_queries"]
    assert sq["n"] == 2 and sq["by_shape"][0]["n"] == 2 and "res_partner" not in ex and "mydb" not in ex, sq
    txt = c.get("/api/export_text?exclude_db=other_db").get_data(as_text=True)
    assert "SLOW QUERIES (2 runs of 1 shapes," in txt and "res_partner" not in txt.split("SLOW QUERIES")[1].split("RAW LOG LINES")[0]
    view = {"items": [], "total": {"bars": [], "lines": []}, "table": {"group": "slow_query"}, "errors": {}}
    html = c.get("/api/export_html?exclude_db=other_db&view=" + quote(json.dumps(view))).get_data(as_text=True)
    assert "brien" not in html and "acme" not in html and "ilike ?" in html, html[:300]
