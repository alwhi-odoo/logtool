"""The exports: summary (JSON), text and standalone HTML, and what they must not give away.
"""

from __future__ import annotations


from support import (
    AUTH, CONFIRM, CRON, DENIED, IMG, LINE, LOGGED_LOG, LOGIN, OP, RPC, _ff, analyze, case, facts, upload,
)

from logtool import web


@case
def test_export_pseudonymizes_and_explains(c):
    many = b"".join(b"2026-09-14 13:09:%02d,000 7 WARNING mydb odoo.x: distinct message %s\n" % (i, b"x" * (i + 1))
                    for i in range(59)) + b"2026-09-14 13:09:59,000 7 WARNING mydb odoo.y: another one\n" * 3
    upload(c, "f.log", LOGIN + AUTH + DENIED + IMG % (b"7", b"0.1") + CRON + many + b"2026-09-14 13:10:00,000 7 ERROR mydb odoo.z: rare\n")
    analyze(c)
    e = c.get("/api/export").get_json()
    text = str(e)
    assert "10.0.0." not in text and "jane@" not in text and "BK1AC94d" not in text and "mydb" not in text, text[:2000]
    assert e["issues_distinct"] == len(e["issues"]) > web.state.MAX_ISSUES, (e["issues_distinct"], len(e["issues"]))
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


@case
def test_export_html_shows_only_the_selected_database_and_censors_lines_without_one(c):
    import json
    import re
    from urllib.parse import quote
    other = b"2026-09-14 13:09:00,000 8 WARNING othercustomer odoo.x: from 10.9.9.9 at othercustomer\n"
    nodb = (b"2026-09-14 13:09:30,000 9 WARNING ? odoo.sql_db: Connection to the database othercustomer failed for bob@corp.com\n"
            b'2026-09-14 13:09:40,000 9 INFO ? werkzeug: 10.8.8.8 - - [14/Sep/2026 13:09:40] '
            b'"GET /web/database/selector HTTP/1.1" 200 - 2 0.01 0.02\n')
    upload(c, "acme-logs.log", LOGIN + AUTH + DENIED + IMG % (b"7", b"0.1") + CRON + other + nodb)
    analyze(c)
    view = {"items": [{"issue": ["WARNING", "odoo.sql_db", "Connection to the database othercustomer failed for bob@corp.com"]}]}
    r = c.get("/api/export_html?exclude_db=othercustomer&desc=x&view=" + quote(json.dumps(view)))
    assert r.status_code == 200 and r.mimetype == "text/html" and ".html" in r.headers["Content-Disposition"], r.get_data()[:300]
    page = r.get_data(as_text=True)
    for secret in ("othercustomer", "10.9.9.9", "10.8.8.8", "bob@corp.com", 'src="/static'):
        assert secret not in page, secret
    snap = json.loads(re.search(r'<script type="application/json" id="snapshot">(.*?)</script>', page, re.S).group(1))
    a = snap["analyze"]
    assert {d[0] for d in a["databases"]} == {"mydb", "-"} and a["excluded_db"] == [] and not a["files"], a["databases"]
    assert snap["view"]["items"][0]["issue"][2] in {i["message"] for i in a["view"]["issues"]}, snap["view"]
    issue = next(d for k, d in snap["details"].items() if "top-secret" in k)    # the selected database: as it is
    f = facts(issue["facts"])
    assert "69" in f["user id"] and "10.0.0.7" in page and "jane@example.com" in page, f
    img = snap["details"]["operation\x1fimage res.partner.avatar_128"]
    assert "/web/image/res.partner/7/avatar_128" in facts(img["facts"])["URL"] and img["examples"], img
    sel = next(d for k, d in snap["details"].items() if "selector" in k)        # a line without a database: censored
    assert [ip[:3] for ip in facts(sel["facts"])["client IP"]] == ["ip-"] and not sel["examples"], sel
    assert snap["focus"] is None and snap["view"]["focus"] is None, snap["focus"]
    w = a["window"]                                                            # an interval: the histogram shows just it
    r = c.get("/api/export_html?exclude_db=othercustomer&from=%d&to=%d&view=%s" % (w[0] + 60, w[0] + 180, quote(json.dumps({"items": []}))))
    snap = json.loads(re.search(r'id="snapshot">(.*?)</script>', r.get_data(as_text=True), re.S).group(1))
    f = snap["focus"]
    assert snap["view"]["focus"] == [f["from"], f["to"]] == [w[0] + 60, w[0] + 180] and f["base"] == 1, f
    assert all(f["from"] // 60 <= m < -(-f["to"] // 60) for m, *_ in f["all"]), f["all"]      # (its last minute: whole)
    was, web.export_html.HTML_OP_DETAILS = web.export_html.HTML_OP_DETAILS, 0                               # past the top rows: every job still has its details
    try:
        r = c.get("/api/export_html?exclude_db=othercustomer&view=" + quote(json.dumps({"items": []})))
    finally:
        web.export_html.HTML_OP_DETAILS = was
    det = json.loads(re.search(r'id="snapshot">(.*?)</script>', r.get_data(as_text=True), re.S).group(1))["details"]
    assert "operation\x1fcron: Credit Check" in det and "operation\x1fimage res.partner.avatar_128" not in det, list(det)
    assert c.get("/api/export_html?view=" + quote("[]")).status_code == 400


@case
def test_export_text_holds_one_database_and_every_occurrence_in_time_order(c):
    import re
    other = (b'2026-09-14 13:05:30,000 77 INFO otherdb werkzeug: 10.9.9.9 - - [14/Sep/2026 13:05:30] '
             b'"GET /web HTTP/1.1" 200 - 1 0.01 0.01\n')
    nodb = b"2026-09-14 13:05:40,000 1 INFO ? odoo.service.server: HTTP service running\n"
    upload(c, "a.log", RPC % b"0.2" + other + nodb + RPC.replace(b"10.0.0.1", b"10.0.0.2").replace(b"13:05:09", b"13:06:09") % b"0.3")
    upload(c, "b.log", CRON + RPC.replace(b"10.0.0.1", b"10.0.0.3").replace(b"13:05:09", b"13:04:00") % b"0.1")
    analyze(c)
    r = c.get("/api/export_text")                                   # two databases: refused
    assert r.status_code == 400 and "select only one" in r.get_json()["error"], r.get_json()
    r = c.get("/api/export_text?exclude_db=otherdb")
    text = r.get_data(as_text=True)
    assert r.status_code == 200 and r.mimetype == "text/plain" and ".txt" in r.headers["Content-Disposition"]
    assert "otherdb" not in text and "10.9.9.9" not in text, text  # the other database's lines: not even as context
    hist, table, raw = text.split("=" * 100)
    assert hist.startswith("REQUESTS OVER TIME") and "#" in hist and table.strip().startswith("OPERATIONS (2,")
    assert re.search(r"\n%s +3 +300\n" % re.escape(OP), table), table       # name, calls, avg ms
    assert "client IP" not in table and "10.0.0.2" not in table and "cron id" in table, table   # its details, but its clients
    heads = re.findall(r"\n\[\d+\] (\S+ \S+)  (.+?)  \(", raw)
    assert [h[0][11:] for h in heads] == ["13:04:00", "13:05:09", "13:06:09", "13:08:00"], heads   # both files, oldest first
    assert raw.count("\n> ") == 4 and "RAW LOG LINES (4 occurrences" in raw
    first = raw.split("\n\n\n")[2]                                     # 13:05:09: nothing above it, its file's next line below
    assert [l[:1] for l in first.splitlines()[1:]] == [">", " "], first
    assert " ? " not in raw and "HTTP service running" not in raw, raw     # no line without a database
    one = c.get("/api/export_text?exclude_db=otherdb&ip=10.0.0.2").get_data(as_text=True)   # the filters scope it
    assert "RAW LOG LINES (1 occurrence," in one and re.findall(r"\n> .*?(10\.0\.0\.\d)", one) == ["10.0.0.2"], one
    assert "OPERATIONS (1," in one


@case
def test_export_html_carries_no_filter_values(c):
    import json
    import re
    from urllib.parse import quote
    upload(c, "l.log", LOGGED_LOG)
    analyze(c)
    ff = _ff(("operation", CONFIRM, {"URL": ["SECRET_FF"]}), ("issue", ["WARNING", "odoo.models", "Access denied (id=#)"], {"user id": ["SECRET_FF2"]}))
    view = {"items": [], "table": {"group": "operation", "search": "SECRET_TS"}, "errors": {"level": "all", "search": "SECRET_ES"}}
    q = ("/api/export_html?ip=10.0.0.2&xd=" + quote(json.dumps([["URL", "not like", "SECRET_XD"]])) + ff +
         "&desc=SECRET_DESC&view=" + quote(json.dumps(view)))
    r = c.get(q)
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "SECRET" not in page, re.findall(r"SECRET_\w+", page)
    snap = json.loads(re.search(r'id="snapshot">(.*?)</script>', page, re.S).group(1))
    assert snap["filter"] == "2 item filters · 1 custom condition · only 1 client IP", snap["filter"]
    assert snap["view"]["table"] == {"group": "operation", "search": ""} and snap["view"]["errors"]["search"] == ""


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
    assert web.scrub._secret_names(set(), [{"name": "acme-prod.log.gz"}, {"name": "odoo.log"}, {"name": "db.log"}]) == ["acme-prod"]
    assert web.scrub._scrub("at 13:05:09 from fe80::1%eth0", []) == "at 13:05:09 from [ip]"
