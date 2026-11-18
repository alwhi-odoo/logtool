"""Chunks: a time interval read from cached chunks equals a re-parse of that window; scanners count like the parser.
"""

from __future__ import annotations

import os
import random

from support import SAMPLE, _profile, analyze, case

from logtool import parser, web
from logtool import profiler as olp
from logtool.parser import parallel as PAR


@case
def test_interval_from_chunks_matches_a_straight_parse(c):
    saved = PAR.PAR_MIN_TOTAL, PAR.PAR_MIN_CHUNK, PAR.PAR_MAX_CHUNK, PAR._cpus
    PAR.PAR_MIN_TOTAL, PAR.PAR_MIN_CHUNK, PAR.PAR_MAX_CHUNK, PAR._cpus = 1 << 16, 1 << 15, 1 << 17, lambda: 4   # force several chunks
    try:
        src = SAMPLE
        rec = web.loading._add_file(src, "sample.log", False)
        fid = rec["id"]
        window = analyze(c)[1]["window"]
        assert len(web.state.CHUNKS[fid]) > 3, len(web.state.CHUNKS[fid])
        rnd = random.Random(7)

        def rows(prof):
            return {r["operation"]: (r["calls"], r["total_s"], r["sql_s"], r["python_s"], r["queries"], r["max_s"])
                    for r in web.results._op_rows(prof)}

        for _ in range(25):
            lo = rnd.randrange(window[0], window[1] - 1)
            hi = min(window[1] - 1, lo + rnd.choice([1, 45, 600, 3600, 20000, 10 ** 6]))
            if hi <= lo:
                continue
            got = web.analysis.analyze(set(), set(), (lo, hi), False)
            a = web.state._args()
            a.since, a.until = web.util._iso(lo), web.util._iso(hi - 1)
            want = web.selection._merge([olp.profile_files(a, [src], jobs=1).parts], frozenset())
            have = web.state.CUT_BASE[((fid,), frozenset(), lo, hi, None)]
            assert rows(have) == rows(want), (lo, hi)
            assert got["view"]["stats"]["requests"] == web.results._summary(want, [])["stats"]["requests"], (lo, hi)
    finally:
        PAR.PAR_MIN_TOTAL, PAR.PAR_MIN_CHUNK, PAR.PAR_MAX_CHUNK, PAR._cpus = saved
        olp.shutdown_pool()


@case
def test_filtered_interval_from_chunks_matches_a_straight_parse(c):
    saved = PAR.PAR_MIN_TOTAL, PAR.PAR_MIN_CHUNK, PAR.PAR_MAX_CHUNK, PAR._cpus
    PAR.PAR_MIN_TOTAL, PAR.PAR_MIN_CHUNK, PAR.PAR_MAX_CHUNK, PAR._cpus = 1 << 16, 1 << 15, 1 << 17, lambda: 4
    try:
        rec = web.loading._add_file(SAMPLE, "sample.log", False)
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
            have = web.selection._cut([rec], (fid,), frozenset(), lo, hi, flt)
            a = web.state._args(ff)
            a.since, a.until = web.util._iso(lo), web.util._iso(hi - 1)
            want = web.selection._merge([olp.profile_files(a, [SAMPLE], jobs=1).parts], frozenset())
            got = {r["operation"]: r["calls"] for r in web.results._op_rows(have)}
            assert got == {r["operation"]: r["calls"] for r in web.results._op_rows(want)}, (lo, hi)
    finally:
        PAR.PAR_MIN_TOTAL, PAR.PAR_MIN_CHUNK, PAR.PAR_MAX_CHUNK, PAR._cpus = saved
        olp.shutdown_pool()


@case
def test_parallel_raw_line_scans_match_a_whole_file_scan(c):
    saved = PAR.PAR_MIN_TOTAL, PAR.PAR_MIN_CHUNK, PAR.PAR_MAX_CHUNK, PAR._cpus
    PAR.PAR_MIN_TOTAL, PAR.PAR_MIN_CHUNK, PAR.PAR_MAX_CHUNK, PAR._cpus = 1 << 16, 1 << 15, 1 << 17, lambda: 4
    try:
        rec = web.loading._add_file(SAMPLE, "sample.log", False)
        fid = rec["id"]
        j = analyze(c)[1]
        assert len(web.state.CHUNKS[fid]) > 3
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
        PAR.PAR_MIN_TOTAL, PAR.PAR_MIN_CHUNK, PAR.PAR_MAX_CHUNK, PAR._cpus = saved
        olp.shutdown_pool()


@case
def test_merging_does_not_change_the_cached_pieces(c):
    w = "2026-09-14 13:00:01,000 7 WARNING db odoo.models: odd thing\n"
    _, a = _profile(w)
    _, b = _profile(w)
    for _ in range(3):
        m = web.selection._merge([a.parts, b.parts], frozenset())
    key = ("WARNING", "odoo.models", "odd thing")
    assert list(a.parts["db"].issues[key][6].values()) == [[1, 0.0, 0.0, 0.0]]
    assert sum(v[0] for v in m.issues[key][6].values()) == m.issues[key][0] == 2


@case
def test_chunks_are_cut_at_log_entries(c):
    import tempfile
    entry = ("2026-09-14 13:%02d:01,000 7 ERROR db odoo.models: odd thing\nTraceback (most recent call last):\n" +
             "".join('  File "x.py", line %d, in f\n' % i for i in range(40)) + "ValueError: boom\n" +
             "2026-09-14 13:%02d:02,000 7 INFO db werkzeug: 1.1.1.1 - - [x] \"GET /web/x HTTP/1.1\" 200 - 1 0.1 0.1\n")
    path = os.path.join(tempfile.mkdtemp(), "c.log")
    with open(path, "w") as fh:
        fh.write("".join(entry % (m, m) for m in range(60)))
    saved = PAR.PAR_MIN_TOTAL, PAR.PAR_MIN_CHUNK, PAR.PAR_MAX_CHUNK, PAR._cpus
    PAR.PAR_MIN_TOTAL, PAR.PAR_MIN_CHUNK, PAR.PAR_MAX_CHUNK, PAR._cpus = 1 << 10, 1 << 12, 1 << 13, lambda: 4
    try:
        chunks = {}
        olp.profile_files(web.state._args(), [path], chunks=chunks)
        parts = chunks[path]
        assert len(parts) > 5
        m = web.selection._merge([ch.parts for ch in parts], frozenset())
        (key, e), = m.issues.items()
        assert e[0] == 60 and e[4] == "ValueError" and sorted(p for ch in parts for p in ch.parts) == ["db"] * len(parts), (e, [list(ch.parts) for ch in parts])
    finally:
        PAR.PAR_MIN_TOTAL, PAR.PAR_MIN_CHUNK, PAR.PAR_MAX_CHUNK, PAR._cpus = saved
        olp.shutdown_pool()


@case
def test_the_scanners_count_like_the_parser(c):
    text = ("2026-09-14 13:00:01,000 7 WARNING ? odoo.models: no database here\n"
            "2026-09-14 13:00:02,000 7 INFO db werkzeug: 1.1.1.1 - - [x] \"GET /websocket HTTP/1.1\" 101 - 1 0.01 300.0\n"
            "2026-09-14 13:01:03,000 8 INFO db odoo.addons.base_automation.models.base_automation: Starting time-based automation rule `N`.\n"
            "2026-09-14 13:01:08,000 8 INFO db odoo.addons.base_automation.models.base_automation: Time-based automation rule `N` done.\n")
    path, _p = _profile(text)
    assert olp.scan_issue_occurrences(path, "WARNING", "odoo.models", "no database here", {"-"}, None, None, 10) == []
    wsop = parser.scan._line_operation("werkzeug", text.splitlines()[1].split("werkzeug: ")[1])[2]
    ws = olp.scan_op_occurrences(path, "operation", wsop, set(), None, None, 10)
    assert len(ws) == 1 and ws[0]["seconds"] < 1, ws                     # held open, not working
    auto = olp.scan_op_occurrences(path, "operation", "automation: N (time-based)", set(), None, None, 10)
    assert [o["seconds"] for o in auto] == [5.0], auto
    hold = {"extra": [["duration", ">", 10]]}
    assert olp.scan_op_occurrences(path, "operation", wsop, set(), None, None, 10, ff=hold) == []
