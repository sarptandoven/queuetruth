"""Gates and tracked metrics of each nightly check, computed from the documents the underlying harnesses write.

Every function returns an `Eval`: `gates`, a list of `{"name", "ok", "detail"}` (the check passes when every gate is
ok); `metrics`, a flat `{name: scalar}` dict recorded in the history and diffed against the last comparable run;
`noisy`, the metric names or name prefixes that are timings (reported with their relative change, never counted as a
change); `provenance`, a flat `{name: scalar}` dict of what the numbers were computed from (build hashes, engine
versions, input paths, whether a subset was streamed). Provenance is shown on the page and its changes are listed as
context, but it is never diffed as a result: a commit changes the source tree hash every time, and an exit code that
fires on every commit would mean nothing.
"""

from __future__ import annotations

import math
import re
from typing import Any, NamedTuple

from .history import flatten

Gate = dict[str, Any]
HORIZONS = ("0.1s", "1s", "10s", "60s")
# a key segment that measures time, speed, memory, load or free disk (never deterministic); a metric is a timing when
# ANY segment of its flattened name matches, so `zoo_wall_s.faithful` and `alloc/power3_wall_s` are timings too
TIMING = re.compile(r"(wall_s|wall_s_step|per_s|seconds|elapsed_s|rss_mb|rss_bytes|peak_rss|free_gib\w*|load_avg\w*)$")
_SEGMENTS = re.compile(r"[./\[\]]+")
# reproduce.py numbers that say where an input or output lives or how it got there, not what it contains (the
# content is pinned by the sha256 numbers next to them, which stay tracked)
REPRO_IDENTITY = frozenset({"subset", "streamed_now", "source_url", "artifact", "stdout_tail"})
# hftbacktest coverage: the share of the orders handed to hftbacktest that must be compared. Orders excluded after a
# known hftbacktest error (#312, error code 14) are allowed up to the remaining 10%; any other error code fails.
HBT_MIN_COVERAGE = 0.9
HBT_KNOWN_ERROR_CODES = frozenset({14})


class Eval(NamedTuple):
    gates: list[Gate]
    metrics: dict[str, Any]
    noisy: list[str]
    provenance: dict[str, Any]


def gate(name: str, ok: bool, detail: str = "") -> Gate:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _num(x: Any) -> Any:
    """JSON-safe scalar: NaN and infinities become None."""
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


def is_timing(key: str) -> bool:
    """True when any segment of the flattened metric name is a timing (see `TIMING`)."""
    return any(TIMING.search(s) for s in _SEGMENTS.split(key) if s)


def scrub_paths(value: Any, roots: dict[str, str]) -> Any:
    """`value` with every occurrence of a root directory replaced by its placeholder (longest root first), so a path
    under this run's output directory compares equal across nights. Non-strings are returned unchanged."""
    if not isinstance(value, str) or not roots:
        return value
    for root in sorted(roots, key=len, reverse=True):
        if root:
            value = value.replace(root, roots[root])
    return value


def nt_major(version: str) -> int:
    return int(version.split(".")[0])


def evaluate_nt(report: dict[str, Any], version: str) -> Eval:
    """`scripts/nt_crosscheck/run.py` report of one NautilusTrader version.

    Gates (all versions): the report is for `version`; the touch default (`touch_p1`, and `d10_touch_p1` when run)
    fills exactly the orders `nt_prob_touch[p=1]` fills at every horizon; each rule-set replica reproduces its
    reference on every order and horizon (`nt_touch` / `nt_queue` reproduce NautilusTrader itself, so a new
    NautilusTrader rule shows up here); no disagreement is left unexplained by the replica rule flips.
    1.x only: `queue_position=True` on the delta feed (`queue_p1`) fills exactly the orders `nt_queue_l2` fills
    (2.x caps queue ahead on book updates, a known and documented difference, so it is tracked but not gated).
    """
    gates: list[Gate] = []
    got = str(report.get("nautilus_trader"))
    gates.append(gate("nautilus_trader version", got == version, f"report {got}, expected {version}"))
    configs = report.get("configs", {})
    metrics: dict[str, Any] = {"nt.n_shadows": report.get("n_shadows")}
    for cname in sorted(configs):
        hz = configs[cname].get("horizons", {})
        for h in HORIZONS:
            row = hz.get(h)
            if row is None:
                continue
            for k in ("nt_rate", "zoo_rate", "exact_rate", "agree", "nt1_zoo0", "nt0_zoo1"):
                if k in row:
                    metrics[f"{cname}.{h}.{k}"] = _num(row[k])
    must_agree = ["touch_p1", "d10_touch_p1"] + (["queue_p1"] if nt_major(version) < 2 else [])
    for cname in must_agree:
        if cname not in configs:
            if cname == "d10_touch_p1":
                continue  # depth-10 configs run only with --depth10
            gates.append(gate(f"{cname} ran", False, "config missing from the report"))
            continue
        hz = configs[cname]["horizons"]
        bad = {h: hz[h].get("agree") for h in HORIZONS if h in hz and hz[h].get("agree") != 1.0}
        missing = [h for h in HORIZONS if h not in hz]
        gates.append(
            gate(
                f"{cname} agrees with {configs[cname].get('zoo_model')} on every order",
                not bad and not missing,
                "every horizon 100%" if not bad and not missing else f"agreement {bad}, missing horizons {missing}",
            )
        )
    rv = report.get("replica_validation")
    if rv is None:
        gates.append(gate("rule-set replicas validated", False, "no replica_validation in the report"))
    else:
        for name in sorted(rv):
            v = rv[name]
            metrics[f"replica.{name}.agree"] = v.get("agree")
            metrics[f"replica.{name}.total"] = v.get("total")
            ok = v.get("total", 0) > 0 and v.get("agree") == v.get("total")
            gates.append(
                gate(f"replica {name} reproduces {v.get('against')}", ok, f"{v.get('agree')}/{v.get('total')}")
            )
    dc = report.get("discrepancy_classes", {})
    unexplained = 0
    for fam in sorted(dc):
        for cause, info in sorted(dc[fam].items()):
            metrics[f"discrepancy.{fam}.{cause}"] = info.get("n")
            if cause == "unexplained":
                unexplained += int(info.get("n", 0))
    gates.append(gate("no unexplained disagreement", unexplained == 0, f"{unexplained} unexplained"))
    return Eval(gates, metrics, [], {"nt.version": got})


def evaluate_hbt(report: dict[str, Any]) -> Eval:
    """`scripts/nightly/hbt_xcheck.py compare` report, per model:

    - every compared order has the same first fill timestamp in hftbacktest and in the faithful zoo model;
    - no order handed to hftbacktest was expired or rejected on arrival (the price rule drops every order that would
      cross, so one that did not rest means the two sides priced it differently);
    - every hftbacktest error is the known #312 (error code 14), and at least `HBT_MIN_COVERAGE` of the orders were
      compared (the rest excluded after that error).
    """
    gates: list[Gate] = []
    metrics: dict[str, Any] = {"hbt.n_orders": report.get("n_orders")}
    for k, v in sorted(report.get("dropped", {}).items()):
        metrics[f"hbt.dropped.{k}"] = v
    models = report.get("models", {})
    if not models:
        gates.append(gate("hftbacktest runs compared", False, "no model in the report"))
    for mid in sorted(models):
        m = models[mid]
        for k in ("orders", "compared", "agree_first_fill_ts", "not_resting_in_hbt", "excluded_after_error"):
            metrics[f"{mid}.{k}"] = m.get(k)
        errors = m.get("hbt_errors", [])
        metrics[f"{mid}.hbt_errors"] = len(errors)
        for h in HORIZONS:
            for k, v in sorted(m.get("horizons", {}).get(h, {}).items()):
                metrics[f"{mid}.{h}.{k}"] = v
        orders, compared = int(m.get("orders") or 0), int(m.get("compared") or 0)
        ok = compared > 0 and m.get("agree_first_fill_ts") == compared
        ex = m.get("mismatches", [])[:1]
        gates.append(
            gate(
                f"{mid}: hftbacktest first fills equal the zoo's",
                ok,
                f"{m.get('agree_first_fill_ts')}/{compared} orders" + (f"; e.g. {ex[0]}" if ex else ""),
            )
        )
        nr = int(m.get("not_resting_in_hbt") or 0)
        gates.append(gate(f"{mid}: every order rested in hftbacktest", nr == 0, f"{nr} expired or rejected on arrival"))
        unknown = sorted({e.get("error_code") for e in errors} - HBT_KNOWN_ERROR_CODES, key=str)
        need = math.ceil(HBT_MIN_COVERAGE * orders)
        gates.append(
            gate(
                f"{mid}: at least {HBT_MIN_COVERAGE:.0%} of orders compared, only known hftbacktest errors",
                not unknown and orders > 0 and compared >= need,
                f"{compared}/{orders} compared, {m.get('excluded_after_error')} excluded after an hftbacktest error, "
                f"{len(errors)} error(s)" + (f", unknown codes {unknown}" if unknown else ""),
            )
        )
    return Eval(gates, metrics, [], {"hbt.engine": report.get("engine")})


def evaluate_repro(
    doc: dict[str, Any], rc: int, steps_expected: list[str], roots: dict[str, str] | None = None
) -> Eval:
    """`scripts/repro/reproduce.py` results.json: the script exited 0, every expected step ran and passed (or recorded
    its numbers), the build did not change during the run, and the run was not aborted by the disk guard.

    `roots` maps directories (this run's output directory, the state directory, the checkout) to placeholders that
    replace them in every string, so numbers that embed a path compare equal across nights. Timings (any key segment
    matching `TIMING`) are noisy; where an input lives or how it got there (`REPRO_IDENTITY`) and the build identity
    are provenance.
    """
    roots = roots or {}
    gates = [gate("reproduce.py exit code 0", rc == 0, f"exit {rc}")]
    steps = doc.get("steps", {})
    metrics: dict[str, Any] = {}
    noisy: list[str] = []
    provenance: dict[str, Any] = {}
    for key in steps_expected:
        if key not in steps:
            gates.append(gate(f"step {key} ran", False, "missing from results.json"))
    for key in sorted(steps):
        st = steps[key]
        status = st.get("status")
        bad = [c["name"] for c in st.get("checks", []) if not c.get("ok")]
        gates.append(
            gate(
                f"step {key}",
                status in ("pass", "recorded"),
                f"{status}"
                + (f"; failed checks {bad[:5]}" if bad else "")
                + (f"; {scrub_paths(st['error'], roots)}" if "error" in st else ""),
            )
        )
        metrics[f"{key}.status"] = status
        metrics[f"{key}.checks_ok"] = sum(1 for c in st.get("checks", []) if c.get("ok"))
        metrics[f"{key}.checks_total"] = len(st.get("checks", []))
        for k, v in flatten(f"{key}.numbers", st.get("numbers", {})).items():
            leaf = k.removeprefix(f"{key}.numbers.").split(".", 1)[0]
            if leaf in REPRO_IDENTITY:
                provenance[k] = scrub_paths(v, roots)
                continue
            metrics[k] = _num(scrub_paths(v, roots))
            if is_timing(k.removeprefix(f"{key}.")):
                noisy.append(k)
        for k in ("wall_s_step", "free_gib_after"):
            if k in st:
                metrics[f"{key}.{k}"] = st[k]
                noisy.append(f"{key}.{k}")
    gates.append(
        gate("build unchanged during the run", not doc.get("build_changed_during_run"), "core and source tree hashes")
    )
    gates.append(gate("not aborted", "aborted" not in doc, str(doc.get("aborted", ""))))
    build = doc.get("build", {})
    for k in ("queuetruth_version", "source_tree_sha256"):
        if k in build:
            provenance[f"build.{k}"] = build[k] if not isinstance(build[k], dict) else build[k].get("sha256")
    ce = build.get("core_extension")
    if isinstance(ce, dict) and "sha256" in ce:
        provenance["build.core_extension_sha256"] = ce["sha256"]
    return Eval(gates, metrics, noisy, provenance)


BENCH_VERDICT = {0: "no regression", 1: "regression", 2: "error", 3: "not comparable"}


def evaluate_bench(
    results: dict[str, Any] | None,
    bench_rc: int,
    check: dict[str, Any] | None,
    check_rc: int | None,
    baseline: str | None = None,
) -> Eval:
    """`benches/bench.py` results and, when a baseline exists, `benches/check.py` against it.

    Gates: bench.py exited 0 (no case errored); every case and every derived ratio (`results["derived"]`: shadow
    overhead, Python-vs-CLI cost) that has a SPEC 7.1 target meets it; check.py found no regression against the
    baseline (exit 1 fails). An environment that is not comparable (check.py exit 3, e.g. the power source or Low
    Power Mode changed) or no baseline is reported, not failed. `baseline` names the baseline in the gate detail.
    """
    gates = [gate("bench.py exit code 0", bench_rc == 0, f"exit {bench_rc}")]
    metrics: dict[str, Any] = {}
    noisy = ["bench.case.", "bench.derived.", "bench.vs_previous"]
    provenance: dict[str, Any] = {"bench.baseline": baseline or "none"}
    if results is None:
        gates.append(gate("bench results written", False, "no results file"))
        return Eval(gates, metrics, noisy, provenance)
    metrics["bench.suite"] = results.get("suite")
    for c in results.get("cases", []):
        cid = c.get("id")
        metrics[f"bench.status.{cid}"] = c.get("status")
        summ = c.get("summary") or {}
        metrics[f"bench.case.{cid}.median"] = _num(summ.get("median"))
        t = c.get("target")
        if t:
            gates.append(
                gate(
                    f"{cid} target {t.get('op')} {t.get('value'):g}",
                    t.get("met") is True,
                    f"median {summ.get('median')} ({c.get('status')})",
                )
            )
    for d in results.get("derived", []):
        did = d.get("id")
        summ = d.get("summary") or {}
        metrics[f"bench.derived.{did}.median"] = _num(summ.get("median"))
        t = d.get("target")
        if t:
            gates.append(
                gate(
                    f"{did} ({d.get('kind')}) target {t.get('op')} {t.get('value'):g}",
                    t.get("met") is True,
                    f"median {summ.get('median')}",
                )
            )
    if check_rc is None:
        metrics["bench.vs_previous"] = "no previous run"
    else:
        verdict = BENCH_VERDICT.get(check_rc, f"exit {check_rc}")
        metrics["bench.vs_previous"] = verdict
        detail = f"{verdict} against {baseline}" if baseline else verdict
        if check:
            regs = [r.get("id") for r in check.get("rows", []) if r.get("status") == "regression"]
            if regs:
                detail += f": {regs}"
            if check.get("env_reasons"):
                detail += f"; environment: {check['env_reasons']}"
            for r in check.get("rows", []):
                metrics[f"bench.vs_previous.{r.get('id')}"] = r.get("status")
        gates.append(gate("no regression against the baseline", check_rc in (0, 3), detail))
    return Eval(gates, metrics, noisy, provenance)
