"""Routes: loading and unloading files, reset, the database to read.
"""

from __future__ import annotations

import logging
import os
import uuid

from flask import jsonify, request

from . import state
from . import storage
from .loading import _add_file, _file_json
from .server import app
from .state import (
    CHUNKS, FCHUNKS, FILES, INDEX, LOCK, ORDER, SLICES, SLOWQ, SUMMARIES, _forget_selections, set_only_db,
)
from .storage import _disk_msg, _remove_file, _upload_dir


@app.post("/api/reset")
def reset():
    """Unload every file and drop every cache, as if the app had just started (with every database read)."""
    set_only_db(None)
    with LOCK:
        gone = list(FILES.values())
        for cache in (FILES, CHUNKS, INDEX, SLICES, SUMMARIES, SLOWQ):
            cache.clear()
        _forget_selections()
        ORDER.clear()
    for rec in gone:
        _remove_file(rec)
    return jsonify({"ok": True})


@app.get("/api/files")
def list_files():
    with LOCK:
        return jsonify([_file_json(FILES[i]) for i in ORDER])


@app.post("/api/files")
def upload():
    _upload_dir()
    added = []
    for up in request.files.getlist("files"):
        if not up.filename:
            continue
        ext = os.path.splitext(up.filename)[1].lower()
        ext = ext if ext in (".gz", ".bz2", ".json") else ".log"      # opener() picks codec by suffix
        path = os.path.join(storage.UPLOAD_DIR, uuid.uuid4().hex[:10] + ext)
        try:
            up.save(path)
        except OSError as e:                            # e.g. out of disk space / over quota: the files
            try:                                         # saved before this one are already usable
                os.remove(path)
            except OSError:
                pass
            logging.getLogger("logtool").exception("upload failed")
            return jsonify({"added": added, "error": _disk_msg(e)}), 500
        name = up.filename.replace("\\", "/").rsplit("/", 1)[-1]          # a bare file name, whatever the client sent
        added.append(_file_json(_add_file(path, name, True)))
    return jsonify(added)


@app.get("/api/only_db")
def api_only_db_get():
    return jsonify({"db": state.ONLY_DB})


@app.post("/api/only_db")
def api_only_db_post():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "send {\"db\": \"name\"}"}), 400
    name = body.get("db") or ""
    if not isinstance(name, str) or len(name) > 200 or len(name.split()) > 1:
        return jsonify({"error": "a database name is one word"}), 400
    changed = set_only_db(name)
    return jsonify({"db": state.ONLY_DB, "changed": changed})


@app.delete("/api/files/<fid>")
def unload(fid):
    with LOCK:
        rec = FILES.pop(fid, None)
        if rec is None:
            return jsonify({"error": "unknown file"}), 404
        ORDER.remove(fid)
        _forget_selections()
        for cache in (CHUNKS, INDEX, SUMMARIES, SLOWQ):
            cache.pop(fid, None)
        for cache in (SLICES, FCHUNKS):
            for key in [k for k in cache if k[0] == fid]:
                cache.pop(key, None)
    _remove_file(rec)
    return jsonify({"ok": True})
