"""A learned competing-risks hazard model on shadow features, compared with zoo models on the same test rows.

Shows: `qt.extract_features` (landmark features of the shared shadow sample, `qt.features`), `qt.run_zoo` on the
same shadows for the comparison, `qt.fit_hazard` (`HazardFitSpec` with a smaller GBDT than the default so the
example stays fast; split `auto` = train/calib on 2026-06-15, test on 2026-06-16 for this 2-day dataset), the
returned `qt.schemas.HazardModelCard` (split, test scores, `zoo_comparison`), and `qt.HazardModel.load(...).predict`
on the feature table (`qt.hazard_predictions`).

Needs the compiled extension (`make develop` or the wheel). Synthetic data only: the numbers mean nothing about
real venues.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.compute as pc
from _data import dataset_path, fmt, horizon_label, out_dir, print_rows, sample_example_shadows, table_by_schema

import queuetruth as qt
from queuetruth.schemas import HazardModelCard

#: Zoo models compared with the learned model.
ZOO_MODELS = ["hbt_power3[n=2]", "hbt_l3_fifo", "trade_through_only"]
#: Smaller than the defaults (max_iter 300, bootstrap_b 1000) to keep the run short.
GBDT = {"max_iter": 100}
BOOTSTRAP_B = 200


def fit(dataset: Path, out: Path, shadows: Path) -> HazardModelCard:
    """Extracts features, runs the comparison zoo and fits the hazard model (outputs under `out`)."""
    f = qt.extract_features(
        {"dataset": {"path": str(dataset)}, "out": str(out / "features"), "shadows": {"path": str(shadows)}}
    )
    c = f.by_cause
    print(
        f"features: {f.n_orders} orders, {f.n_rows} landmark rows (fill {c.fill}, cancel {c.cancel}, "
        f"censor {c.censor}); {len(f.feature_names)} features, {len(f.privileged_names)} privileged"
    )
    features = table_by_schema(out / "features", f.files, "qt.features")
    z = qt.run_zoo(
        {
            "dataset": {"path": str(dataset)},
            "shadows": {"path": str(shadows)},
            "out": str(out / "zoo"),
            "models": ZOO_MODELS,
            "bootstrap_b": BOOTSTRAP_B,
        }
    )
    preds = table_by_schema(out / "zoo", z.files, "qt.zoo_predictions")
    return qt.fit_hazard(
        {
            "features": {"path": str(features)},
            "out": str(out / "model"),
            "gbdt": GBDT,
            "bootstrap_b": BOOTSTRAP_B,
            "zoo_predictions": {"path": str(preds)},
        }
    )


def show(card: HazardModelCard) -> None:
    """Prints the split, the test scores and the zoo comparison."""
    print(f"\nmodel {card.model_id} (recalibration {card.recalibration}, privileged features {card.privileged})")
    sp, n = card.split, card.n_orders
    print(f"  split {sp.scheme}: train {sp.train}, calib {sp.calib}, test {sp.test}")
    print(f"  orders: train {n.train}, calib {n.calib}, test {n.test}; person-period rows (train) {card.n_rows_train}")
    sc = card.scores_test
    print(f"  test: rcll {fmt(sc.rcll)}, ibs {fmt(sc.ibs)}, c_index_uno {fmt(sc.c_index_uno)} (discrimination only)")
    print(f"  {'horizon':>8}  {'brier':>7}  {'ipcw_ll':>7}  {'bss_vs_clim [95% CI]':>28}  zoo brier")
    for h in card.grid_ns:
        k = str(h)
        bss = sc.bss_vs_climatology.get(k)
        bss_s = "n/a" if bss is None else f"{fmt(bss.value, 3)} [{fmt(bss.ci_lo, 3)}, {fmt(bss.ci_hi, 3)}]"
        zoo = ""
        for model_id, zb in (card.zoo_comparison or {}).items():
            if k in zb.brier:
                zoo += f"  {model_id.split('[')[0].split(':')[0]} {fmt(zb.brier[k])}"
        print(f"  {horizon_label(h):>8}  {fmt(sc.brier.get(k)):>7}  {fmt(sc.ipcw_logloss.get(k)):>7}  {bss_s:>28}{zoo}")
    for w in card.warnings:
        print(f"  warning: {w}")


def predict(out: Path, card: HazardModelCard) -> None:
    """Loads the saved model and predicts the fill and cancel CIFs for the first feature row. `load` checks `model.npz`
    against the trusted sha256 `fit_hazard` recorded in the card it returned (no code runs when loading)."""
    sha = qt.model.model_sha256(card)
    model = qt.HazardModel.load(str(out / "model"), sha256=sha)
    feats = qt.read_table(str(next((out / "features").glob("*.parquet"))), expect="qt.features")
    one = feats.filter(pc.equal(feats.column("order_ref"), feats.column("order_ref")[0])).slice(0, 1)
    p = model.predict(one)
    print(f"\nHazardModel.load(...).predict: {p.num_rows} rows ({len(model.grid_ns)} horizons) for one landmark row")
    cols = [c for c in ("order_ref", "landmark_ns", "horizon_ns", "cif_fill", "cif_cancel") if c in p.column_names]
    print_rows(p, cols, n=len(model.grid_ns))


def main() -> None:
    """Samples shadows, fits the hazard model, prints the card and one prediction."""
    dataset = dataset_path()
    out = out_dir(__file__)
    print(f"queuetruth {qt.__version__}; dataset {dataset}; outputs {out}")
    shadows = sample_example_shadows(dataset, out)
    card = fit(dataset, out, shadows)
    show(card)
    predict(out, card)


if __name__ == "__main__":
    main()
