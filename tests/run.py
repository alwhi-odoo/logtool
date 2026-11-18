#!/usr/bin/env python3
"""
Run the test cases without a test framework (pytest runs the same ones: `python -m pytest`):

    python tests/run.py                # everything
    python tests/run.py gz chunk       # only the cases whose name contains one of these words

The HTTP cases go through Flask's test client (no network).
"""

from __future__ import annotations

import glob
import importlib
import os
import sys

from support import CASES, HERE, fresh


def main(argv):
    for path in sorted(glob.glob(os.path.join(HERE, "test_*.py"))):          # importing a module registers its cases
        importlib.import_module(os.path.splitext(os.path.basename(path))[0])
    only = argv[1:]
    failed = 0
    ran = 0
    for fn in CASES:
        if only and not any(o in fn.__name__ for o in only):
            continue
        ran += 1
        try:
            fn(fresh())
            print("ok    " + fn.__name__)
        except Exception as e:                            # noqa: BLE001 - report every failure, keep going
            failed += 1
            print("FAIL  %s: %s: %s" % (fn.__name__, type(e).__name__, e))
    fresh()
    print("\n%d tests, %d failed" % (ran, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
