"""O-SH-5 style forward check of the oracle (audit finding 11): a separate toy exchange runs the same order flow
twice, without the shadow (its MBO output is the tape) and with the shadow as a real order. The oracle, given only
the first run's tape, must reproduce the shadow's fills of the second run exactly.

The toy exchange is written independently of `qt_oracle` (its own book, matching and MBO emission). Its order flow
respects the oracle's documented assumptions: takers execute their full size on the tape (no IOC remainder), and
no hidden order ever crosses the shadow (hidden liquidity is unobservable before it prints).
"""

from __future__ import annotations

import random

import pytest
from qt_oracle.engine import OracleConfig, PartitionRunner
from qt_oracle.scenarios import meta_for, shadow
from qt_oracle.tape import FLAG_LAST, Rec


class ToyExchange:
    def __init__(self, venue: str, sh: dict | None = None, nonprint: set[int] | None = None) -> None:
        self.venue = venue
        self.book: dict[str, dict[int, list[list]]] = {"B": {}, "A": {}}  # px -> [[oid, size, hidden], ...]
        self.where: dict[int, tuple[str, int]] = {}
        self.recs: list[Rec] = []
        self.seq = 0
        self.sh = sh
        self.sh_live = False
        self.sh_left = sh["qty"] if sh else 0
        self.fills: list[tuple[int, int, int, str]] = []
        self.conflict = False
        self.nonprint = nonprint or set()
        self.exec_n = 0

    # -- helpers
    def best(self, side: str, include_hidden: bool = True):
        pxs = [p for p, q in self.book[side].items() if any(include_hidden or not o[2] for o in q)]
        if not pxs:
            return None
        return max(pxs) if side == "B" else min(pxs)

    def avail(self, side: str, limit: int) -> int:
        tot = 0
        for p, q in self.book[side].items():
            if p >= limit if side == "B" else p <= limit:
                tot += sum(o[1] for o in q)
        return tot

    def emit(self, ts, act, side, px, size, oid, last=True, new_seq=True):
        if new_seq:
            self.seq += 1
        self.recs.append(Rec(ts, 1, act, side, px, size, oid, FLAG_LAST if last else 0, self.seq))

    def _rest(self, oid, side, px, size, hidden=False):
        self.book[side].setdefault(px, []).append([oid, size, hidden])
        self.where[oid] = (side, px)

    def _drop(self, oid):
        side, px = self.where.pop(oid)
        q = self.book[side][px]
        q[:] = [o for o in q if o[0] != oid]
        if not q:
            del self.book[side][px]

    def _find(self, oid):
        if oid not in self.where:
            return None
        side, px = self.where[oid]
        for o in self.book[side][px]:
            if o[0] == oid:
                return o
        return None

    # -- shadow
    def maybe_insert_shadow(self, ts: int) -> None:
        sh = self.sh
        if sh is None or self.sh_live or ts <= sh["ts"]:
            return
        self.sh_live = True
        side, px = sh["side"], sh["px"]
        opp = "A" if side == "B" else "B"
        # Marketable on arrival: the shadow takes the opposite side at the resting prices.
        for p in sorted(self.book[opp], reverse=(opp == "B")):
            if not (p <= px if side == "B" else p >= px) or self.sh_left == 0:
                break
            for o in self.book[opp][p]:
                if o[2]:
                    self.conflict = True  # hidden liquidity crossed by the shadow: outside the oracle's scope
                take = min(o[1], self.sh_left)
                o[1] -= take
                self.sh_left -= take
                if take:
                    self.fills.append((sh["ts"], take, p, "taker"))
                if self.sh_left == 0:
                    break
            self.book[opp][p] = [o for o in self.book[opp][p] if o[1] > 0]
            for oid in [k for k, (s, pp) in self.where.items() if s == opp and pp == p]:
                if self._find(oid) is None:
                    self.where.pop(oid)
            if not self.book[opp][p]:
                del self.book[opp][p]
        if self.sh_left > 0:
            self._rest(-1, side, px, self.sh_left)

    # -- order flow
    def add(self, ts, oid, side, px, size, hidden=False):
        opp = "A" if side == "B" else "B"
        if self.sh is not None and hidden and self.sh_live and self._find(-1) is not None:
            if self.sh["side"] == opp and (px <= self.sh["px"] if side == "A" else px >= self.sh["px"]):
                self.conflict = True
        ob = self.best(opp)
        if ob is not None and (px >= ob if side == "B" else px <= ob):
            size = self._take(ts, "B" if side == "B" else "A", size, px, emit=False)
        if size > 0:
            self._rest(oid, side, px, size, hidden)
            if not hidden:
                self.emit(ts, "A", side, px, size, oid)

    def cancel(self, ts, oid, by=None):
        o = self._find(oid)
        if o is None:
            return
        side, px = self.where[oid]
        if by is None or by >= o[1]:
            if not o[2]:
                self.emit(ts, "C", side, px, o[1], oid)
            self._drop(oid)
        else:
            o[1] -= by
            if not o[2]:
                self.emit(ts, "C", side, px, by, oid)

    def modify(self, ts, oid, new_px, delta):
        o = self._find(oid)
        if o is None or o[2]:
            return
        side, px = self.where[oid]
        new = o[1] + delta
        if new <= 0:
            self.emit(ts, "C", side, px, o[1], oid)
            self._drop(oid)
            return
        if new_px == px and delta < 0:
            o[1] = new  # keeps priority
        else:
            self._drop(oid)
            opp = "A" if side == "B" else "B"
            ob = self.best(opp)
            if ob is not None and (new_px >= ob if side == "B" else new_px <= ob):
                new = self._take(ts, side, new, new_px, emit=False)
            if new > 0:
                self._rest(oid, side, new_px, new)
        self.emit(ts, "M", side, new_px, max(new, 0), oid)

    def market(self, ts, aggr, qty, limit):
        self._take(ts, aggr, qty, limit, emit=True)

    def _take(self, ts, aggr, qty, limit, emit):
        rest = "A" if aggr == "B" else "B"
        first = True
        for p in sorted(self.book[rest], reverse=(rest == "B")):
            if qty == 0 or not (p <= limit if rest == "A" else p >= limit):
                break
            q = self.book[rest][p]
            for want_hidden in (False, True):
                for o in q:
                    if o[2] != want_hidden or qty == 0:
                        continue
                    take = min(o[1], qty)
                    o[1] -= take
                    qty -= take
                    if o[0] == -1:
                        self.sh_left -= take
                        self.fills.append((ts, take, p, "maker"))
                        continue
                    if not emit:
                        continue
                    self.exec_n += 1
                    nasdaq = self.venue == "nasdaq"
                    if o[2]:
                        self.emit(ts, "T", "N", p, take, 0, last=True)
                        continue
                    if nasdaq:
                        if self.exec_n not in self.nonprint:
                            self.emit(ts, "T", aggr, p, take, 0, last=False)
                            self.emit(ts, "F", rest, p, take, o[0], last=False, new_seq=False)
                        else:
                            self.emit(ts, "F", rest, p, take, o[0], last=False)
                        self.emit(ts, "C", rest, p, take, o[0], last=True, new_seq=False)
                    else:
                        self.emit(ts, "T", aggr, p, take, 0, last=False, new_seq=not first)
                        if first:
                            first = False
                        self.emit(ts, "F", rest, p, take, o[0], last=False)
                        self.emit(ts, "C", rest, p, take, o[0], last=False)
            q[:] = [o for o in q if o[1] > 0]
            for oid in [k for k, (s, pp) in list(self.where.items()) if s == rest and pp == p]:
                if self._find(oid) is None:
                    self.where.pop(oid)
            if not q:
                del self.book[rest][p]
        if (
            emit
            and self.venue == "cme"
            and self.recs
            and not (self.recs[-1].flags & FLAG_LAST)
            and self.recs[-1].ts_event == ts
        ):
            self.recs[-1].flags |= FLAG_LAST
        return qty


def gen_ops(rng: random.Random, venue: str, n_ops: int = 160):
    """Order flow generated against a shadow-free exchange so that every op is valid on the tape."""
    ex = ToyExchange(venue)
    ops = []
    oid = 1000
    ts = 0
    for k in range(5):
        for side, base in (("B", 100 - k), ("A", 101 + k)):
            for _ in range(rng.randint(1, 3)):
                oid += 1
                ops.append(("add", 0, oid, side, base, rng.randint(1, 20), False))
                ex.add(0, oid, side, base, ops[-1][5])
    for _ in range(n_ops):
        ts += 10
        r = rng.random()
        live = [k for k in ex.where if k > 0]
        if r < 0.38 or len(live) < 6:
            side = rng.choice("BA")
            opp = "A" if side == "B" else "B"
            ob = ex.best(opp)
            sb = ex.best(side) or (100 if side == "B" else 101)
            px = sb + rng.choice([-2, -1, 0, 0, 1]) * (1 if side == "B" else -1)
            if ob is not None and (px >= ob if side == "B" else px <= ob):
                px = ob - 1 if side == "B" else ob + 1
            oid += 1
            hidden = venue == "nasdaq" and rng.random() < 0.15
            if hidden:  # hidden interest only at or behind the displayed best of its side
                db = ex.best(side, include_hidden=False)
                if db is None:
                    continue
                px = min(px, db) if side == "B" else max(px, db)
            ops.append(("add", ts, oid, side, px, rng.randint(1, 20), hidden))
            ex.add(*ops[-1][1:])
        elif r < 0.58:
            o = rng.choice(live)
            by = None if rng.random() < 0.6 else rng.randint(1, 5)
            ops.append(("cancel", ts, o, by))
            ex.cancel(ts, o, by)
        elif r < 0.68 and venue == "cme":
            o = rng.choice(live)
            side, px = ex.where[o]
            choice = rng.random()
            if choice < 0.3:
                new_px, delta = px, 0  # same size: loses priority (A6)
            elif choice < 0.7:
                new_px, delta = px, rng.choice([-3, -1, 2, 4])
            else:
                opp = "A" if side == "B" else "B"
                ob = ex.best(opp)
                new_px = px + rng.choice([-1, 1])
                if ob is not None and (new_px >= ob if side == "B" else new_px <= ob):
                    continue
                delta = 0
            ops.append(("modify", ts, o, new_px, delta))
            ex.modify(*ops[-1][1:])
        else:
            aggr = rng.choice("BA")
            rest = "A" if aggr == "B" else "B"
            ob = ex.best(rest)
            if ob is None:
                continue
            limit = ob + rng.choice([0, 0, 1, 2]) * (1 if aggr == "B" else -1)
            qty = min(rng.randint(1, 60), ex.avail(rest, limit))  # fully executed on the tape
            if qty <= 0:
                continue
            ops.append(("market", ts, aggr, qty, limit))
            ex.market(*ops[-1][1:])
    return ops, ex


def replay(ops, venue, sh=None, nonprint=None):
    ex = ToyExchange(venue, sh, nonprint)
    for op in ops:
        ts = op[1]
        ex.maybe_insert_shadow(ts)
        getattr(ex, op[0])(*op[1:])
    return ex


def _agg(rows):
    out: dict[tuple, int] = {}
    for ts, q, px, liq in rows:
        out[(ts, px, liq)] = out.get((ts, px, liq), 0) + q
    return out


@pytest.mark.parametrize("venue", ["cme", "nasdaq"])
@pytest.mark.parametrize("mode", ["fifo", "directed"])
def test_o_sh_5_oracle_reproduces_counterfactual_exchange(venue: str, mode: str) -> None:
    checked = skipped = with_fills = 0
    for seed in range(150):
        rng = random.Random(seed * 7919 + (venue == "nasdaq"))
        ops, ex1 = gen_ops(rng, venue)
        nonprint = {i for i in range(1, ex1.exec_n + 1) if rng.random() < 0.1} if venue == "nasdaq" else set()
        ex1 = replay(ops, venue, None, nonprint)
        side = rng.choice("BA")
        # Shadow placement around the touch of the book at a random time.
        t_sub = rng.choice([op[1] for op in ops[10:]]) - rng.choice([0, 3])
        probe = replay([op for op in ops if op[1] <= t_sub], venue)
        sb = probe.best(side, include_hidden=False) or (100 if side == "B" else 101)
        px = sb + rng.choice([-2, -1, 0, 0, 1, 2]) * (1 if side == "B" else -1)
        sh = {"side": side, "px": px, "qty": rng.randint(1, 40), "ts": t_sub}
        ex2 = replay(ops, venue, sh, nonprint)
        if ex2.conflict:
            skipped += 1
            continue
        runner = PartitionRunner(
            meta_for(venue), [shadow("X", side, px, sh["qty"], t_sub)], {}, OracleConfig(match_mode=mode)
        )
        res = runner.run(ex1.recs)[0]
        got = _agg([(f["ts_event"], f["fill_qty"], f["fill_px"], f["liquidity"]) for f in res["fills"]])
        want = _agg(ex2.fills)
        assert got == want, (seed, sh, res["notes"][:10])
        assert res["diag"]["unmatched_exec_volume"] == 0, seed
        checked += 1
        with_fills += bool(want)
    assert checked >= 120 and skipped <= 30
    assert with_fills >= 40  # the check is not vacuous
