"""The parse cache: loading the same logs again (a restart, the same files dropped again) reads their parsed
chunks back from disk instead of parsing them again.
"""

from __future__ import annotations

import hashlib
import os
import pickle
import tempfile

from . import state
from .. import parser as _parser
from .. import profiler as olp


PARSE_CACHE = os.environ.get("LOGTOOL_PARSE_CACHE", "1") != "0"
PARSE_CACHE_MAX = 4 << 30           # bytes of parsed chunks kept on disk; the least recently used go first
_PARSER_ID = None


def _parsed_dir():
    """Where parsed chunks are kept (~/.cache/logtool/logtool-parsed, see cli.main), or None when it cannot be
    trusted: the cache is unpickled, so it must be a directory of this user's that nobody else can write to
    (a shared /tmp fallback would let another local user plant a file that runs code when loaded)."""
    d = os.path.join(tempfile.gettempdir(), "logtool-parsed")
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.lstat(d)
    except OSError:
        return None
    if not os.path.isdir(d) or os.path.islink(d) or st.st_uid != os.getuid() or st.st_mode & 0o022:
        return None
    return d


def _parse_key(path):
    """What a parse of `path` depends on: its content (size, first and last MiB -- logs only grow, and two
    different logs of the same size do not start and end with the same bytes), the parser's own code, the
    chunking settings and the database it was limited to (ONLY_DB). None when it cannot be read (it is then parsed, and fails, as usual)."""
    global _PARSER_ID
    if not PARSE_CACHE:
        return None
    try:
        if _PARSER_ID is None:                      # every module of the parser: a change in any of them changes the key
            folder, code = os.path.dirname(_parser.__file__), hashlib.sha1()
            for name in sorted(n for n in os.listdir(folder) if n.endswith(".py")):
                with open(os.path.join(folder, name), "rb") as fh:
                    code.update(name.encode() + b"\0" + fh.read() + b"\0")
            _PARSER_ID = code.hexdigest()
        size = os.path.getsize(path)
        h = hashlib.sha1(("%s|%d|%d|%d|%s|" % (_PARSER_ID, size, olp.PAR_MIN_CHUNK, olp.PAR_MAX_CHUNK,
                                               "" if state.ONLY_DB is None else "db=" + state.ONLY_DB)).encode())
        with open(path, "rb") as fh:
            h.update(fh.read(1 << 20))
            if size > 1 << 20:                      # the last MiB (or what follows the first one, if less)
                fh.seek(max(1 << 20, size - (1 << 20)))
                h.update(fh.read())
        return h.hexdigest()
    except OSError:
        return None


def _cache_load(key):
    d = _parsed_dir() if key is not None else None
    if d is None:
        return None
    path = os.path.join(d, key + ".pickle")
    try:
        with open(path, "rb") as fh:
            chunks, index = pickle.load(fh)
        os.utime(path)                              # recently used: evicted last
        return chunks, index, None
    except FileNotFoundError:
        return None
    except Exception:                               # corrupt / from another Python: parse again (and replace it)
        return None


def _cache_save(key, parsed):
    """Write one file's parsed chunks (best effort: no room, no cache), then trim the cache to PARSE_CACHE_MAX."""
    d = _parsed_dir() if key is not None else None
    if d is None:
        return
    try:
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, ".%s.%d.tmp" % (key, os.getpid()))
        with open(tmp, "wb") as fh:
            pickle.dump((parsed[0], parsed[1]), fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, os.path.join(d, key + ".pickle"))
        entries = sorted((e for e in os.scandir(d) if e.name.endswith(".pickle")), key=lambda e: e.stat().st_mtime, reverse=True)
        total = 0
        for e in entries:
            total += e.stat().st_size
            if total > PARSE_CACHE_MAX:
                os.remove(e.path)
    except OSError:
        try:
            os.remove(tmp)
        except (OSError, UnboundLocalError):
            pass
