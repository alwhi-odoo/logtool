"""Small helpers with no state of their own: log-clock time conversions and the LRU cache read / write.
"""

from __future__ import annotations

import datetime

from .. import profiler as olp


def _iso(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _secs(stamp):
    """'2026-09-14T13:05:09' -> seconds since the unix epoch (the log's own clock)."""
    return olp.stamp_minute(stamp[:10], stamp[11:16]) * 60 + int(stamp[17:19])


def _duration(t0, t1):
    try:
        return (datetime.datetime.fromisoformat(t1) - datetime.datetime.fromisoformat(t0)).total_seconds()
    except (TypeError, ValueError):
        return None


def _cache_get(cache, key):
    """LRU read from an OrderedDict cache: a hit counts as this entry's most recent use, so a run of
    later insertions (e.g. dragging across the histogram) doesn't evict something still in play."""
    if key not in cache:
        return None
    cache.move_to_end(key)
    return cache[key]


def _remember(cache, key, value, limit=24):
    """LRU write: caches `value`, evicting the least recently used entry once `cache` is over `limit`."""
    if key not in cache and len(cache) >= limit:
        cache.popitem(last=False)
    cache[key] = value
    cache.move_to_end(key)
    return value


_stamp_secs = _secs      # of an occurrence's "YYYY-MM-DDTHH:MM:SS" stamp (the raw-line scanners' "stamp")
