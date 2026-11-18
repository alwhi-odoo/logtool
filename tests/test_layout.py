"""The layout of the code itself (see ARCHITECTURE.md): what keeps the modules small, separate and wired correctly.
"""

from __future__ import annotations

import ast
import glob
import json
import os
import re
import sys
from urllib.parse import quote

from support import LOGIN, ROOT, analyze, case, upload

from logtool import profiler as olp

PKG = os.path.join(ROOT, "logtool")
MAX_LINES = 1000            # of one Python module or script: past this, split it (one concern per module)
REBOUND = {"state": ("ONLY_DB", "ONLY_GEN"), "storage": ("UPLOAD_DIR",)}     # read as <module>.<name>, never imported by name


def _sources(*parts):
    for path in sorted(glob.glob(os.path.join(PKG, *parts))):
        with open(path, encoding="utf-8") as fh:
            yield os.path.relpath(path, ROOT), fh.read()


@case
def test_every_parser_name_the_web_app_uses_is_exported(c):
    missing = sorted({"%s: olp.%s" % (path, name) for path, src in _sources("web", "*.py")
                      for name in re.findall(r"\bolp\.([A-Za-z_]\w*)", src) if not hasattr(olp, name)})
    assert not missing, "add these to logtool/profiler.py: %s" % missing


@case
def test_the_parser_uses_the_standard_library_only(c):
    stdlib = getattr(sys, "stdlib_module_names", None)
    bad = []
    for path, src in _sources("parser", "*.py"):
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module]
            elif isinstance(node, ast.ImportFrom) and node.level == 2 and node.module is None:
                bad += ["%s: from .. import %s" % (path, a.name) for a in node.names if a.name != "__version__"]
                continue
            elif isinstance(node, ast.ImportFrom) and node.level >= 2:
                bad.append("%s: imports %s%s" % (path, "." * node.level, node.module))
                continue
            else:
                continue
            bad += ["%s: imports %s" % (path, n) for n in names
                    if n.split(".")[0] in ("flask", "werkzeug", "logtool") or (stdlib and n.split(".")[0] not in stdlib)]
    assert not bad, bad


@case
def test_rebound_state_is_never_imported_by_name(c):
    bad = []
    for path, src in _sources("web", "*.py"):
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module in REBOUND:
                bad += ["%s imports %s from .%s: read it as %s.%s (it is rebound)" % (path, a.name, node.module, node.module, a.name)
                        for a in node.names if a.name in REBOUND[node.module]]
    assert not bad, bad


@case
def test_a_module_imports_only_from_the_ones_listed_above_it(c):
    # the order is the one of each package's __init__.py (and of the tables in ARCHITECTURE.md): no import cycles
    bad = []
    for pkg in ("parser", "web"):
        sources = dict(_sources(pkg, "*.py"))
        init = ast.parse(sources.pop(os.path.join("logtool", pkg, "__init__.py")))
        order = [a.name for node in init.body if isinstance(node, ast.ImportFrom) and node.level == 1 and not node.module
                 for a in node.names]
        names = {os.path.splitext(os.path.basename(path))[0]: path for path in sources}
        bad += ["%s is not listed in logtool/%s/__init__.py" % (path, pkg) for name, path in names.items() if name not in order]
        for name, path in names.items():
            for node in ast.walk(ast.parse(sources[path])):
                if isinstance(node, ast.ImportFrom) and node.level == 1:
                    used = [node.module] if node.module else [a.name for a in node.names]
                    bad += ["%s imports .%s, which is listed below it" % (path, m) for m in used
                            if m in order and name in order and order.index(m) >= order.index(name)]
    assert not bad, bad


@case
def test_no_module_grows_past_the_size_limit(c):
    big = ["%s (%d lines)" % (path, src.count("\n")) for pattern in (("*.py",), ("parser", "*.py"), ("web", "*.py"))
           for path, src in _sources(*pattern) if src.count("\n") > MAX_LINES]
    assert not big, "split these (more than %d lines): %s" % (MAX_LINES, big)


@case
def test_the_page_and_its_export_load_every_script_in_one_order(c):
    # the frontend is classic scripts in logtool/static/, loaded in the order templates/index.html lists them
    scripts = {os.path.basename(path): src for path, src in _sources("static", "*.js")}
    big = ["%s (%d lines)" % (name, src.count("\n")) for name, src in scripts.items() if src.count("\n") > MAX_LINES]
    assert scripts and not big, "split these (more than %d lines): %s" % (MAX_LINES, big)
    order = re.findall(r'<script src="/static/([^"]+)"></script>', c.get("/").get_data(as_text=True))
    assert sorted(order) == sorted(scripts), "index.html must list every script of logtool/static/ once: %s" % order
    upload(c, "a.log", LOGIN)
    analyze(c)
    page = c.get("/api/export_html?view=" + quote(json.dumps({"items": []}))).get_data(as_text=True)
    inline = re.findall(r"<script>(.*?)</script>", page, re.S)             # Export HTML: the same scripts, inline
    assert inline == [scripts[name] for name in order], [s[:60] for s in inline]
    assert "<script src" not in page and '<link rel="stylesheet"' not in page


@case
def test_the_front_doors_still_open(c):
    from logtool import app as A
    from logtool import web
    assert A.app is web.app and A.main is web.main and olp.main is not None
    rules = {r.rule for r in A.app.url_map.iter_rules()}
    assert {"/", "/api/analyze", "/api/files", "/api/export_html", "/api/op_lines", "/api/domain"} <= rules, sorted(rules)
