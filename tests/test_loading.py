"""Loading: files that are not what they should be, header parsing, the parse cache, the database to read.
"""

from __future__ import annotations

import bz2
import gzip
import io
import os
import random
import tempfile

from support import LINE, OP, RPC, _profile, analyze, case, fresh, sample, upload

from logtool import parser, web
from logtool import profiler as olp


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
    assert status == 200 and j["view"]["stats"]["operations"] == 20000 and len(j["view"]["rows"]) == web.state.MAX_ROWS


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


@case
def test_another_instance_keeps_a_running_ones_files(c):
    import tempfile
    base = tempfile.mkdtemp()
    live, dead, old = (os.path.join(base, n) for n in ("logtool_live", "logtool_dead", "logtool_old"))
    for d, pid in ((live, os.getppid()), (dead, 2 ** 22 + 12345)):          # the parent: another live process
        os.makedirs(d)
        with open(os.path.join(d, web.storage.OWNER_FILE), "w") as fh:
            fh.write(str(pid))
    os.makedirs(old)
    os.utime(old, (0, 0))
    assert not web.storage._abandoned(live) and web.storage._abandoned(dead) and web.storage._abandoned(old)
    # and a file that disappeared says so, instead of "no occurrence found"
    path = os.path.join(base, "gone.log")
    with open(path, "wb") as fh:
        fh.write(RPC % b"0.2")
    rec = web.loading._add_file(path, "gone.log", False)
    analyze(c)
    os.remove(rec["path"])
    r = c.get("/api/op_lines?by=operation&name=%s" % OP.replace(" ", "%20"))
    assert r.status_code == 400 and "no longer on disk" in r.get_json()["error"], r.get_json()


@case
def test_loading_the_same_logs_again_reads_the_parse_cache(c):
    import tempfile
    d = tempfile.mkdtemp()
    saved = web.parse_cache.PARSE_CACHE, web.parse_cache._parsed_dir, olp.profile_files
    web.parse_cache.PARSE_CACHE, web.parse_cache._parsed_dir = True, lambda: d
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
        web.parse_cache.PARSE_CACHE, web.parse_cache._parsed_dir, olp.profile_files = saved


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
        alone = web.state._args()
        alone.only_db = None
        want = olp.Profile(alone)
        want.feed(io.StringIO(((LINE + nodb) * 50).decode()))
        assert sum(ch.header_ok for c_ in web.state.CHUNKS[web.state.ORDER[-1]] for ch in c_.parts.values()) == \
            sum(p.header_ok for p in want.parts.values())
        r = c.post("/api/only_db", json={"db": ""}).get_json()   # every database again: the loaded log is read again
        assert r == {"db": None, "changed": True}, r
        st, j = analyze(c)
        assert st == 200 and {d for d, _ in j["databases"]} == {"mydb", "otherdb", "-"}, j["databases"]
        assert c.get("/api/only_db").get_json() == {"db": None}
    finally:
        web.state.set_only_db(None)


def _gz(data):
    return gzip.compress(data)


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
        keys.append(web.parse_cache._parse_key(path))
    was = web.parse_cache.PARSE_CACHE
    assert keys[0] != keys[1] or not was, "two different 1.x MiB logs must not share a parse-cache entry"


@case
def test_parse_cache_refuses_a_directory_others_can_write(c):
    import tempfile
    old, d = tempfile.tempdir, tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "logtool-parsed"))
    os.chmod(os.path.join(d, "logtool-parsed"), 0o777)
    tempfile.tempdir = d
    try:
        assert web.parse_cache._parsed_dir() is None and web.parse_cache._cache_load("0" * 40) is None
        os.chmod(os.path.join(d, "logtool-parsed"), 0o700)
        assert web.parse_cache._parsed_dir() == os.path.join(d, "logtool-parsed")
    finally:
        tempfile.tempdir = old


@case
def test_unloading_a_log_while_it_decompresses_leaves_no_copy_behind(c):
    fid = upload(c, "a.log.gz", _gz(LINE * 100)).get_json()[0]["id"]
    real = web.loading._decompress_once

    def unload_meanwhile(path, reserved=0):
        out = real(path, reserved)
        web.app.test_client().delete("/api/files/" + fid)
        return out
    web.loading._decompress_once = unload_meanwhile
    try:
        c.get("/api/analyze?init=1")
    finally:
        web.loading._decompress_once = real
    left = [n for n in os.listdir(web.storage.UPLOAD_DIR) if n != web.storage.OWNER_FILE]
    assert not web.state.FILES and not left, left


@case
def test_a_summary_with_a_bad_file_count_is_not_a_summary(c):
    import json
    bad = dict({k: [] for k in web.loading.SUMMARY_KEYS}, totals={}, stats={}, groups={}, files_n="x")
    upload(c, "s.json", json.dumps(bad).encode())
    st, j = analyze(c)
    assert st == 400 and web.state.FILES[web.state.ORDER[-1]]["problem"] == "not a logtool export", (st, j)


@case
def test_reset_reads_every_database_again(c):
    try:
        c.post("/api/only_db", json={"db": "mydb"})
        c.post("/api/reset")
        assert c.get("/api/only_db").get_json() == {"db": None}
    finally:
        web.state.set_only_db(None)


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
        web.state.set_only_db(None)


@case
def test_colored_logs_are_scanned_as_they_are_parsed(c):
    color = ("2026-09-14 13:00:00,000 7 \x1b[1;33m\x1b[1;49mWARNING\x1b[0m db odoo.models: odd thing\n"
             "2026-09-14 13:00:01,000 7 \x1b[1;32m\x1b[1;49mINFO\x1b[0m other odoo.models: elsewhere\n")
    path, p = _profile(color)
    assert p.parts["db"].issues, "the parser counts the colored warning"
    got = olp.scan_issue_occurrences(path, "WARNING", "odoo.models", "odd thing", set(), None, None, 10)
    assert [o["line"] for o in got] == [1], got
    with parser.logio._numbered_lines(path) as numbered:
        assert [i for i, _ in parser.logio._only_db_lines(numbered, "other")] == [2]


@case
def test_an_impossible_timestamp_is_never_a_header(c):
    assert parser.lines._parse_header_g("2026-02-31 13:00:00,000 7 INFO db x.y: not a real date\n") is None
    assert parser.lines._parse_header("2026-09-14 25:00:00,000 7 INFO db x.y: not a real hour\n") is None
    assert parser.lines._parse_header("2026-09-14 13:00:00,000 7 INFO db x.y: fine\n")[3] == "db"


@case
def test_a_slow_query_log_unloaded_while_it_loads_leaves_nothing_behind(c):
    path = os.path.join(tempfile.mkdtemp(), "pg_slow_queries.log")
    with open(path, "w") as fh:
        fh.write("-" * 80 + "\n2026-11-10 17:24:10.365 UTC my_db: approximate duration: 41 seconds\n\nSELECT 1\n")
    rec = web.loading._add_file(path, "pg_slow_queries.log", False)
    web.loading._load_batch([rec])                          # read ...
    assert web.state.SLOWQ[rec["id"]]["entries"]
    with web.state.LOCK:                                    # ... and unloaded before that request was done with it
        del web.state.FILES[rec["id"]]
        web.state.ORDER.remove(rec["id"])
    web.loading._load_pending()
    assert rec["id"] not in web.state.SLOWQ, list(web.state.SLOWQ)
