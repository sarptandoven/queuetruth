# Validate my backtest against live fills

**Goal:** answer the question of hftbacktest discussion #332, "how are people validating the queue penalty against
live?", with numbers and intervals. The discussion proposes four checks. `queuetruth advanced score` and `calibrate-live`
give each of them:

| Check from #332 | Where it is |
|---|---|
| McNemar's test on the orders that filled in one run and not the other, on a fixed schedule | `score`: the `RESULT:` line, summary fields `mcnemar` and `discordance_clustered` |
| Markouts at 100 ms, 1 s and 10 s | `score`: the `markouts:` line, summary fields `markouts`, `markout_diff`, `sim_only_vs_live_only_markout` |
| Time-to-fill survival curves | `score`: the `time to fill:` line, summary field `ttf_survival`, the report's Time to fill section |
| Fit the latency first, then the queue penalty | `calibrate-live --latency-from-log` |

**You need:** `pip install queuetruth`, a live log of your orders (see
[Import fills from my venue or broker](import-live-logs.md)) and the market data of the same days. The blocks below
run on the synthetic fixture that `queuetruth advanced fixtures synth` writes, so you can try them first. They use no real
market data.

## 1. Score the simulator on the live schedule

<!-- check-docs: run -->
```sh
queuetruth advanced fixtures synth --out synth
queuetruth advanced score --live synth/own_orders/live --data synth --sim-model "hbt_power3[n=2]" --out sc
```

`score` turns every live order into a fixed schedule (same instrument, side, price, size, entry and cancel times),
runs the simulator on it, and compares the two arms order by order. Here the simulator is hftbacktest's
`PowerProbQueueFunc3` with `n = 2` as the zoo runs it. For your own backtest, import its fills with
`queuetruth advanced live import --venue sim_hftbacktest` and pass `--sim-log` instead of `--sim-model` (see
[Score and calibrate hftbacktest with the plugin](score-and-calibrate-hftbacktest.md)).

The first lines of the terminal answer each check in plain words: the fill-rate difference with its interval and
the discordance tests, the P&L gap, the share of orders still unfilled after 1 s in each arm, and the markout
difference at 100 ms, 1 s and 10 s.

## 2. Discordant fills: McNemar on a fixed schedule

Only the orders that filled in one arm and not the other carry information. `b` counts live-only fills, `c`
sim-only fills.

<!-- check-docs: run -->
```python
import json

s = json.load(open("sc/summary.json"))
m, dc = s["mcnemar"], s["discordance_clustered"]
print("live only b =", m["b"], " sim only c =", m["c"])
print("exact McNemar p =", m["p_exact"], "(assumes independent orders)")
print("cluster-robust p =", dc["p_value"], "method", dc["method"], "clusters", dc["n_clusters"])
print("fill-rate bias", s["bias"]["fill_rate_bias"], "95% CI", s["bias"]["ci_lo"], s["bias"]["ci_hi"])
```

Read the cluster-robust p, not the exact one. Orders placed in the same half hour see the same market, so their
disagreements are correlated and the plain McNemar test is too confident. The scorer clusters by session day (by
30-minute block when there are fewer than 5 days); see [Scorecard statistics](../concepts/scorecard.md#discordance-tests).

## 3. Markouts at 100 ms, 1 s and 10 s

<!-- check-docs: run -->
```python
import json

s = json.load(open("sc/summary.json"))
H = {100_000_000: "100 ms", 1_000_000_000: "1 s", 10_000_000_000: "10 s"}
for r in s["markouts"]:
    if r["horizon_ns"] in H and r["ref"] == "mid":
        print(r["arm"], H[r["horizon_ns"]], "mean", r["mean_ticks"], "ticks, 95% CI", r["ci_lo"], r["ci_hi"], "n", r["n"])
for r in s["markout_diff"]:
    if r["horizon_ns"] in H and r["ref"] == "mid":
        print("sim minus live", H[r["horizon_ns"]], r["mean_diff_ticks"], "CI", r["ci_lo"], r["ci_hi"])
for r in s["sim_only_vs_live_only_markout"]:
    if r["horizon_ns"] in H:
        print(H[r["horizon_ns"]], "sim-only fills:", r["n_sim_only"], "mean", r["sim_only_mean"],
              "CI", r["sim_only_ci_lo"], r["sim_only_ci_hi"], "| live-only fills:", r["n_live_only"])
```

The three horizons are in the default set (with 50 ms, 15 s, 60 s and 300 s; choose others with
`--markout-horizons`). Markouts are in ticks against this dataset's own mid. The paired difference uses the orders
filled in both arms. The last rows are the discordant fills: if the fills only the simulator makes mark out worse
than the fills only live makes, the simulator fills you exactly when you would be picked off.

## 4. Time-to-fill survival curves

<!-- check-docs: run -->
```python
import json

sv = json.load(open("sc/summary.json"))["ttf_survival"]
for arm in ("live", "sim"):
    a = sv["arms"][arm]
    print(arm, "filled", a["n_filled"], "censored", a["n_censored"], "median ns", a["median_ns"])
    for x in a["at"]:
        print("   still unfilled after", x["horizon_ns"] / 1e9, "s:", round(x["survival"], 3), "CI", x["ci_lo"], x["ci_hi"])
for d in sv["diff"]:
    print("sim minus live at", d["horizon_ns"] / 1e9, "s:", d["diff"], "CI", d["ci_lo"], d["ci_hi"])
print("log-rank p", sv["logrank"]["p_value"], "|", sv["logrank"]["method"], "|", sv["logrank"]["n_clusters"], "clusters")
```

Each arm gets a Kaplan-Meier curve of the time to the first maker fill on the same orders. A cancel, an expiry or the
end of the window censors the order at that time. A sim curve below the live curve means the simulator fills sooner
than live. The test is a log-rank stratified by instrument whose variance treats the two arms of an order, and the
orders of one instrument and day, as one cluster. With few clusters it cannot give a very small p-value; that is
intended. `queuetruth report --in sc --out report.html` draws both curves with their intervals.

## 5. Fit the latency first, then the queue penalty

The queue penalty and the latency both delay fills, so a fit of the queue alone absorbs any latency it was not told
about. Measure the latency on the live log first, then fit the queue with it:

<!-- check-docs: run -->
```sh
queuetruth advanced calibrate latency --live synth/own_orders/live --out lat
queuetruth advanced calibrate-live --data synth --live synth/own_orders/live --family power3 --latency-from-log --out fit
```

`calibrate latency` prints the latency distribution (entry, response, cancel; one-way values assume synchronized
clocks). `calibrate-live --latency-from-log` takes the median entry latency (exchange ack time minus local send time)
from the same log, schedules each order at its send time plus that latency, and only then fits the queue parameter.
It prints both: a `latency (fitted first, on the live log)` line and the `fitted:` line with its intervals, held-out
check and verdict.

<!-- check-docs: run -->
```python
import json

c = json.load(open("fit/calibration.json"))
print("latency p50 ns:", c["latency"]["entry_ns"]["p50"], "| entry rule:", c["spec"]["entry"], c["spec"]["latency"])
print(c["param_name"], "=", c["estimate"], "profile CI", c["ci_profile"], "bootstrap CI", c["ci_bootstrap"])
print("verdict:", c["trust"]["verdict"], c["trust"]["reasons"])
print("hftbacktest latency file:", c["exports"]["latency_npz"])
```

On this one-day synthetic fixture the fit lands at the edge of the grid, so its verdict is `refused` and
`queuetruth advanced export` will not write it. A real log of several days usually gives a usable fit; when it does,
`queuetruth advanced export hftbacktest --calibration fit/calibration.json` writes a snippet with both the latency file and
the queue model.

When your log has exchange ack times, the default entry rule (`exchange_ack`) already starts each order when the
venue accepted it. The latency is then part of the entry time, and `--latency-from-log` changes how the order is
scheduled, not what is measured. Use it when you want the simulator's latency model and its queue model to be fitted
together, as you will run them in the backtest.

That the recipe recovers both values is checked on synthetic data with a known answer
(`python/tests/score/test_score_latency_first.py`): a live log with a 2 s entry latency and a known queue parameter
gives back the 2 s latency and the true parameter inside its profile interval. Fitting the queue on send times without
the latency gives a parameter that is too small.

## 6. Score again with the fitted value

Score the fitted model on held-out days and read the same four checks:

<!-- check-docs: skip (needs an accepted fit on your own live log and data) -->
```sh
N=$(python -c 'import json; print(json.load(open("fit/calibration.json"))["estimate"])')
queuetruth advanced score --live live_test --data data --sim-model "hbt_power3[n=$N]" --out sc_fitted
```

## What these numbers do not tell you

- They describe this venue, feed and period. A queue penalty fitted on one venue does not transfer to another.
- The live arm is one realisation: your own orders were in the book, and the scorer removes them from the market
  data before the simulator runs (see [Scorecard statistics](../concepts/scorecard.md#the-exact-arm-and-your-own-orders)).
- A p-value above 0.05 is not evidence that the simulator is right. Use the equivalence test (`--fill-rate-margin`,
  `--markout-margin-ticks`) to show that the difference is small.
