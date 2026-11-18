"""Bucket: the numbers of one operation (calls, SQL / Python / unsplit seconds, percentile sample, per-minute hist).

`PackedHist` is the compact, read-only form a parsed chunk keeps its per-minute data in (see `_pack`).
"""

from __future__ import annotations

import array
from itertools import chain


def _thin(sample, k):
    """At most `k` of a percentile sample, evenly strided: it keeps the spread over time and the distribution
    the same as a random subset would, at a fraction of the cost (a merge thins every operation's sample).
    Bucket.merge weighs the two samples it joins before thinning, so the result still stands for every call."""
    step = -(-len(sample) // k)
    return array.array("d", sample[::step])


class Bucket:
    __slots__ = ("n", "sql", "py", "unsplit", "nq", "mx", "errors", "sample", "seen", "hist", "st", "pc", "ex")

    RESERVOIR = 20000
    LINE_MAX = 400          # longest example log line kept

    def hist_add(self, minute, sql, py, extra=0.0, count=1):
        """Per-minute [requests, sql s, python s, unsplit s]: when the work happened."""
        e = self.hist.get(minute)
        if e is None:
            self.hist[minute] = [count, sql, py, extra]
        else:
            e[0] += count
            e[1] += sql
            e[2] += py
            e[3] += extra

    def __init__(self):
        self.hist = {}
        self.st = {}          # HTTP status -> count
        self.pc = None        # memo of the last percentile: (seen, p, value)
        self.n = 0
        self.sql = 0.0
        self.py = 0.0
        self.unsplit = 0.0    # seconds with no SQL/Python split (jobs logged as messages)
        self.nq = 0
        self.mx = 0.0
        self.errors = 0
        self.sample = array.array("d")   # reservoir of total times, for percentiles (8 bytes each, not a float object)
        self.seen = 0
        self.ex = None        # example log lines: {"first": (stamp, line), "slow": (seconds, stamp, line), "err": (stamp, line)}

    def note(self, stamp, raw, t, is_err):
        """Keep real log lines of this operation to show as examples: the first one, the slowest and the first failing one."""
        ex = self.ex
        if ex is None:
            line = raw.rstrip()[:self.LINE_MAX]
            self.ex = {"first": (stamp, line), "slow": (t, stamp, line)}
            if is_err:
                self.ex["err"] = (stamp, line)
            return
        slow = t > ex["slow"][0]
        if slow or (is_err and "err" not in ex):
            line = raw.rstrip()[:self.LINE_MAX]
            if slow:
                ex["slow"] = (t, stamp, line)
            if is_err and "err" not in ex:
                ex["err"] = (stamp, line)

    def merge(self, o, rng, full=True):
        """Fold another bucket in. full=False skips the per-minute and per-status detail
        (used for roll-ups such as types and apps, which never show it)."""
        self.n += o.n
        self.sql += o.sql
        self.py += o.py
        self.unsplit += o.unsplit
        self.nq += o.nq
        self.errors += o.errors
        mine_seen, self.seen = self.seen, self.seen + o.seen
        self.mx = max(self.mx, o.mx)
        if full:
            for k, v in o.st.items():
                self.st[k] = self.st.get(k, 0) + v
            mine, h = self.hist, o.hist
            if isinstance(h, PackedHist):                # (the usual case: a parsed chunk's) read in place
                vs = h.vals
                for i, k in enumerate(h.keys):
                    j = 4 * i
                    e = mine.get(k)
                    if e is None:
                        mine[k] = [vs[j], vs[j + 1], vs[j + 2], vs[j + 3]]
                    else:
                        e[0] += vs[j]
                        e[1] += vs[j + 1]
                        e[2] += vs[j + 2]
                        e[3] += vs[j + 3]
            else:
                for k, v in h.items():
                    e = mine.get(k)
                    if e is None:
                        mine[k] = list(v)
                    else:
                        e[0] += v[0]
                        e[1] += v[1]
                        e[2] += v[2]
                        e[3] += v[3]
        if o.ex:
            mine = self.ex
            if mine is None:
                self.ex = dict(o.ex)
            else:
                for k, v in o.ex.items():
                    cur = mine.get(k)
                    if cur is None or (v[0] > cur[0] if k == "slow" else v[0] < cur[0]):   # slowest wins; first / err: the earliest stamp
                        mine[k] = v
        # Each sample value stands for seen / len(sample) calls. Where the two sides differ (one of them hit the
        # reservoir cap), the side whose values stand for fewer calls is thinned first, so a big chunk's calls
        # weigh in the percentiles as much as they did in the log (as combine() does for roll-ups).
        theirs = o.sample
        if self.sample and theirs and mine_seen and o.seen:
            if mine_seen * len(theirs) < o.seen * len(self.sample):
                k = max(1, mine_seen * len(theirs) // o.seen)
                if len(self.sample) > k:
                    self.sample = _thin(self.sample, k)
            elif o.seen * len(self.sample) < mine_seen * len(theirs):
                k = max(1, o.seen * len(self.sample) // mine_seen)
                if len(theirs) > k:
                    theirs = _thin(theirs, k)
        self.sample.extend(theirs)                   # in place; thin out only when it has doubled, so merges stay cheap
        if len(self.sample) > 2 * self.RESERVOIR:
            self.sample = _thin(self.sample, self.RESERVOIR)

    @staticmethod
    def combine(buckets, rng):
        """One roll-up bucket over many (e.g. every operation of a type). The percentile sample takes a
        share from each bucket in proportion to how many calls it saw."""
        out = Bucket()
        total_seen = sum(b.seen for b in buckets)
        want = Bucket.RESERVOIR
        samples = []
        for b in buckets:
            out.n += b.n
            out.sql += b.sql
            out.py += b.py
            out.unsplit += b.unsplit
            out.nq += b.nq
            out.errors += b.errors
            out.seen += b.seen
            if b.mx > out.mx:
                out.mx = b.mx
            s = b.sample
            if total_seen > want:
                k = max(1, want * b.seen // total_seen)
                if len(s) > k:
                    s = _thin(s, k)
            samples.extend(s)
        out.sample = array.array("d", samples)
        return out

    def add(self, sql, py, nq, is_error, rng, extra=0.0):
        t = sql + py + extra
        self.n += 1
        self.sql += sql
        self.py += py
        self.unsplit += extra
        self.nq += nq
        if t > self.mx:
            self.mx = t
        if is_error:
            self.errors += 1
        self.seen += 1
        if len(self.sample) < self.RESERVOIR:
            self.sample.append(t)
        else:                                    # reservoir sampling
            j = rng.randrange(self.seen)
            if j < self.RESERVOIR:
                self.sample[j] = t

    @property
    def total(self):
        return self.sql + self.py + self.unsplit

    def pct(self, p):
        if not self.sample:
            return 0.0
        c = self.pc
        if c is not None and c[0] == self.seen and c[1] == p:
            return c[2]
        s = sorted(self.sample)
        v = s[min(len(s) - 1, int(len(s) * p / 100.0))]
        self.pc = (self.seen, p, v)
        return v


class PackedHist:
    """A read-only {minute: [values]} (an operation's per-minute hist, a client's activity) packed into two flat
    arrays: about 40 bytes a minute instead of ~230 for a dict of lists of floats, and quick to send between
    processes. A parsed chunk keeps its profiles this way (they are only ever merged from, see _pack); merged
    profiles keep plain dicts, which they update in place. Reads like the dict: items(), values(), keys, len."""
    __slots__ = ("keys", "vals", "w")

    def __init__(self, d, w):
        ks = sorted(d)
        self.keys, self.w = array.array("q", ks), w
        self.vals = array.array("d", chain.from_iterable(map(d.__getitem__, ks)))

    def __len__(self):
        return len(self.keys)

    def __iter__(self):
        return iter(self.keys)

    def items(self):
        v, w = self.vals, self.w
        return ((k, v[i * w:i * w + w]) for i, k in enumerate(self.keys))

    def values(self):
        v, w = self.vals, self.w
        return (v[i * w:i * w + w] for i in range(len(self.keys)))


def _pack(p):
    """Pack what a parsed piece holds per minute (see PackedHist), now that nothing will be added to it."""
    for prof in [p] + list(p.parts.values()):
        for buckets in (prof.det, prof.hours):
            for b in buckets.values():
                if isinstance(b.hist, dict):
                    b.hist = PackedHist(b.hist, 4)
        prof.clients = {c: PackedHist(h, 2) if isinstance(h, dict) else h for c, h in prof.clients.items()}
