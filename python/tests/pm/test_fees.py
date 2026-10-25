"""Trading fees, Kalshi fee rounding and Polymarket maker rebates against the venues' own published examples
(`queuetruth.pm.fees`; docs.kalshi.com/getting_started/fee_rounding, the Kalshi fee schedule filing,
docs.polymarket.com/trading/fees and /programs/maker-rebates, read 2026-10-24)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pytest

from queuetruth.errors import QtError
from queuetruth.pm.fees import (
    POLY_CATEGORY_RATES,
    KalshiFeeAccumulator,
    KalshiFeeSchedule,
    PolymarketFeeSchedule,
    maker_rebate,
)

D = Decimal


# ---- Kalshi -------------------------------------------------------------------------------------------------------


def test_kalshi_general_formula_and_maker_shares() -> None:
    q = KalshiFeeSchedule()
    assert q.model_fee(D(100), D("0.50"), maker=False) == D("1.75")  # 0.07 x 100 x 0.5 x 0.5
    assert q.model_fee(D(100), D("0.50"), maker=True) == 0  # quadratic: makers free
    assert q.model_fee(D(100), D("0.30"), maker=False) == q.model_fee(D(100), D("0.70"), maker=False)
    mk = KalshiFeeSchedule("quadratic_with_maker_fees")
    assert mk.model_fee(D(100), D("0.50"), maker=True) == D("0.4375")  # a quarter of the taker fee
    combo = KalshiFeeSchedule("quadratic_with_combo_maker_fees", D("0.5"))
    assert combo.model_fee(D(100), D("0.50"), maker=True) == D("0.4375")  # half, times the 0.5 multiplier
    assert combo.model_fee(D(100), D("0.50"), maker=False) == D("0.875")
    waived = KalshiFeeSchedule(fee_waiver_until_ns=1_000)
    assert waived.model_fee(D(5), D("0.5"), maker=False, ts_ns=999) == 0
    assert waived.model_fee(D(5), D("0.5"), maker=False, ts_ns=1_000) == D("0.0875")
    flat = KalshiFeeSchedule("flat", flat_taker=D("0.02"), flat_maker=D("0.005"))
    assert flat.model_fee(D(10), D("0.9"), maker=False) == D("0.20")
    assert flat.model_fee(D(10), D("0.9"), maker=True) == D("0.050")


@pytest.mark.parametrize(
    ("kwargs", "msg"),
    [
        ({"fee_type": "linear"}, "fee_type"),
        ({"fee_multiplier": D(-1)}, "negative"),
        ({"fee_type": "flat"}, "Specific Trading Fees Table"),
        ({"fee_type": "flat", "flat_taker": D("0.01")}, "flat_maker"),
    ],
)
def test_kalshi_schedule_refuses_bad_configs(kwargs: dict, msg: str) -> None:
    with pytest.raises(QtError) as e:
        KalshiFeeSchedule(**kwargs)
    assert e.value.code == "QT-CONFIG-001" and msg in e.value.message


def test_kalshi_model_fee_input_checks() -> None:
    with pytest.raises(QtError, match="outside"):
        KalshiFeeSchedule().model_fee(D(1), D("1.2"), maker=False)
    with pytest.raises(QtError, match="negative"):
        KalshiFeeSchedule().model_fee(D(-1), D("0.2"), maker=False)
    with pytest.raises(QtError, match="member"):
        KalshiFeeAccumulator("retail")  # type: ignore[arg-type]
    with pytest.raises(QtError, match="negative"):
        KalshiFeeAccumulator().fill(D(-1), D(-1))


def test_kalshi_fee_rounding_worked_example_fcm_cleared_fill() -> None:
    """docs.kalshi.com fee rounding: a non-direct buy with -$0.055 revenue and a $0.00363825 model fee."""
    f = KalshiFeeAccumulator("non_direct").fill(D("-0.055000"), D("0.00363825"))
    assert f.trade_fee == D("0.003639")
    assert f.balance_change == D("-0.06")
    assert f.rounding_fee == D("0.001361")
    assert f.trade_fee + f.rounding_fee == D("0.005") and f.rebate == 0 and f.net_fee == D("0.005")


def test_kalshi_fee_accumulator_worked_example_rebates_on_the_third_fill() -> None:
    """The documented table: three fills each add $0.004 of rounding; the third rebates $0.01, $0.002 carries."""
    acc = KalshiFeeAccumulator("non_direct")
    a = acc.fill(D("-0.056"), D(0))
    b = acc.fill(D("-0.056"), D(0))
    assert (a.rounding_fee, a.rebate, b.rounding_fee, b.rebate) == (D("0.004"), 0, D("0.004"), 0)
    assert acc.accumulated == D("0.008")
    c = acc.fill(D("-0.048"), D("0.008"))  # enough total fee (0.012) to cover the rebate
    assert (c.rounding_fee, c.rebate, acc.accumulated) == (D("0.004"), D("0.01"), D("0.002"))
    assert c.net_fee == D("0.002") and c.net_fee >= 0


def test_kalshi_rebate_is_capped_so_the_net_fee_is_never_negative() -> None:
    acc = KalshiFeeAccumulator("non_direct", accumulated=D("0.05"))
    f = acc.fill(D("-0.50"), D("0.003"))  # trade fee 0.003, rounding 0.007: at most 0.01 back
    assert f.rebate == D("0.01") and f.net_fee == 0 and acc.accumulated == D("0.047")


# The fee schedule filed with the CFTC on 2022-09-09: "General Trading Fees Table" (fee for 1 and for 100
# contracts) and the S&P 500 / Nasdaq-100 "Specific Trading Fees Table" at 0.035, i.e. fee_multiplier 0.5
GENERAL = {
    "0.01": ("0.01", "0.07"), "0.05": ("0.01", "0.34"), "0.10": ("0.01", "0.63"), "0.15": ("0.01", "0.90"),
    "0.20": ("0.02", "1.12"), "0.25": ("0.02", "1.32"), "0.30": ("0.02", "1.47"), "0.35": ("0.02", "1.60"),
    "0.40": ("0.02", "1.68"), "0.45": ("0.02", "1.74"), "0.50": ("0.02", "1.75"), "0.55": ("0.02", "1.74"),
    "0.60": ("0.02", "1.68"), "0.65": ("0.02", "1.60"), "0.70": ("0.02", "1.47"), "0.75": ("0.02", "1.32"),
    "0.80": ("0.02", "1.12"), "0.85": ("0.01", "0.90"), "0.90": ("0.01", "0.63"), "0.95": ("0.01", "0.34"),
    "0.99": ("0.01", "0.07"),
}  # fmt: skip
INDEX_100 = {
    "0.01": "0.04", "0.05": "0.17", "0.10": "0.32", "0.15": "0.45", "0.20": "0.56", "0.25": "0.66", "0.30": "0.74",
    "0.35": "0.80", "0.40": "0.84", "0.45": "0.87", "0.50": "0.88", "0.55": "0.87", "0.60": "0.84", "0.65": "0.80",
    "0.70": "0.74", "0.75": "0.66", "0.80": "0.56", "0.85": "0.45", "0.90": "0.32", "0.95": "0.17", "0.99": "0.04",
}  # fmt: skip


def test_kalshi_fee_schedule_tables_through_the_rounding_path() -> None:
    """A whole-cent taker fill of whole contracts by a non-direct ($0.01) member pays the filed tables' fees."""

    def paid(sched: KalshiFeeSchedule, c: int, p: str) -> Decimal:
        model = sched.model_fee(D(c), D(p), maker=False)
        return KalshiFeeAccumulator("non_direct").fill(-D(c) * D(p), model).net_fee

    general, index = KalshiFeeSchedule(), KalshiFeeSchedule(fee_multiplier=D("0.5"))
    for p, (one, hundred) in GENERAL.items():
        assert (paid(general, 1, p), paid(general, 100, p)) == (D(one), D(hundred)), p
        assert paid(index, 1, p) == D("0.01") and paid(index, 100, p) == D(INDEX_100[p]), p


def test_kalshi_non_direct_single_contract_pays_the_old_round_up_to_a_cent() -> None:
    """With whole contracts at whole-cent prices a non-direct member pays ceil_cent(model fee) per fill."""
    for c, p in [(1, "0.50"), (3, "0.07"), (10, "0.33"), (7, "0.99")]:
        model = KalshiFeeSchedule().model_fee(D(c), D(p), maker=False)
        f = KalshiFeeAccumulator("non_direct").fill(-D(c) * D(p), model)
        assert f.net_fee == (model / D("0.01")).to_integral_value(rounding="ROUND_CEILING") * D("0.01")
    one = KalshiFeeAccumulator("non_direct").fill(D("-0.50"), D("0.0175"))
    assert one.net_fee == D("0.02")
    direct = KalshiFeeAccumulator("direct").fill(D("-0.50"), D("0.0175"))
    assert direct.net_fee == D("0.0175") and direct.balance_change == D("-0.5175")


# ---- Polymarket -----------------------------------------------------------------------------------------------------

# docs.polymarket.com/trading/fees "Fee Tables (100 Shares)": price -> taker fee shown to the cent
CRYPTO_100 = {
    "0.01": "0.07", "0.05": "0.33", "0.10": "0.63", "0.15": "0.89", "0.20": "1.12", "0.25": "1.31",
    "0.30": "1.47", "0.35": "1.59", "0.40": "1.68", "0.45": "1.73", "0.50": "1.75", "0.55": "1.73",
    "0.60": "1.68", "0.65": "1.59", "0.70": "1.47", "0.75": "1.31", "0.80": "1.12", "0.85": "0.89",
    "0.90": "0.63", "0.95": "0.33", "0.99": "0.07",
}  # fmt: skip
SPORTS_100 = {"0.01": "0.05", "0.05": "0.24", "0.10": "0.45", "0.20": "0.80", "0.50": "1.25", "0.60": "1.20"}


@pytest.mark.parametrize(("category", "table"), [("crypto", CRYPTO_100), ("Sports", SPORTS_100)])
def test_polymarket_fee_tables_of_the_docs(category: str, table: dict[str, str]) -> None:
    s = PolymarketFeeSchedule.for_category(category)
    for p, shown in table.items():
        fee = s.taker_fee(D(100), D(p))
        assert fee.quantize(D("0.01"), rounding=ROUND_HALF_UP) == D(shown), p
        assert s.maker_fee(D(100), D(p)) == 0  # "Makers are never charged fees"


def test_polymarket_fee_precision_rounds_down_to_five_decimals() -> None:
    s = PolymarketFeeSchedule(rate=D("0.07"))
    assert s.taker_fee(D(100), D("0.01")) == D("0.06930")
    assert s.taker_fee(D(1), D("0.01")) == D("0.00069")  # 0.000693
    assert s.taker_fee(D("0.1"), D("0.01")) == D("0.00006")  # 0.0000693
    assert s.taker_fee(D("0.01"), D("0.01")) == 0  # 0.00000693: "anything smaller rounds to zero"
    assert s.fee_equivalent(D("0.01"), D("0.01")) == D("0.00000693")


def test_polymarket_categories_exponent_and_maker_fees() -> None:
    assert set(POLY_CATEGORY_RATES) >= {"crypto", "sports", "politics", "geopolitics", "other"}
    assert PolymarketFeeSchedule.for_category("Other / General").rate == D("0.05")
    assert PolymarketFeeSchedule.for_category("general").rebate_rate == D("0.25")
    geo = PolymarketFeeSchedule.for_category("geopolitics")
    assert not geo.fees_enabled and geo.taker_fee(D(100), D("0.5")) == 0
    with pytest.raises(QtError) as e:
        PolymarketFeeSchedule.for_category("astrology")
    assert e.value.code == "QT-CONFIG-001"
    sq = PolymarketFeeSchedule(rate=D("0.04"), exponent=D(2))
    assert not sq.documented and sq.fee_equivalent(D(100), D("0.5")) == D("0.2500")  # 100 x 0.04 x 0.25^2
    both = PolymarketFeeSchedule(rate=D("0.04"), taker_only=False)
    assert both.maker_fee(D(100), D("0.5")) == D("1.00000")
    off = PolymarketFeeSchedule(fees_enabled=False, rate=D("0.04"))
    assert off.taker_fee(D(100), D("0.5")) == 0
    for bad in ({"rate": D(-1)}, {"rebate_rate": D("1.5")}, {"exponent": D(0)}):
        with pytest.raises(QtError):
            PolymarketFeeSchedule(**bad)


def test_polymarket_maker_rebate_share_of_the_pool() -> None:
    assert maker_rebate(D(1), D(4), D(100)) == D(25)
    assert maker_rebate(D(0), D(0), D(100)) == 0  # no maker fee equivalent in the market
    with pytest.raises(QtError, match="exceeds"):
        maker_rebate(D(5), D(4), D(1))
    with pytest.raises(QtError, match="non-negative"):
        maker_rebate(D(-1), D(4), D(1))
