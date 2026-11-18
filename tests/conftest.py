"""pytest only: every case gets a freshly reset app as `c` (see support.py; tests/run.py runs them without pytest)."""

import pytest

from support import fresh


@pytest.fixture
def c():
    yield fresh()
    fresh()
