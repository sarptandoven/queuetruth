"""Markout and realized-spread reference values (SPEC 5.7.5 "Markouts", oracle O-SCORE-3)
-> tests/fixtures/reference/markouts.json.

One instrument-day: `price_exp = -9`, `tick_size = 10_000_000` (0.01 in price units), `size_exp = 0`,
`contract_multiplier = 1.0`, `last_event_ts` given explicitly. BBO rows follow qt.bbo (`bid_px`/`ask_px` null
when the side is empty, sizes 0 then). For each fill (d = +1 buy, -1 sell; P; qty) and horizon h:
- `M(t)` = reference price of the PREVAILING quote: the last BBO row with `ts_event <= t`; null if none, if a side
  is empty, or if `t > last_event_ts` (t equal to last_event_ts is still valid).
- ref `mid`: `(bid_px + ask_px) / 2`; ref `microprice`: `(bid_sz * ask_px + ask_sz * bid_px) / (bid_sz + ask_sz)`
  (null if both sizes are 0).
- `mo_ticks = d (M(t + h) - P) / tick`; `mo_ccy = d (M(t + h) - P) 10^price_exp * qty 10^size_exp * multiplier`;
  `mm_ticks = d (M(t + h) - M(t-)) / tick`, `M(t-)` from the last BBO row strictly before t.
- Identity: for the taker on the other side of a maker fill, the SEC realized spread `RS_h = 2 * mo` of the maker
  (`rs_taker_ticks`), i.e. `2 * d_taker * (P - M(t + h)) / tick` with `d_taker = -d`.
All values computed in exact rational arithmetic, then converted to float.
"""

from __future__ import annotations

from fractions import Fraction as Fr

from _common import header, write_fixture

PRICE_EXP = -9
TICK = 10_000_000
SIZE_EXP = 0
MULT = 1.0
MS = 1_000_000
S = 1_000_000_000
T0 = 1_767_261_600_000_000_000  # 2026-01-01 10:00:00 UTC in ns (fixed constant, not wall clock)


def px(dollars: str) -> int:
    v = Fr(dollars) * 10**9
    assert v.denominator == 1
    return int(v)


BBO = [  # ts offset ns, bid_px, bid_sz, ask_px, ask_sz
    (0, "100.00", 500, "100.02", 300),
    (40 * MS, "100.00", 200, "100.02", 300),
    (100 * MS, "100.01", 100, "100.02", 300),
    (S, "100.01", 100, None, 0),  # ask side empty
    (S + 500 * MS, "100.00", 400, "100.03", 100),
    (10 * S, "99.98", 250, "100.01", 750),
    (15 * S, "100.05", 600, "100.07", 200),
]
LAST_EVENT = 70 * S

FILLS = [  # ts offset ns, side, price, qty
    (0, "buy", "100.00", 100),  # fill at the same ts as a BBO row: M(t-) has no strictly-earlier row -> null
    (40 * MS, "sell", "100.02", 50),  # t + 0 ... t + 60 ms hits BBO at exactly 100 ms (inclusive as-of)
    (900 * MS, "buy", "100.01", 25),  # t + 100 ms lands on the one-sided quote -> null
    (5 * S, "sell", "100.03", 10),
    (60 * S, "buy", "100.06", 7),  # t + 10 s = 70 s = last_event_ts (valid); t + 15 s, 60 s, 300 s beyond -> null
]
HORIZONS = [50 * MS, 60 * MS, 100 * MS, S, 10 * S, 15 * S, 60 * S, 300 * S]


def quote_before(t: int, strict: bool):
    row = None
    for r in BBO:
        if (r[0] < t) if strict else (r[0] <= t):
            row = r
    return row


def ref_price(row, ref: str):
    if row is None or row[1] is None or row[3] is None:
        return None
    b, bs, a, as_ = px(row[1]), row[2], px(row[3]), row[4]
    if ref == "mid":
        return Fr(b + a, 2)
    if bs + as_ == 0:
        return None
    return Fr(bs * a + as_ * b, bs + as_)


def M(t: int, ref: str, strict: bool = False):
    if t > LAST_EVENT:
        return None
    return ref_price(quote_before(t, strict), ref)


def main() -> None:
    out = []
    for i, (t, side, p, qty) in enumerate(FILLS):
        d = 1 if side == "buy" else -1
        P = px(p)
        for ref in ("mid", "microprice"):
            m_pre = M(t, ref, strict=True)
            for h in HORIZONS:
                m = M(t + h, ref)
                mo = None if m is None else d * (m - P) / TICK
                scale = Fr(10) ** PRICE_EXP * qty * Fr(10) ** SIZE_EXP * Fr(MULT)
                mo_ccy = None if m is None else float(d * (m - P) * scale)
                mm = None if (m is None or m_pre is None) else d * (m - m_pre) / TICK
                out.append(
                    {
                        "fill_index": i,
                        "horizon_ns": h,
                        "ref": ref,
                        "mo_ticks": None if mo is None else float(mo),
                        "mo_ccy": mo_ccy,
                        "mm_ticks": None if mm is None else float(mm),
                        "M_t_plus_h": None if m is None else float(m),
                        "M_t_minus": None if m_pre is None else float(m_pre),
                        "rs_taker_ticks": None if mo is None else float(2 * (-d) * (P - m) / TICK),
                    }
                )
                if mo is not None:
                    assert 2 * mo == 2 * (-d) * (P - m) / TICK
    position, pos = [], 0
    for t, side, _, qty in FILLS:
        pos += qty if side == "buy" else -qty
        position.append({"ts_event": T0 + t, "position": pos})
    doc = header(["O-SCORE-3"], "gen_markouts.py", __doc__.strip())
    doc["tolerance"] = {"all": "abs 1e-12 (mo_ccy rel 1e-12)"}
    doc["instrument"] = {
        "price_exp": PRICE_EXP,
        "tick_size": TICK,
        "size_exp": SIZE_EXP,
        "contract_multiplier": MULT,
        "last_event_ts": T0 + LAST_EVENT,
    }
    doc["bbo"] = [
        {
            "ts_event": T0 + r[0],
            "bid_px": None if r[1] is None else px(r[1]),
            "bid_sz": r[2],
            "ask_px": None if r[3] is None else px(r[3]),
            "ask_sz": r[4],
        }
        for r in BBO
    ]
    doc["fills"] = [{"ts_event": T0 + t, "side": s, "px": px(p), "qty": q} for t, s, p, q in FILLS]
    doc["horizons_ns"] = HORIZONS
    doc["markouts"] = out
    doc["position_series"] = position
    print(write_fixture("markouts.json", doc))


if __name__ == "__main__":
    main()
