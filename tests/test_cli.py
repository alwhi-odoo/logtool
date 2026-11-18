"""The command lines: logtool-report's filters and stdin, stopping the server.
"""

from __future__ import annotations

import io
import os
import sys

from support import LINE, ROOT, case

from logtool import profiler as olp


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
