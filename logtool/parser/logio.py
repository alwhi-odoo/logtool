"""Reading log files: plain, .gz and .bz2, whole or one byte range of a plain file (a chunk), with line numbers.

`_lines()` is what a parser worker reads for its chunk; `_numbered_lines()` is what the raw-line scanners
(scan.py) read, so their line numbers agree with the chunks the parser made.
"""

from __future__ import annotations

import bz2
import contextlib
import gzip
import os
import sys
import zlib

from .lines import _db_of, _parse_header, _parse_header_g, stamp_minute


def opener(path):
    """A text reader of a log: plain, .gz or .bz2 (by suffix), or "-" for stdin (left open when done)."""
    if path == "-":
        return contextlib.nullcontext(sys.stdin)
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    if path.endswith(".bz2"):
        return bz2.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "rt", encoding="utf-8", errors="replace")


def _only_db_lines(numbered, only):
    """(line number, line) of `numbered` without the entries (header and continuation lines) of databases other
    than `only` -- what Profile.feed leaves out with args.only_db, so the raw lines agree with the counts."""
    skip = False
    for i, raw in numbered:
        g = _parse_header_g(raw)
        if g is not None:
            skip = _db_of(g) not in (only, "-")
        if not skip:
            yield i, raw


@contextlib.contextmanager
def _numbered_lines(path, piece=None):
    """(line number, line) of a whole file (any codec), or with `piece` = (start byte, end byte or None, the
    line number of the first line starting at or after `start`, see Chunk.lines) of that byte range of a plain
    file -- what the parser reads for one chunk."""
    if piece is None or piece[0] is None:
        with opener(path) as fh:
            yield enumerate(fh, 1)
        return
    start, end, first = piece
    gen = _lines(path, start, os.path.getsize(path) if end is None else end)
    try:
        yield enumerate(gen, first)
    finally:
        gen.close()


def _lines(path, start, end, index=None):
    """Yield the lines of a plain file whose first byte lies in [start, end).

    If `index` is a list it receives (byte_offset, minute) every time the minute of the
    leading timestamp changes, so a later call can seek straight to a time range."""
    with open(path, "rb") as f:
        pos = start
        if start > 0:
            f.seek(start - 1)
            pos = start - 1 + len(f.readline())       # skip to the first line starting at/after start
        last = None
        while pos < end:
            line = f.readline()
            if not line:
                break
            if index is not None and line[:1].isdigit():
                key = line[:16]
                if key != last:
                    last = key
                    s = key.decode("ascii", "replace")
                    if len(s) == 16 and s[4] == "-" and s[10] in " T" and s[13] == ":":
                        try:
                            index.append((pos, stamp_minute(s[:10], s[11:16])))
                        except ValueError:
                            pass
            pos += len(line)
            if line.endswith(b"\r\n"):
                line = line[:-2] + b"\n"
            yield line.decode("utf-8", "replace")


READ_ERRORS = (OSError, EOFError, zlib.error)       # a missing, corrupt or truncated log file


def _entry_start(path, x, end, look=1 << 20):
    """The first byte, at or after `x` (and before `end`), of a line that starts a log entry (a header), so a
    chunk cut there keeps an entry's continuation lines (traceback, message body) with it. Within `look`
    bytes; else the first line start after `x`."""
    with open(path, "rb") as f:
        f.seek(max(0, x - 1))
        pos = x - 1 + len(f.readline()) if x > 0 else 0         # the first line starting at or after x
        first = pos
        while pos < min(end, x + look):
            line = f.readline()
            if not line:
                break
            if _parse_header(line.decode("utf-8", "replace")) is not None:
                return pos
            pos += len(line)
    return min(first, end)
