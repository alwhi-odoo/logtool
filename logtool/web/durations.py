"""The call-duration histogram (log-scale bins) of an item's details, from the percentile samples.
"""

from __future__ import annotations

import bisect


# edges of the call-duration histogram in an item's details, in ms: 0.1, 0.15, 0.2, 0.3, 0.5, 0.7, 1, 1.5, ...
# (even steps on a log scale, so 2 ms and 20 s calls both get readable bars)
DUR_EDGES = [round(m * 10 ** e, 4) for e in range(-1, 7) for m in (1, 1.5, 2, 3, 5, 7)]


def _durations(bs):
    """How long the calls of one or more operations took, from their percentile samples (see olp.Bucket.sample),
    each value standing for seen / len(sample) calls: {"bins": [[from ms, to ms, calls], ...] (the empty bins
    before the first and after the last call left out; None: no bound), "estimated": a sample did not keep
    every call}."""
    counts = [0.0] * (len(DUR_EDGES) + 1)
    for b in bs:
        if b.sample:
            _bin_add(counts, sorted(b.sample), b.seen / len(b.sample))
    return {"bins": _bins(counts), "estimated": any(b.seen > len(b.sample) for b in bs)}


def _bin_add(counts, secs, w=1):
    """Adds sorted durations `secs` (seconds), each standing for `w` calls, to the DUR_EDGES bins `counts`."""
    at = [0] + [bisect.bisect_left(secs, e / 1000) for e in DUR_EDGES] + [len(secs)]
    for i in range(len(counts)):
        counts[i] += w * (at[i + 1] - at[i])


def _bins(counts):
    """[[from ms, to ms, calls], ...] of DUR_EDGES bin counts, the empty bins before the first and after the last
    call left out (None: no bound)."""
    bins = [[DUR_EDGES[i - 1] if i else 0, DUR_EDGES[i] if i < len(DUR_EDGES) else None, round(n)]
            for i, n in enumerate(counts)]
    used = [i for i, b in enumerate(bins) if b[2]]
    return bins[used[0]:used[-1] + 1] if used else []
