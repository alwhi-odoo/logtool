"""The global filter as a domain (FILTERS.md): round trips, the view, conditions on every occurrence.
"""

from __future__ import annotations


from support import FF_LOG, OP, _ff, _post_domain, analyze, case, upload

from logtool import parser
from logtool import profiler as olp


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
                                                              "ip": [], "ff": [], "xd": None}
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
def test_job_details_and_bare_numbers_make_valid_domains(c):
    text = olp.dom_render([], [], None, [("operation", "automation: Notify (on_write)",
                                          {"rule id": {"12"}, "records processed": {"3/3"}, "modules": {"7"}}),
                                         ("issue", ("WARNING", "x.y", "# records could not be processed"), {"#": {"3"}})],
                          None)
    assert olp.dom_tree(olp.dom_parse_text(text)) is not None
    d = {}
    parser.facts._num_facts(d, "3 records could not be processed")
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
