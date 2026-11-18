"""Scrubbing for the exports: blank out names, emails and addresses; pseudonymize clients, accounts and ids.
"""

from __future__ import annotations

import os
import re

from .. import profiler as olp


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# IPv6: all eight groups, or a "::" shortening (so 13:05:09 times stay), with an optional zone (%eth0)
_H = "[0-9A-Fa-f]{1,4}"
_IP6_RE = re.compile(r"(?<![\w:.])(?:{h}(?::{h}){{7}}|{h}(?::{h}){{0,6}}::(?:{h}(?::{h}){{0,6}})?|::{h}(?::{h}){{0,6}})"
                     r"(?:%[\w.]+)?(?![\w:])".format(h=_H))
_LOG_SUFFIXES = (".gz", ".bz2", ".json", ".log", ".txt")
# words too common in an Odoo log to blank out: a file named odoo.log must not turn every 'odoo.http' into [redacted]
_GENERIC_NAMES = {"odoo", "odoo-server", "openerp", "server", "log", "logs", "test", "tests", "debug", "error",
                  "errors", "warning", "info", "http", "web", "base", "main", "prod", "production", "staging", "dev",
                  "local", "db", "database", "postgres", "default", "none", "worker", "cron", "access", "out"}


def _secret_names(dbs, files):
    """The database and file names an export must not show, as words to blank out (see _scrub). A file name
    loses its suffixes ('acme-prod.log.gz' -> 'acme-prod'); names shorter than 4 characters or common words
    ('odoo', 'test') are left alone: blanking them would wreck every logger and message that contains them."""
    names = set(dbs)
    for f in files:
        name = f["name"]
        while name.lower().endswith(_LOG_SUFFIXES):
            name = os.path.splitext(name)[0]
        names.add(name)
    names = {n for n in names if n and len(n) >= 4 and n.lower() not in _GENERIC_NAMES}
    return sorted(names, key=len, reverse=True)


def _scrub(text, secrets):
    """Blank out this selection's own database / file names wherever they show up in free text as a whole
    name (not inside a longer word), plus email and IP (v4 and v6) addresses. Numbers and ids are already
    replaced by '#' upstream (see the issue message template); this only catches what that pass does not."""
    if not text:
        return text
    for s in secrets:
        if s in text:
            text = re.sub(r"(?<![\w-])%s(?![\w-])" % re.escape(s), "[redacted]", text)
    return _IP6_RE.sub("[ip]", _IP_RE.sub("[ip]", _EMAIL_RE.sub("[email]", text)))


class _Alias:
    """Stable pseudonyms: the same (kind, value) always gets the same name, e.g. ("ip", "10.0.0.7") -> "ip-3"."""

    def __init__(self):
        self.maps = {}

    def __call__(self, kind, value):
        m = self.maps.setdefault(kind, {})
        if value not in m:
            m[value] = "%s-%d" % (kind, len(m) + 1)
        return m[value]


def _scrub_ids(text, alias, kind="id"):
    """Every number in `text` replaced by a stable pseudonym (see _Alias): user and record ids, for Export HTML."""
    return re.sub(r"\d+", lambda m: alias(kind, m.group()), text)


def _scrub_url(url, secrets, alias=None):
    """A raw path with its query values and token-like segments blanked (they can be access tokens); ids stay,
    unless `alias` is given: then they become pseudonyms too (see _scrub_ids)."""
    path, q, query = url.partition("?")
    path = "/".join("<token>" if olp._TOKEN.match(p) and any(c.isdigit() for c in p) else p for p in path.split("/"))
    if alias:
        path = _scrub_ids(path, alias)
    return _scrub(path + ("?" + re.sub(r"=[^&]*", "=…", query) if q else ""), secrets)


def _scrub_facts(facts, secrets, alias, ids=False):
    """_facts_json() output with clients and accounts pseudonymized (see _Alias) and free text scrubbed.
    ids: user and record ids (and the numbers behind a '#') too, for Export HTML."""
    def one(slot, v):
        if v == olp.FACT_OTHER:
            return v
        if slot == olp.F_IP:
            return alias("ip", v)
        if slot == olp.F_SESSION:
            return alias("session", v)
        if slot == olp.F_LOGIN:
            return alias("user", v)
        if slot == olp.F_URL:
            return _scrub_url(v, secrets, alias if ids else None)
        if ids and slot in (olp.F_USER, olp.F_LOGGED_USER):
            return _scrub_ids(v, alias, "uid")
        if ids and (slot in (olp.F_REC, olp.F_LOGGED_REC) or slot.endswith("#")):
            return _scrub_ids(_scrub(v, secrets), alias)
        return _scrub(v, secrets)
    return [dict(f, detail=_scrub(f["detail"], secrets),
                 values=[[one(f["detail"], e[0]), e[1]] + ([[alias("user", x) for x in e[2]]] if len(e) > 2 else [])
                         for e in f["values"]])
            for f in facts]


def _scrub_row(r, secrets):
    return dict(r, family=_scrub(r["family"], secrets), app=_scrub(r["app"], secrets),
                operation=_scrub(r["operation"], secrets))


def _scrub_timeline_op(o, secrets):
    return dict(o, family=_scrub(o["family"], secrets), app=_scrub(o["app"], secrets),
                operation=_scrub(o["operation"], secrets))


def _scrub_issue(i, secrets, alias, ids=False):
    return {"level": i["level"], "logger": _scrub(i["logger"], secrets), "exc": _scrub(i["exc"], secrets),
            "src": _scrub(i["src"], secrets), "message": _scrub(i["message"], secrets), "n": i["n"],
            "first": i["first"], "last": i["last"], "ops_n": i["ops_n"], "unattributed": i["unattributed"],
            "ops": [[_scrub(op, secrets), n] for op, n in i["ops"]], "hist": i["hist"],
            "facts": _scrub_facts(i["facts"], secrets, alias, ids)}
