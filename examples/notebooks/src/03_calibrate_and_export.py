# %% [markdown]
# # Calibrate a queue model against exact truth, then export it
#
# Every L2 backtester has a queue-model knob that is usually guessed: hftbacktest's `power_prob_queue_model3(n)`,
# NautilusTrader's `prob_fill_on_limit`. This notebook fits such a parameter against what really happened in the
# queue, checks it on a day it was not fitted on, and exports a snippet to paste into your backtest. The export is
# fail-safe: a fit that does not beat the library default out of sample is refused (`QT-CALIB-001`), so a number
# that would make the backtest worse never reaches it unmarked.
#
# Data: queuetruth's **synthetic** dataset (CME rules, 2 instruments, 2 days). The numbers only show the workflow;
# they say nothing about a real venue. Run the same cells on your own ingested order-level data for a real fit.

# %%
import warnings
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

import queuetruth as qt
from queuetruth import frames

pd.set_option("display.width", 140)
plt.rcParams.update({"figure.dpi": 80, "figure.figsize": (7.5, 3.4)})

WORK = Path("qt-notebooks") / "03_calibrate"
WORK.mkdir(parents=True, exist_ok=True)
DATA = WORK / "synth"
if not (DATA / "manifest.json").exists():
    qt.fixtures_synth(str(DATA))
TRAIN, TEST = {"dates": ["2026-06-15"]}, {"dates": ["2026-06-16"]}


def hz(ns):
    """`1000000000` -> `1s`, `100000000` -> `100ms`."""
    ns = int(ns)
    return f"{ns // 10**9}s" if ns % 10**9 == 0 else f"{ns // 10**6}ms"


def export(target, cal_dir, out, force=False):
    """`qt.api.export`, with the export notes (reported as Python warnings) printed as `note:` lines."""
    with warnings.catch_warnings(record=True) as notes:
        warnings.simplefilter("always")
        ex = qt.api.export({"target": target, "calibration": {"path": str(cal_dir)}, "out": str(out), "force": force})
    for n in notes:
        print("note:", n.message)
    return ex


print("queuetruth", qt.__version__, "| synthetic data, train", TRAIN["dates"], "test", TEST["dates"])

# %% [markdown]
# ## 1. Shadows that look like your orders
#
# The fit uses shadows, so place them where your strategy quotes. Here: at the touch (70%) or one tick behind
# (30%), 1 or 5 lots. Every shadow rests up to 5 minutes, the longest horizon scored, so held-out metrics cover
# every horizon.

# %%
SHADOWS = WORK / "shadows.parquet"
qt.sample_shadows(
    {
        "dataset": {"path": str(DATA)},
        "out": str(SHADOWS),
        "n": 2000,
        "seed": 0,
        "placements": [{"offset_ticks": 0, "weight": 0.7}, {"offset_ticks": -1, "weight": 0.3}],
        "qty": [{"kind": "lots", "lots": 1, "weight": 0.5}, {"kind": "lots", "lots": 5, "weight": 0.5}],
    }
)
print(len(frames.read(SHADOWS, expect="qt.shadow_spec")), "shadows")

# %% [markdown]
# ## 2. Fit hftbacktest's `power3` exponent `n`
#
# `kind="alloc"` fits how the model shares a cancel at your price level between the queue ahead of you and behind
# you, scored one cancel at a time at the exact queue state (a quasi-likelihood, with a profile interval and a
# block-bootstrap interval). Then the fitted model and the library default `n = 2` both run through the zoo on the
# test day.

# %%
power3 = qt.calibrate(
    {
        "kind": "alloc",
        "family": "power3",
        "dataset": {"path": str(DATA)},
        "shadows": {"path": str(SHADOWS)},
        "out": str(WORK / "cal-power3"),
        "train": TRAIN,
        "test": TEST,
    }
)
print(f"{power3.param_name} = {power3.estimate:.3f}  bootstrap CI {tuple(round(x, 3) for x in power3.ci_bootstrap)}")
print(f"from {power3.n_events} cancel events of {power3.n_shadows} train shadows")
print("trust:", power3.trust.verdict, power3.trust.reasons)

# %% [markdown]
# `frames.held_out` flattens the held-out comparison: one row per arm (`fitted` or `default`) and horizon, with the
# trust check's per-horizon verdict. To be exported, a fit must be no worse than the default on log loss and Brier
# score at every horizon and strictly better at one.

# %%
ho = frames.held_out(power3)
ho["horizon"] = ho["horizon_ns"].map(hz)
wide = ho.pivot(index="horizon", columns="arm", values=["logloss", "fill_rate_bias"])
wide = wide.reindex([hz(h) for h in sorted(ho["horizon_ns"].unique())])
wide["beats_default"] = ho.drop_duplicates("horizon").set_index("horizon")["beats_default"]
wide.round(5)

# %%
fig, ax = plt.subplots()
for arm, g in ho.groupby("arm"):
    ax.plot(g["horizon"], 100 * g["fill_rate_bias"], marker="o", label=f"{arm}")
ax.axhline(0, color="black", lw=0.8)
ax.set_ylabel("held-out fill-rate bias, pp")
ax.set_title("Synthetic data: power3 fitted vs default n = 2 on the test day")
ax.legend()
fig.tight_layout()

# %% [markdown]
# ## 3. Export, or see why the export is refused
#
# `export` writes the fitted value into a snippet with its provenance as comments. When the trust verdict is
# `refused`, it raises `QT-CALIB-001` (exit code 1 on the command line) and says why. `force=True` writes the snippet
# anyway under a `# !! NOT VALIDATED` header: for inspecting a fit, not for pasting it.

# %%
try:
    ex = export("hftbacktest", WORK / "cal-power3", WORK / "hbt_power3.py")
    print(ex.text)
except qt.QtError as e:
    print(f"refused: {e.code} (CLI exit code {e.exit_code})")
    print(e.message)
    forced = export("hftbacktest", WORK / "cal-power3", WORK / "hbt_power3_forced.py", force=True)
    print("\n".join(forced.text.splitlines()[:6]))

# %% [markdown]
# A refusal is an answer, not a failure: on this data the fitted `n` is worse than the default at 60 s on the test
# day (and better only at 5 minutes), so the default stays. A fit that cannot contain its own maximum is refused
# too. With the search grid narrowed to
# `n` between 2.7 and 7.4 (ln n from 1 to 2), the best value sits on the grid edge:

# %%
narrow = qt.calibrate(
    {
        "kind": "alloc",
        "family": "power3",
        "dataset": {"path": str(DATA)},
        "shadows": {"path": str(SHADOWS)},
        "out": str(WORK / "cal-narrow"),
        "train": TRAIN,
        "test": TEST,
        "grid": {"alloc": {"lo": 1.0, "hi": 2.0, "n": 21}},
    }
)
t = narrow.trust
print(f"n = {narrow.estimate:.3f}; trust {t.verdict}: reasons {t.reasons}, flags {t.flags}")
try:
    export("hftbacktest", WORK / "cal-narrow", WORK / "hbt_narrow.py")
except qt.QtError as e:
    print(f"refused: {e.code}: {e.message}")

# %% [markdown]
# ## 4. Fit NautilusTrader's `prob_fill_on_limit`
#
# The same workflow for NautilusTrader's touch fill model: `kind="nt_touch"` fits the probability that a touch of
# your price fills you, against the exact queue.

# %%
ntc = qt.calibrate(
    {
        "kind": "nt_touch",
        "family": "nt_prob_touch",
        "dataset": {"path": str(DATA)},
        "shadows": {"path": str(SHADOWS)},
        "out": str(WORK / "cal-nt"),
        "train": TRAIN,
        "test": TEST,
    }
)
print(f"{ntc.param_name} = {ntc.estimate:.3f}  bootstrap CI {tuple(round(x, 3) for x in ntc.ci_bootstrap)}")
print("trust:", ntc.trust.verdict, ntc.trust.reasons, {hz(h): v for h, v in ntc.trust.beats_default.items()})
try:
    print(export("nautilus", WORK / "cal-nt", WORK / "nt_fill_model.py").text)
except qt.QtError as e:
    print(f"refused: {e.code}: {e.message}")

# %% [markdown]
# ## 5. Look inside a fit
#
# Every calibration directory holds its evidence as tables. `frames.load_run` opens it: `alloc_events` holds every
# cancel at a shadow's price level on both days (the queue ahead and behind at each one, and how much of it came
# from ahead), `held_out/...` is the zoo run on the test day.

# %%
cal_run = frames.load_run(WORK / "cal-power3")
print(cal_run)
print("tables:", list(cal_run))
events = cal_run["alloc_events"]
print(len(events), "alloc events on both days; columns:", list(events.columns))
# The rows the power3 fit scored: the train day, with queue both ahead of and behind the shadow.
train_day = pd.Timestamp(TRAIN["dates"][0]).date()
fit_rows = events[(events["session_date"] == train_day) & (events["front"] > 0) & (events["back"] > 0)]
assert len(fit_rows) == power3.n_events
print(len(fit_rows), "of them are the train-day events the fit scored")

# %% [markdown]
# What the fit sees: where in the queue the cancelled quantity sat. Grouping the fit's events by the shadow's relative
# position (`front / (front + back)`, 0 = at the head of the queue), the share of cancelled quantity that came from
# ahead of the shadow is the curve a queue model's cancel allocation has to reproduce.

# %%
pos = fit_rows.copy()
pos["position"] = pd.cut(pos["front"] / (pos["front"] + pos["back"]), [0, 0.2, 0.4, 0.6, 0.8, 1.0], include_lowest=True)
g = pos.groupby("position", observed=True)[["ahead_cancel", "behind_cancel"]].sum()
g["share_ahead"] = g["ahead_cancel"] / (g["ahead_cancel"] + g["behind_cancel"])
g.assign(events=pos.groupby("position", observed=True).size()).round(3)

# %% [markdown]
# ## On your own data
#
# Replace the dataset with yours (`queuetruth ingest --format itch|dbn|coinbase|... --out my-ds`), place shadows
# where you quote, and train and test on different days (or `{"blocks": {"index": [...], "n_blocks": 5}}` windows
# within one day). The command-line equivalents are `queuetruth calibrate alloc|nt-touch ...` and
# `queuetruth export hftbacktest|nautilus --calibration ...`; the how-to *Calibrate my hftbacktest queue model* in
# the documentation has the full recipe, and [02](02_nautilus_too_optimistic.ipynb) runs the NautilusTrader fit on
# real Nasdaq data.
