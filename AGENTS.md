# Working on logtool

Read [ARCHITECTURE.md](ARCHITECTURE.md) first: it maps every module and lists the rules. In short:

* `logtool/parser/` is the parser (standard library only). `logtool/web/` is the Flask app. `logtool/profiler.py`
  and `logtool/app.py` are only front doors.
* The web app reaches the parser as `olp.<name>`; a new name it needs goes into `logtool/profiler.py`.
* `ONLY_DB`, `ONLY_GEN` and `UPLOAD_DIR` are rebound at run time: read them as `state.ONLY_DB`,
  `storage.UPLOAD_DIR`, never `from .state import ONLY_DB`.
* `Profile.feed` (`logtool/parser/profile.py`) is the hot loop: time a change of it on a large log.
* The page is `logtool/static/*.js`: plain classic scripts (no modules, no build step) loaded in the order
  `logtool/templates/index.html` lists them. Code that runs while a script loads can only use what an earlier script
  defines; a new script is added to that list.
* One concern per module or script, at most 1,000 lines.

Check a change with:

```bash
python -m logtool.profiler --selftest
python -m pytest            # or: python tests/run.py [words in the case names]
for f in logtool/static/*.js; do node --check "$f"; done
```

A fixed bug gets a test case in the `tests/test_*.py` of its subject and a line in `CHANGELOG.md`. A change in
behaviour is described in `README.md`, and in `SRS.md` / `FILTERS.md` where they cover it.
