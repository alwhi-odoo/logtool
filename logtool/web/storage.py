"""Where logtool keeps files on disk: this process's upload folder, the temp directory, cleaning up after old runs.

UPLOAD_DIR is rebound by `_upload_dir()`: other modules read it as `storage.UPLOAD_DIR`.
"""

from __future__ import annotations

import datetime
import errno
import os
import shutil
import tempfile

from .state import DIR_LOCK


UPLOAD_DIR = None       # created on first upload


def _remove_file(rec):
    if rec["owned"]:
        try:
            os.remove(rec["path"])
        except OSError:
            pass


def _disk_msg(e):
    """A message worth showing a user for a failed write, with the out-of-space case called out (it is
    the one they can actually act on: unload some files, or free up space where UPLOAD_DIR lives)."""
    if isinstance(e, OSError) and e.errno in (errno.ENOSPC, errno.EDQUOT):
        return ("out of disk space (or over its quota) where logtool keeps the files it loads%s. "
                 "Unload some files, or free up space there, then try again."
                 % (" (%s)" % UPLOAD_DIR if UPLOAD_DIR else ""))
    return "%s: %s" % (type(e).__name__, e)


def _free_bytes(path):
    """Free space on the filesystem holding `path` (a directory, or one that will be created), or None
    if that cannot be determined -- callers then just proceed and let the write itself fail or not."""
    d = path if os.path.isdir(path) else os.path.dirname(path) or "."
    try:
        return shutil.disk_usage(d).free
    except OSError:
        return None


OWNER_FILE = ".owner"     # in each UPLOAD_DIR: the pid of the logtool process that uses it


def _proc_start(pid):
    """When process `pid` started (clock ticks since boot, from /proc), or "" where that cannot be read:
    tells a live owner from a new process that was handed the same pid (a container restart, ...)."""
    try:
        with open("/proc/%d/stat" % pid) as fh:
            return fh.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError, ValueError):
        return ""


def _upload_dir():
    """This process's own folder for uploaded and decompressed logs (created once, whichever thread asks
    first), marked with its pid and start time so another logtool starting meanwhile (see _pick_tmp_dir)
    knows it is in use."""
    global UPLOAD_DIR
    with DIR_LOCK:
        if UPLOAD_DIR is None:
            d = tempfile.mkdtemp(prefix="logtool_")
            try:
                with open(os.path.join(d, OWNER_FILE), "w") as fh:
                    fh.write("%d %s" % (os.getpid(), _proc_start(os.getpid())))
            except OSError:
                pass
            UPLOAD_DIR = d
        return UPLOAD_DIR


def _abandoned(folder):
    """Is this 'logtool_*' folder left over from a process that is gone? A folder without an owner file
    (written by an older version) counts as abandoned only once it has not been touched for two days."""
    try:
        with open(os.path.join(folder, OWNER_FILE)) as fh:
            owner = fh.read().split()
        pid, started = int(owner[0]), owner[1] if len(owner) > 1 else ""
    except (OSError, ValueError, IndexError):
        try:
            return os.path.getmtime(folder) < datetime.datetime.now().timestamp() - 2 * 86400
        except OSError:
            return False
    if pid <= 0 or pid == os.getpid():           # this process has no folder yet: one with its pid is an old run's
        return True
    try:
        os.kill(pid, 0)                          # signal 0: only asks whether the process exists
    except ProcessLookupError:
        return True
    except OSError:                              # exists, owned by someone else
        pass
    return bool(started) and started != _proc_start(pid)     # a new process that got the same pid


def _pick_tmp_dir():
    """Where every file logtool loads ends up, one way or another: uploaded logs, their decompressed
    copies (see _decompress_once), and -- because this also becomes tempfile.tempdir, see cli.main() --
    even Werkzeug's own internal buffering of an incoming upload, before this module's code ever runs.
    The OS default temp directory (usually /tmp) is often its own small, separately quota'd filesystem
    (a tmpfs capped well below the machine's real disk), so a single log or two can exhaust it even
    though there is plenty of room elsewhere; a directory under the user's home is far more likely to
    have real space. Falls back to the OS default (None) if that is not writable either.

    Best-effort sweeps this directory of the 'logtool_*' folders left behind by a previous run that
    did not shut down cleanly (a crash, SIGKILL, ...). Only abandoned ones (see _abandoned): another
    logtool still running keeps its files -- it re-reads them for raw log lines and time intervals."""
    base = os.path.join(os.path.expanduser("~"), ".cache", "logtool")
    try:
        os.makedirs(base, exist_ok=True)
        probe = tempfile.mkdtemp(prefix=".write_check_", dir=base)
        os.rmdir(probe)
    except OSError:
        return None
    try:
        listening = _unix_socket_paths()
        for name in os.listdir(base):
            path = os.path.join(base, name)
            if name.startswith("logtool_") and _abandoned(path):
                shutil.rmtree(path, ignore_errors=True)
            elif name.startswith("pymp-") and listening is not None and _stale_pymp(path, listening):
                shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass
    return base


def _unix_socket_paths():
    """The paths of the Unix sockets some process is listening on (from /proc/net/unix), or None."""
    try:
        with open("/proc/net/unix") as fh:
            return {line.split(None, 7)[7].strip() for line in fh if len(line.split(None, 7)) == 8}
    except OSError:
        return None


def _stale_pymp(folder, listening):
    """Is this 'pymp-*' folder (the worker pool's sockets, which versions before 1.2 left in ~/.cache/logtool
    when killed) unused? It is once no process listens on any socket in it any more."""
    try:
        return not any(os.path.join(folder, n) in listening for n in os.listdir(folder))
    except OSError:
        return False
