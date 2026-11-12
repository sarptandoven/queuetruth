"""`qt.fit`: the parameter that fixes a backtester's fill model, with its verdict.

`fit(backtester, data=...)` picks the calibration that already exists for the backtester and the input and runs it
with a train and a held-out window:

- order-level data (`data`, optional `shadows`): `calibrate alloc` or `calibrate nt-touch`, against exact queue
  truth;
- live fills (`live` and its market data `data`): `calibrate-live`, against the user's own fills.

The families (`FAMILIES`, the default first): hftbacktest `power3`, `power2`, `power`; nautilus `nt_prob_touch`,
`nt_queue_l2_prob_touch`; databento `databento`.

Without `shadows` it samples them from the dataset (`qt.sample_shadows`, seeded). The windows come from
`queuetruth.fit.split` (`split="70%"`, `test_after="09:42"`, or expert `train` / `test`). The verdict is the export's
own fail-safe assessment (`queuetruth.export.trust.assess`): the fit must have enough events, a usable interval and
beat the library default on the held-out window. Only then is the paste-ready snippet written (`queuetruth.export`;
for Databento a short constant with the same header). A refused fit says why and what would make it pass, with how
much more data it needs where that is the reason.

Files in `out`: `fit.json` (the `qt.fit` document), `calibration/` (the calibration run, `calibration.json`),
`shadows.parquet` (when sampled) and `<backtester>.py` (only when the fit passes).
"""

from __future__ import annotations

import json
import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from queuetruth.errors import QtError
from queuetruth.fit import split as sp
from queuetruth.schemas.models import CalibrationResult
from queuetruth.schemas.specs import WindowSpec
from queuetruth.score._dataset import DatasetView

BACKTESTERS = ("hftbacktest", "nautilus", "databento")
#: Families each backtester can use, the default first.
FAMILIES: dict[str, tuple[str, ...]] = {
    "hftbacktest": ("power3", "power2", "power"),
    "nautilus": ("nt_prob_touch", "nt_queue_l2_prob_touch"),
    "databento": ("databento",),
}
#: `calibrate` kind of each family (order-level data).
KIND = {
    "power": "alloc",
    "power2": "alloc",
    "power3": "alloc",
    "databento": "alloc",
    "nt_prob_touch": "nt_touch",
    "nt_queue_l2_prob_touch": "nt_touch",
}
#: The library's own name of the parameter.
SETTING = {
    "power": "hftbacktest power_prob_queue_model",
    "power2": "hftbacktest power_prob_queue_model2",
    "power3": "hftbacktest power_prob_queue_model3",
    "databento": "Databento's queue estimator",
    "nt_prob_touch": "NautilusTrader prob_fill_on_limit",
    "nt_queue_l2_prob_touch": "NautilusTrader prob_fill_on_limit (queue_position=True)",
}
#: Fewest held-out orders below which a refusal suggests a longer held-out window.
FEW_TEST_ORDERS = 100
#: Bootstrap blocks the calibration interval needs (`queuetruth.stats`: days with 5 or more dates, else half-hours).
MIN_BLOCKS = 5
HALF_HOUR_NS = 1_800_000_000_000
DEFAULT_N_SHADOWS = 1000


@dataclass
class FitResult:
    """The answer of `fit` and of `qt.atlas.calibration`: `summary` is the one-line answer, `doc` the `qt.fit`
    document, `calibration` the `qt.calibration_result` behind it (None for a setting read from the atlas),
    `snippet` the paste-ready text (None when the fit is refused).

    `doc["source"]` says where it came from: `order_level` or `live` (a fit on your data) or `atlas` (the atlas's
    closest setting, with `doc["atlas"]` naming the atlas root, tier, horizon and days). `doc["settings"]` holds the
    library settings to apply, in the library's own names; `queuetruth_nautilus.venue_options` and
    `queuetruth_hftbacktest.configure` read them."""

    doc: dict[str, Any]
    calibration: CalibrationResult | None
    out: Path | None = None

    @property
    def summary(self) -> str:
        """The answer in plain words."""
        return str(self.doc["answer"])

    @property
    def passed(self) -> bool:
        """True when the fit beats the default on held-out data and the snippet was written."""
        return bool(self.doc["verdict"]["passed"])

    @property
    def source(self) -> str:
        """`order_level`, `live` or `atlas`."""
        return str(self.doc["source"])

    @property
    def settings(self) -> dict[str, Any]:
        """The library settings to apply (`doc["settings"]`)."""
        if "settings" in self.doc:
            return dict(self.doc["settings"])
        return settings_of(self.doc["backtester"], self.doc["family"], self.doc["param"]["value"])

    @property
    def snippet(self) -> str | None:
        """The paste-ready snippet, or None when the fit is refused."""
        sn = self.doc.get("snippet")
        return None if sn is None else str(sn["text"])

    def to_dict(self) -> dict[str, Any]:
        """The `qt.fit` document (JSON-ready)."""
        return self.doc

    def save(self, path: str | Path) -> Path:
        """Writes the `qt.fit` document to `path` (for a framework in another venv: `qthbt.configure(asset, path)`,
        `qtn.load_calibration(path)`); returns the path."""
        p = Path(path)
        p.write_text(json.dumps(self.doc, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        return p

    def text(self) -> str:
        """The terminal report: the answer first, then the held-out table, the verdict and the snippet."""
        from queuetruth.fit.render import text

        return text(self.doc)

    def __str__(self) -> str:
        return self.text()


# ---- inputs -------------------------------------------------------------------------------------------------------


def _family(backtester: str, family: str | None) -> str:
    if backtester not in BACKTESTERS:
        raise QtError("QT-USAGE-001", f"backtester: expected one of {', '.join(BACKTESTERS)}, got {backtester!r}")
    fams = FAMILIES[backtester]
    fam = (family or fams[0]).replace("-", "_").removeprefix("hbt_")
    if fam not in fams:
        raise QtError(
            "QT-USAGE-001",
            f"family {family!r} is not a {backtester} fill model; use one of {', '.join(fams)}",
        )
    return fam


def _shadow_orders(path: Path) -> list[sp.Order]:
    t = pq.read_table(path, columns=["instrument_id", "ts_submit", "latency_ns"])
    iids = t.column("instrument_id").to_pylist()
    ts = t.column("ts_submit").cast("int64").to_pylist()
    lat = t.column("latency_ns").to_pylist()
    return [sp.Order(int(i), int(s) + int(x or 0)) for i, s, x in zip(iids, ts, lat, strict=True)]


def _live_orders(live: Path) -> list[sp.Order]:
    """Resting limit orders of a canonical live log at their exchange ack (the `calibrate-live` entry)."""
    from queuetruth.score._io import read_live_log

    orders, events = read_live_log(live)
    ack: dict[str, int] = {}
    for e in events:
        if e.get("event_type") == "ack":
            ts = e.get("ts_exchange") or e.get("ts_local_recv")
            k = str(e["order_key"])
            if ts is not None and k not in ack:
                ack[k] = int(ts)
    out = []
    for o in orders:
        k = str(o["order_key"])
        if o.get("order_type") == "limit" and o.get("tif") not in ("ioc", "fok") and k in ack:
            iid = o.get("instrument_id")
            out.append(sp.Order(None if iid is None else int(iid), ack[k]))
    return out


def _windows(
    ds: DatasetView,
    orders: Sequence[sp.Order],
    *,
    split: str | float | None,
    test_after: str | None,
    train: Any,
    test: Any,
    what: str,
) -> sp.Split:
    if train is not None or test is not None:
        if train is None or test is None or split is not None or test_after is not None:
            raise QtError(
                "QT-USAGE-001",
                "give both --train and --test (expert windows), or one of --split / --test-after",
            )
        tr = sp.window_arg(train, "--train") if isinstance(train, str) else WindowSpec.model_validate(train)
        te = sp.window_arg(test, "--test") if isinstance(test, str) else WindowSpec.model_validate(test)
        return sp.given(ds, orders, tr, te)
    return sp.plan(ds, orders, split=split, test_after=test_after, what=what)


def _too_little(s: sp.Split, n: int, what: str) -> QtError | None:
    """`QT-DATA-001` when a window holds no order, saying how many more are needed."""
    if s.n_train and s.n_test:
        return None
    side, other = ("held-out", "train") if not s.n_test else ("train", "held-out")
    return QtError(
        "QT-DATA-001",
        f"the {side} window ({s.test_text if not s.n_test else s.train_text}) holds no {what} of the {n} "
        f"({s.how}); a verdict needs {what} on both sides of the cut",
        f"move the cut (--split 50%, --test-after HH:MM) or add data: at least one more {what[:-1]} in the {side} "
        f"window, and about as many there as in the {other} window for a usable comparison",
    )


# ---- the run ------------------------------------------------------------------------------------------------------


def fit(
    backtester: str,
    *,
    data: str | Path | None = None,
    shadows: str | Path | None = None,
    live: str | Path | None = None,
    family: str | None = None,
    split: str | float | None = None,
    test_after: str | None = None,
    train: Any = None,
    test: Any = None,
    out: str | Path = "qt-fit",
    n_shadows: int = DEFAULT_N_SHADOWS,
    seed: int = 0,
    threads: int = 1,
) -> FitResult:
    """Fits `backtester`'s fill-model parameter on `data` (order-level, exact queue truth) or on the live fills in
    `live` (with its market data `data`), compares it with the library default on held-out data and writes the
    paste-ready snippet only when it passes. `split` (`"70%"`, the default) or `test_after` (`"09:42"`, venue-local)
    choose the held-out window; `train` / `test` (dates or `blocks:I-J/N`) are the expert form."""
    from queuetruth.ops.calibrate import calibrate, calibrate_live
    from queuetruth.ops.shadows import sample_shadows

    fam = _family(backtester, family)
    if data is None:
        if live is not None:
            raise QtError(
                "QT-USAGE-001",
                "fitting to live fills needs the market data of the same period (--data DATASET, L2 or MBO)",
                "queuetruth calibrate latency --live LOG fits the latency alone",
            )
        raise QtError("QT-USAGE-001", "fit needs --data DATASET (order-level data), or --live LOG with --data")
    if live is not None and shadows is not None:
        raise QtError("QT-USAGE-001", "--shadows applies to order-level fits; a live fit uses the live orders")
    outp = Path(out)
    outp.mkdir(parents=True, exist_ok=True)
    ds = DatasetView(data)
    cal_dir = outp / "calibration"
    sampled: Path | None = None
    if live is not None:
        what = "live orders"
        orders = _live_orders(Path(live))
        if not orders:
            raise QtError(
                "QT-INPUT-002",
                f"{live}: no acked resting limit order to fit on",
                "fit needs the live log's limit orders with their exchange acks",
            )
    else:
        what = "shadows"
        if shadows is None:
            sampled = outp / "shadows.parquet"
            sample_shadows({"dataset": {"path": str(data)}, "n": int(n_shadows), "seed": seed, "out": str(sampled)})
            shadows = sampled
        orders = _shadow_orders(Path(shadows))
    s = _windows(ds, orders, split=split, test_after=test_after, train=train, test=test, what=what)
    err = _too_little(s, len(orders), what)
    if err is not None:
        raise err
    windows = {"train": s.train.model_dump(mode="json"), "test": s.test.model_dump(mode="json")}
    if live is not None:
        command = "calibrate-live"
        cal = calibrate_live(
            {
                "dataset": {"path": str(data)},
                "live": {"path": str(live)},
                "family": fam,
                "out": str(cal_dir),
                "seed": seed,
                "threads": threads,
                **windows,
            }
        )
    else:
        command = f"calibrate {KIND[fam].replace('_', '-')}"
        cal = calibrate(
            {
                "kind": KIND[fam],
                "family": fam,
                "dataset": {"path": str(data)},
                "shadows": {"path": str(shadows)},
                "out": str(cal_dir),
                "seed": seed,
                "threads": threads,
                **windows,
            }
        )
    doc = _document(backtester, fam, command, cal, s, ds, orders, what, live is not None)
    doc["files"] = {
        "fit": str(outp / "fit.json"),
        "calibration": str(cal_dir / "calibration.json"),
        "shadows": None if sampled is None else str(sampled),
        "snippet": None,
    }
    if doc["verdict"]["passed"]:
        doc["snippet"] = _write_snippet(backtester, cal, cal_dir, outp)
        doc["files"]["snippet"] = doc["snippet"]["path"]
    from queuetruth.fit.render import answer

    doc["answer"] = answer(doc)
    (outp / "fit.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    return FitResult(doc, cal, outp)


# ---- the document -------------------------------------------------------------------------------------------------


def _finite(x: Any) -> float | None:
    return float(x) if isinstance(x, int | float) and math.isfinite(x) else None


def _value(fam: str, cal: CalibrationResult, x: float | None) -> float | None:
    """A calibration number in the library's units (`n`, `k` or `p`)."""
    from queuetruth.export._text import LOG_N_NAMES

    if x is None:
        return None
    if fam.startswith("power") and (cal.param_name or "").lower() in LOG_N_NAMES:
        return math.exp(x)
    return x


def _interval(fam: str, cal: CalibrationResult) -> tuple[list[float | None] | None, str | None]:
    for kind, ci in (("bootstrap", cal.ci_bootstrap), ("profile", cal.ci_profile)):
        if ci is not None and (_finite(ci[0]) is not None or _finite(ci[1]) is not None):
            return [_value(fam, cal, _finite(ci[0])), _value(fam, cal, _finite(ci[1]))], kind
    return None, None


def _held_out_rows(cal: CalibrationResult, labels: dict[int, str]) -> list[dict[str, Any]]:
    from queuetruth.export.trust import dur

    fho = next(iter(cal.held_out.values()), None)
    dho = next(iter(cal.baseline_held_out.values()), None)
    if fho is None or dho is None:
        return []
    keys = sorted({int(k) for k in (*fho.logloss, *dho.logloss, *fho.fill_rate_bias, *dho.fill_rate_bias)})
    return [
        {
            "horizon": dur(h),
            "horizon_ns": h,
            "fitted_bias_pp": None if (v := _finite(fho.fill_rate_bias.get(str(h)))) is None else v * 100,
            "default_bias_pp": None if (v := _finite(dho.fill_rate_bias.get(str(h)))) is None else v * 100,
            "fitted_logloss": _finite(fho.logloss.get(str(h))),
            "default_logloss": _finite(dho.logloss.get(str(h))),
            "result": labels.get(h, "n/a"),
        }
        for h in keys
    ]


def _half_hours(ds: DatasetView, orders: Sequence[sp.Order], w: WindowSpec) -> tuple[int, int]:
    """(session dates, UTC half-hours) holding orders of the window `w` (the calibration's bootstrap blocks)."""
    inside = [o for o in orders if sp.inside(ds, o, w)]
    return len({ds.day(o.instrument_id, o.ts)[0] for o in inside}), len({o.ts // HALF_HOUR_NS for o in inside})


def _remedies(
    reasons: list[tuple[str, str]],
    cal: CalibrationResult,
    s: sp.Split,
    ds: DatasetView,
    orders: Sequence[sp.Order],
    what: str,
    fam: str,
    backtester: str,
) -> list[str]:
    """What would make a refused fit pass, with how much more data where that is the reason."""
    out: list[str] = []
    codes = {k for k, _ in reasons}
    if codes & {"no_estimate", "few_events"}:
        n = int(cal.n_events or 0)
        if n:
            more = max(math.ceil(s.n_train * (30 / n - 1)), 1)
            out.append(
                f"More train data: the fit saw {n} informative events, at least 30 are needed. That is about "
                f"{30 / n:.1f} times the train window: roughly {more} more {what} in it, or a longer capture."
            )
        else:
            out.append(
                f"More train data: no {what[:-1]} in the train window gave the fit any information (no queue event "
                "at its level, or no touch). Use a longer capture or more active instruments."
            )
    if codes & {"degenerate_ci", "flat_likelihood", "not_identified"}:
        days, halves = _half_hours(ds, orders, s.train)
        if days >= MIN_BLOCKS:
            out.append(
                f"More train data: the {days} train days do not pin the value down; add days until the interval "
                "narrows."
            )
        elif halves < MIN_BLOCKS:
            need = MIN_BLOCKS - halves
            out.append(
                f"More train data: the interval is a bootstrap over half-hours of data and the train window covers "
                f"{halves}; it needs at least {MIN_BLOCKS}: about {need * 30} more minutes of data, or "
                f"{MIN_BLOCKS} trading days."
            )
        else:
            out.append(
                f"More train data: {halves} half-hours of data do not pin the value down; add data (a day or "
                f"more) until the interval narrows."
            )
    if "at_grid_edge" in codes:
        others = [f for f in FAMILIES[backtester] if f != fam]
        alt = f" Try --family {' or '.join(others)}." if others else ""
        out.append(
            "The best value is at the end of the range the fit searches: this fill model cannot match the data "
            f"within it.{alt}"
        )
    if "own_orders_in_depth" in codes:
        out.append(
            "Your own resting orders are still in the depth the fit ran on: use MBO data of the period, or a live "
            "log whose orders match the tape (calibrate-live removes them when it can)."
        )
    if "no_held_out" in codes:
        out.append(f"No held-out {what[:-1]} was scored: hold out more data (--split 60%) or add data after the cut.")
    if "not_beating_default" in codes:
        if s.n_test < FEW_TEST_ORDERS:
            out.append(
                f"The held-out window has {s.n_test} {what}; with fewer than {FEW_TEST_ORDERS} a small gain cannot "
                f"be told from noise. Add data after the cut (about {FEW_TEST_ORDERS - s.n_test} more {what}) and "
                "fit again."
            )
        else:
            out.append(
                "On this data the library default is as good as the fitted value: keep the default. A fit on a "
                "different period, venue or --family may differ."
            )
    return out


def _document(
    backtester: str,
    fam: str,
    command: str,
    cal: CalibrationResult,
    s: sp.Split,
    ds: DatasetView,
    orders: Sequence[sp.Order],
    what: str,
    is_live: bool,
) -> dict[str, Any]:
    from queuetruth.export.trust import assess
    from queuetruth.score.calibrate_live import DEFAULTS, PARAM

    t = assess(cal)
    ci, ci_kind = _interval(fam, cal)
    labels = {h.horizon_ns: h.label for h in t.horizons}
    return {
        "schema": "qt.fit",
        "answer": "",
        "backtester": backtester,
        "family": fam,
        "source": "live" if is_live else "order_level",
        "command": command,
        "run_id": cal.run_id,
        "param": {
            "name": PARAM[fam],
            "setting": SETTING[fam],
            "value": _value(fam, cal, _finite(cal.estimate)),
            "ci": ci,
            "ci_kind": ci_kind,
            "default": DEFAULTS[fam],
            "n_events": int(cal.n_events or 0),
        },
        "split": {
            "how": s.how,
            "train": s.train_text,
            "test": s.test_text,
            "n_train": s.n_train,
            "n_test": s.n_test,
            "orders": what,
            "train_window": s.train.model_dump(mode="json", exclude_none=True),
            "test_window": s.test.model_dump(mode="json", exclude_none=True),
        },
        "held_out": _held_out_rows(cal, labels),
        "verdict": {
            "passed": t.ok,
            "reasons": [{"code": k, "text": v} for k, v in t.reasons],
            "remedies": [] if t.ok else _remedies(t.reasons, cal, s, ds, orders, what, fam, backtester),
        },
        "snippet": None,
        "settings": settings_of(backtester, fam, _value(fam, cal, _finite(cal.estimate))),
    }


def settings_of(backtester: str, family: str, value: float | None) -> dict[str, Any]:
    """The library settings of a fill-model family at `value`, in the library's own names:

    - nautilus: `prob_fill_on_limit`, `queue_position` (True for the queue-gated family), `book_type` and
      `trade_execution` (the venue settings the fit and the atlas ran under);
    - hftbacktest: `queue_model` (the `BacktestAsset` builder), `queue_model_args` and `exchange` (the export's
      default, the one that runs on the released hftbacktest 2.4.4);
    - databento: `k` of the queue-position estimator.

    Empty when the family has no value to set."""
    from queuetruth.export.hftbacktest import DEFAULT_EXCHANGE, EXCHANGES, FIXED_BUILDERS, POWER_BUILDERS

    if backtester == "nautilus":
        if value is None:
            return {}
        return {
            "prob_fill_on_limit": float(value),
            "queue_position": family != "nt_prob_touch",
            "book_type": "L2_MBP",
            "trade_execution": True,
        }
    if backtester == "hftbacktest":
        if family in POWER_BUILDERS:
            if value is None:
                return {}
            builder, args = POWER_BUILDERS[family], [float(value)]
        elif family in FIXED_BUILDERS:
            builder, args = FIXED_BUILDERS[family], []
        else:
            return {}
        return {"queue_model": builder, "queue_model_args": args, "exchange": EXCHANGES[DEFAULT_EXCHANGE]}
    if backtester == "databento":
        return {} if value is None else {"k": float(value)}
    return {}


# ---- the snippet --------------------------------------------------------------------------------------------------

NAUTILUS_ONE_LINER = (
    "engine.add_venue(..., **qtn.venue_options(qtn.load_calibration({path})))  "
    "# import queuetruth_nautilus as qtn (integrations/nautilus)"
)


def databento_snippet(cal: CalibrationResult) -> str:
    """The fitted `k` of Databento's queue-position estimator as a constant, with the export's header lines."""
    from queuetruth.export import trust
    from queuetruth.export._text import VERSION, data_sources_text, g6, window_text

    k = float(cal.estimate)  # type: ignore[arg-type]
    return "\n".join(
        [
            f"# queuetruth {VERSION} | calibration {cal.run_id} | {cal.family} fitted on {window_text(cal.train)}; "
            f"held-out {window_text(cal.test)}",
            f"# data: {data_sources_text(cal)}",
            *trust.header_lines(cal, f"k = {g6(k)}"),
            "# Databento's queue-position estimator: a depth decrease is taken from the front of the queue with",
            "# probability x ** (1 / (1 + k)) for k <= 0, 1 - (1 - x) ** (1 / (1 - k)) for k > 0, x = your share",
            "# of the level ahead of you (queuetruth docs: The L2 model zoo, databento_mc).",
            f"QUEUE_K = {g6(k)}",
            "",
        ]
    )


def _write_snippet(backtester: str, cal: CalibrationResult, cal_dir: Path, out: Path) -> dict[str, Any]:
    """Writes `<out>/<backtester>.py` (only for a passing fit) with `queuetruth.export`; returns the snippet entry."""
    path = out / f"{backtester}.py"
    notes: list[str] = []
    one_liner = None
    if backtester == "databento":
        text = databento_snippet(cal)
        path.write_text(text, encoding="utf-8")
    else:
        from queuetruth.export import run_export

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            text = str(run_export({"target": backtester, "calibration": {"path": str(cal_dir)}, "out": str(path)}).text)
        notes = [str(w.message) for w in caught]
        if backtester == "nautilus":
            one_liner = NAUTILUS_ONE_LINER.format(path=json.dumps(str(cal_dir / "calibration.json")))
    return {"path": str(path), "text": text, "notes": notes, "one_liner": one_liner}


__all__ = ["BACKTESTERS", "FAMILIES", "FitResult", "databento_snippet", "fit", "settings_of"]
