"""What every test module shares: the sample log, a freshly reset app, the registry of cases.

A case is a function taking `c`, a Flask test client of a freshly reset app (no network). pytest gives it
through the fixture in conftest.py; `python tests/run.py` needs no pytest.
"""

from __future__ import annotations

import io
import os
import sys

os.environ.setdefault("LOGTOOL_PARSE_CACHE", "0")       # every case parses for real (one case turns the cache on)
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from logtool import web          # noqa: E402
from logtool.parser import parallel as PAR   # noqa: E402


SAMPLE = os.path.join(HERE, "data", "sample-19.0.log")
LINE = (b'2026-09-14 13:05:09,123 4242 INFO mydb werkzeug: 10.0.0.1 - - [14/Sep/2026 13:05:09] '
        b'"GET /web/x HTTP/1.1" 200 - 5 0.1 0.2\n')

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def fresh():
    c = web.app.test_client()
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


RPC = (b'2026-09-14 13:05:09,123 4242 INFO mydb werkzeug: 10.0.0.1 - - [14/Sep/2026 13:05:09] '
       b'"POST /web/dataset/call_kw/stock.valuation.layer/search_read HTTP/1.1" 200 - 5 0.1 %s\n')
OP = "call_kw stock.valuation.layer.search_read"


def detail(c, by, name, query=""):
    r = c.get("/api/detail?by=%s&name=%s%s" % (by, name.replace(" ", "%20"), query))
    return r.status_code, r.get_json() or {}


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


def _ff(*items):
    from urllib.parse import quote
    return "&ff=" + quote(__import__("json").dumps([{"by": b, "name": n, "facts": f} for b, n, f in items]))


FF_LOG = (RPC % b"0.2" + RPC.replace(b"10.0.0.1", b"10.0.0.2") % b"0.3" +
          RPC.replace(b"10.0.0.1", b"10.0.0.3").replace(b"13:05:09", b"13:25:09") % b"0.4" +
          b"2026-09-14 13:30:00,000 4242 WARNING mydb odoo.models: Order 17 is odd\n" +
          b"2026-09-14 13:30:01,000 4242 WARNING mydb odoo.models: Order 18 is odd\nTraceback x\n  sale.order(18,)\n")

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


def _post_domain(c, text):
    r = c.post("/api/domain", json={"domain": text})
    return r.status_code, r.get_json() or {}


def _profile(text, ff=None, start=0, end=None):
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "x.log")
    with open(path, "w") as fh:
        fh.write(text)
    return path, PAR._work(web.state._args(ff), path, start, os.path.getsize(path) if end is None else end)[0]
