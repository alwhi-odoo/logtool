"""`logtool-report --selftest`: built-in checks of the parser against a synthetic log.
"""

from __future__ import annotations

import argparse
import sys

from .facts import F_IP
from .profile import Profile


SELFTEST_LOG = """\
2026-09-13 22:00:09,711 2422355 INFO db-name werkzeug: 10.0.0.1 - - [13/Sep/2026 22:00:09] "POST /web/dataset/call_kw/account.move/web_read HTTP/1.1" 200 - 91 0.412 0.663
2026-09-13 22:00:10,001 2422355 INFO db-name odoo.addons.base.models.res_users: Login successful for login:bot@example.com from n/a
2026-09-13 22:00:10,002 2422355 INFO db-name werkzeug: 10.0.0.9 - - [13/Sep/2026 22:00:10] "POST /xmlrpc/2/common HTTP/1.1" 200 - 7 0.002 0.338
2026-09-13 22:00:11,000 2422355 INFO db-name werkzeug: 10.0.0.9 - - [13/Sep/2026 22:00:11] "POST /xmlrpc/2/object#sale.order.create HTTP/1.1" 200 - 250 1.500 2.500
2026-09-13 22:00:12,000 2422356 INFO odoo.addons.base.models.ir_cron: Starting job `Mail: Email Queue Manager`.
2026-09-13 22:00:13,000 2422356 ERROR db-name odoo.http: Exception during request handling.
Traceback (most recent call last):
  File "x.py", line 1, in y
ValueError: bad thing
2026-09-13 22:00:14,000 2422357 INFO db-name werkzeug: 10.0.0.2 - - [13/Sep/2026 22:00:14] "GET /web/assets/1/web.assets_backend.min.js HTTP/1.1" 200 -
2026-09-13 22:00:15,000 2422357 INFO db-name werkzeug: 10.0.0.3 - - [13/Sep/2026 22:00:15] "GET /en_US/shop/some-product-123 HTTP/1.1" 500 - 12 0.100 0.400
2026-09-13 22:00:16,000 2422357 INFO db-name werkzeug: 10.0.0.4 - - [13/Sep/2026 22:00:16] "GET /.env HTTP/1.1" 404 - 1 0.001 0.002
2026-09-13 22:00:17,000 2422357 WARNING db-name py.warnings: ir.actions.server(2519,):93: DeprecationWarning: read_group is deprecated
2026-09-13 22:00:18,000 2422357 WARNING db-name odoo.api: Too many iterations for recomputing fields!
2026-09-13 22:00:19,000 2422358 INFO db-name odoo.addons.base.models.ir_cron: Job 'Mail: Email Queue Manager' (8) fully done (#loop 1; done 20; remaining 0; duration 0.62s)
2026-09-13 22:00:20,000 2422358 INFO db-name odoo.addons.base_automation.models.base_automation: Automation Rule 'Notify boss' (12) done (on_write; 3/3 records; duration 0.532s)
2026-09-13 22:00:21,000 2422358 INFO db-name odoo.modules.loading: 762 modules loaded in 7.38s, 2400 queries (+300 extra)
2026-09-13 22:01:00,100 2422360 INFO db-name odoo.addons.base.models.ir_cron: Starting job `Old Style`.
2026-09-13 22:01:00,600 2422360 INFO db-name odoo.addons.base.models.ir_cron: Job done: `Old Style` (0.500s).
2026-09-13 22:01:01,000 2422360 INFO db-name odoo.addons.base.models.ir_cron: Job 'Eighteen' (5) starting
2026-09-13 22:01:02,000 2422360 INFO db-name odoo.addons.base.models.ir_cron: Job 'Eighteen' (5) done in 1.250s
2026-09-13 22:01:02,100 2422360 INFO db-name odoo.addons.base.models.ir_cron: Job 'Eighteen' (5) completed
2026-09-13 22:01:03,000 2422361 INFO db-name odoo.addons.base_automation.models.base_automation: Starting time-based automation rule `Nightly`.
2026-09-13 22:01:05,500 2422361 INFO db-name odoo.addons.base_automation.models.base_automation: Time-based automation rule `Nightly` done.
2026-09-13 22:01:06,000 2422362 INFO db-name odoo.http.server: 10.0.0.7 BK1AC94d - [13/Sep/2026 22:01:06] "POST /web/dataset/call_kw/stock.picking/button_validate HTTP/1.1" 200 7454 19 0.032 0.483 rw
2026-09-13 22:01:07,000 2422362 INFO db-name odoo.http.server: 10.0.0.7 BK1AC94d - [13/Sep/2026 22:01:07] "GET /shop/cart HTTP/1.1" 200 stream 3 0.006 0.065 ro->rw
2026-09-13 22:00:22,000 2422359 INFO db-name werkzeug: 10.0.0.5 - - [13/Sep/2026 22:00:22] "POST /web/dataset/call_button/sale.order/action_confirm HTTP/1.1" 200 - 40 0.500 1.500
2026-09-13 22:00:23,000 2422359 INFO db-name werkzeug: 10.0.0.5 - - [13/Sep/2026 22:00:23] "GET /some-typo-page HTTP/1.1" 404 - 3 0.010 0.020
2026-09-13 22:00:24,000 2422359 ERROR db-name odoo.addons.base.models.ir_cron: Job 'Fix things' (9) server action #77 failed
Traceback (most recent call last):
  File "ir.actions.server(77,)", line 3, in <module>
KeyError: 'x'
2026-09-13 22:02:00,500 3000001 WARNING db-name odoo.addons.sale.models.sale_order: Something odd about order 42
2026-09-13 22:02:00,700 3000002 WARNING db-name odoo.addons.sale.models.sale_order: A different worker warned about order 43
2026-09-13 22:02:01,000 3000001 INFO db-name werkzeug: 10.0.0.8 - - [13/Sep/2026 22:02:01] "POST /web/dataset/call_kw/sale.order/action_confirm HTTP/1.1" 200 - 5 0.300 0.400
this line is complete garbage and must not crash the parser
"""


def selftest():
    import io
    checks, fails = [], []

    def ck(name, cond, got=None):
        checks.append(name)
        if not cond:
            fails.append("%s (got %r)" % (name, got))

    a = argparse.Namespace(top=10, by="both", min_calls=1, min_total=0.0, fold=1, events=True,
                           event_top=5, logins=True, hours=True, unclassified=True, all=True,
                           since=None, until=None, db=None, grep=None, csv=None, json=None, out=None)
    p = Profile(a)
    p.feed(io.StringIO(SELFTEST_LOG))
    p.finalize()

    ck("garbage line did not crash and was counted as continuation", p.continuation >= 2, p.continuation)
    ck("no-dbname header matched", p.header_hits.get("odoo-nodb", 0) == 1, dict(p.header_hits))
    ck("timed access lines parsed", p.access_ok == 10, p.access_ok)
    attributed = {op for d in p.issue_ops.values() for op in d}
    ck("a warning is attributed to the request its worker was running",
       attributed == {"call_kw sale.order.action_confirm"}, p.issue_ops)
    ck("a warning from another worker stays unattributed",
       sum(sum(d.values()) for d in p.issue_ops.values()) == 1 and p.levels["WARNING"] >= 4, dict(p.levels))
    ck("untimed access line tolerated", p.access_untimed == 1, p.access_untimed)
    ck("no unparsable access lines", p.access_bad == 0, p.access_bad)
    ck("total time = sql + python + unsplit job time",
       abs(sum(b.total for b in p.fam.values()) - sum(b.sql + b.py + b.unsplit for b in p.fam.values())) < 1e-9)
    ck("app totals equal type totals",
       abs(sum(b.total for b in p.app.values()) - sum(b.total for b in p.fam.values())) < 1e-6)
    ck("family totals equal detail totals",
       abs(sum(b.total for b in p.fam.values()) - sum(b.total for b in p.det.values())) < 1e-6)
    ck("call_kw read classified", "ui.read" in p.fam, sorted(p.fam))
    ck("xmlrpc common -> api.auth", p.fam.get("api.auth") and p.fam["api.auth"].n == 1, sorted(p.fam))
    ck("xmlrpc object create -> api.write", "api.write" in p.fam, sorted(p.fam))
    ck("app comes from the model prefix", "sale" in p.app and "account" in p.app, sorted(p.app))
    ck("button call is a workflow action", p.fam.get("ui.action") and p.fam["ui.action"].n >= 1, sorted(p.fam))
    ck("unknown page that 404s is not-found", "not-found" in p.fam, sorted(p.fam))
    ck("cron run counted with its duration", abs(p.det["cron: Mail: Email Queue Manager"].total - 0.62) < 1e-9)
    ck("cron app comes from the name prefix", "mail" in p.app, sorted(p.app))
    ck("automation rule counted", abs(p.det["automation: Notify boss (on_write)"].total - 0.532) < 1e-9, sorted(p.det))
    ck("module loading counted with its queries", p.det["load modules"].nq == 2400, sorted(p.det))
    ck("17.0 cron format", abs(p.det["cron: Old Style"].total - 0.5) < 1e-9, sorted(p.det))
    ck("18.0 cron format (once, not per line)", p.det["cron: Eighteen"].n == 1 and abs(p.det["cron: Eighteen"].total - 1.25) < 1e-9)
    ck("time-based automation paired start/done", abs(p.det["automation: Nightly (time-based)"].total - 2.5) < 1e-6, sorted(p.det))
    ck("19.4+ access line (odoo.http.server) parsed", p.det["call_kw stock.picking.button_validate"].n == 1, sorted(p.det))
    ck("stream body size and ro->rw flag tolerated", p.access_bad == 0 and p.formats["read-only requests retried read-write"] == 1, dict(p.formats))
    ck("both access-log variants recognised", len([k for k in p.formats if k.startswith("access:")]) == 2, dict(p.formats))
    ck("job time has no SQL/Python split", p.fam["cron"].unsplit > 0 and p.fam["cron"].sql == 0)
    ck("job time is part of the total", abs(sum(b.total for b in p.fam.values()) - sum(b.total for b in p.det.values())) < 1e-6)
    ck("server action tagged on its error",
       any(e[5] == "server action #77" for e in p.issues.values()), [e[5] for e in p.issues.values()])
    ck("language prefix stripped, shop page classified", "website" in p.fam, sorted(p.fam))
    ck("5xx counted on the right family", p.fam["website"].errors == 1, p.fam["website"].errors)
    ck("dotfile probe separated from 'other'", "probe" in p.fam and "other" not in p.fam, sorted(p.fam))
    ck("untimed asset line contributes 0 s", p.fam["assets"].total == 0.0, p.fam["assets"].total)
    ck("login paired to its client ip",
       p.logins.get(("bot@example.com", "10.0.0.9", "successful")) == 1, dict(p.logins))
    ck("exception class taken from traceback", p.exceptions.get("ValueError") == 1, dict(p.exceptions))
    ck("server action id extracted", p.server_actions.get("2519") == 1, dict(p.server_actions))
    ck("cron line grouped", "cron" in p.events, sorted(p.events))
    ck("recompute warning grouped", "orm-recompute" in p.events, sorted(p.events))
    ck("report renders", (lambda: (p.report(io.StringIO()), True)[1])())
    ck("cron id and status kept behind the operation name",
       p.op_facts["cron: Mail: Email Queue Manager"].get("cron id") == {"8": 1}
       and p.op_facts["cron: Mail: Email Queue Manager"]["status"] == {"fully done": 1}, p.op_facts.get("cron: Mail: Email Queue Manager"))
    ck("client IP kept behind the operation name", p.op_facts["xmlrpc sale.order.create"].get(F_IP) == {"10.0.0.9": 1})
    odd = p.issue_facts[("WARNING", "odoo.addons.sale.models.sale_order", "Something odd about order #")]
    ck("the number behind '#' kept under the word before it", odd.get("order #") == {"42": 1}, odd)
    ck("a warning keeps the client of the request it was logged during", odd.get(F_IP) == {"10.0.0.8": 1}, odd)
    ck("every level is counted", p.all_levels["INFO"] > 10 and p.all_levels["ERROR"] == 2, dict(p.all_levels))

    # header parsing edge cases: no-db lines whose message contains a colon, impossible dates and times
    q = Profile(argparse.Namespace(**dict(vars(a), split_db=True)))
    q.feed(io.StringIO("2026-09-14 13:00:00,100 77 INFO odoo.modules.loading: Loading: base\n"
                       "2026-02-31 13:00:00,100 77 INFO db x.y: not a real date\n"
                       "2026-09-14 25:00:00,100 77 INFO db x.y: not a real hour\n"))
    ck("a colon in the message of a no-db line does not turn the logger into a database",
       set(q.parts) == {"-"} and q.header_ok == 0 and q.parts["-"].header_ok == 1, (sorted(q.parts), q.header_ok))
    ck("impossible timestamps are continuation lines, not crashes", q.parts["-"].continuation == 2, q.parts["-"].continuation)

    out = sys.stdout
    for name in checks:
        bad = [f for f in fails if f.startswith(name)]
        out.write("  %s %s\n" % ("FAIL" if bad else "ok  ", bad[0] if bad else name))
    out.write("\n%d checks, %d failed\n" % (len(checks), len(fails)))
    return 1 if fails else 0
