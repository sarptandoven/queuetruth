"""The answer-first layout (docs/design/extensions/report-redesign.md): the answer block (one plain sentence per
question with its base rate and 95% CI, numbers checked against the summaries), the leaderboard right after it,
collapsed drill-downs and provenance, every section and anchor reachable, the script-free theme switch, print rules
that never use the dark tokens, accessible markup, and a bound on what is visible before anything is expanded."""

from __future__ import annotations

import html as html_mod
import itertools
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from queuetruth.report import fmt as F
from queuetruth.report import page, render
from queuetruth.report.answer import ZOO_MARGIN, warnings_naming
from queuetruth.report.page import DARK_TOKENS, THEME_IDS
from report import pageparse as pp
from report import synth as rc

S = 1_000_000_000
H60 = str(60 * S)


def _render(tmp: Path, inputs: list[Any], **kw: Any) -> tuple[Any, str]:
    out = tmp / "report.html"
    summary = render({"inputs": [str(i) for i in inputs], "out": str(out), **kw})
    return summary, out.read_text(encoding="utf-8")


def _answers(html: str) -> list[dict[str, str]]:
    """(badge label, question, sentence, href) of every answer, in order."""
    out = []
    for li in re.findall(r"<li>(<span class=\"badge.*?)</li>", pp.section(html, "answer"), re.S):
        m = re.match(
            r'<span class="badge (\w+)"><span aria-hidden="true">([^<]+)</span> ([^<]+)</span><div><h3>([^<]+)</h3>'
            r'<p>([^<]+)</p><a class="go" href="(#[a-z_]+)">([^<]+)</a></div>$',
            li,
        )
        assert m, li
        out.append(
            {
                "cls": m.group(1),
                "icon": m.group(2),
                "label": m.group(3),
                "q": m.group(4),
                "s": html_mod.unescape(m.group(5)),
                "href": m.group(6),
            }
        )
    return out


class Visible(HTMLParser):
    """What a reader sees on screen before expanding anything: the text and the tags outside closed `<details>`
    bodies (the `<summary>` of a closed block is visible) and outside `hidden` elements."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []  # (tag, hides its non-summary content)
        self.hidden = 0
        self.in_summary: list[int] = []
        self.tags: dict[str, int] = {}
        self.text: list[str] = []

    def _hidden_now(self) -> bool:
        return self.hidden > len(self.in_summary)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("meta", "input", "br", "wbr", "link"):
            if not self._hidden_now():
                self.tags[tag] = self.tags.get(tag, 0) + 1
            return
        if tag == "summary" and self.stack and self.stack[-1] == ("details", True):
            self.in_summary.append(self.hidden)
        if not self._hidden_now():
            self.tags[tag] = self.tags.get(tag, 0) + 1
        # a closed <details> hides all but its summary; the `hidden` attribute hides the element (print-only copies)
        closed = (tag == "details" and not any(k == "open" for k, _ in attrs)) or any(k == "hidden" for k, _ in attrs)
        self.stack.append((tag, closed))
        if closed:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("meta", "input", "br", "wbr", "link"):
            return
        t, closed = self.stack.pop()
        assert t == tag, (t, tag)
        if tag == "summary" and self.in_summary and self.in_summary[-1] == self.hidden:
            self.in_summary.pop()
        if closed:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden_now():
            self.text.append(data)


def _visible(html: str) -> Visible:
    body = html.split("<main>", 1)[1].split("</main>", 1)[0]
    v = Visible()
    v.feed(body)
    v.close()
    assert v.stack == []
    return v


# ---- order, reachability, collapsed provenance -------------------------------------------------------------------


def test_answer_then_leaderboard_then_drill_downs(rendered: tuple[Any, str]) -> None:
    summary, html = rendered
    assert summary.sections[:4] == ["header", "answer", "leaderboard", "verdict"]
    order = [html.index(f'id="{s}"') for s in summary.sections]
    assert order == sorted(order)
    # nothing but the header and the table of contents sits above the answer
    above = html.split('<section id="answer"', 1)[0].split("<main>", 1)[1]
    assert above.count("<section") == 0 and above.count("<table") == 1  # the inputs table, collapsed


def test_every_section_and_anchor_is_reachable(rendered: tuple[Any, str]) -> None:
    summary, html = rendered
    nav = re.search(r'<nav class="toc" aria-label="Sections">(.*?)</nav>', html, re.S).group(1)
    assert re.findall(r'href="#([a-z_]+)"', nav) == summary.sections[1:]
    ids = set(re.findall(r'\sid="([^"]+)"', html))
    targets = set(re.findall(r'href="#([^"]+)"', html))
    assert targets and targets <= ids, targets - ids
    assert 'href="#answer"' in html.split("<main>", 1)[0]  # skip link before the page content


def test_provenance_and_hashes_are_collapsed(bundle: Any, rendered: tuple[Any, str]) -> None:
    _, html = rendered
    head = re.search(r'<header id="header"[^>]*>(.*?)</header>', html, re.S).group(1)
    v = _visible(html)
    seen = "".join(v.text)
    for d in (bundle.score, bundle.zoo):
        for p in sorted(d.iterdir()):
            if p.suffix in (".json", ".parquet"):
                h = rc.sha256(p)
                assert h in head  # still in the header (SPEC 5.9: inputs with sha256) ...
                assert h not in seen  # ... but one click away
    assert "Synthetic or fixture data" in seen  # the provenance banner stays visible
    assert "<summary><h2>Inputs</h2>" in head and "<summary><h2>Scope and limits</h2>" in head
    cfg = pp.section(html, "config")
    assert cfg.count('<details class="item"><summary><h3>') >= 4  # one collapsed block per summary
    assert "Spec echo" in cfg and "Files recorded by this summary" in cfg


def test_drill_downs_collapsed_and_visible_content_bounded(rendered: tuple[Any, str]) -> None:
    _, html = rendered
    v = _visible(html)
    all_svgs = html.count("<svg")
    # open: 1 never-cancel forest + 1 cancel-bearing forest + 1 reliability model (5 panels) + 1 decile heatmap in the
    # zoo, the score charts, the calibration and learned-model charts; closed: the other horizons, models and traces
    assert v.tags.get("svg", 0) < all_svgs / 2
    zoo = pp.section(html, "zoo")
    assert zoo.count("<details") > 0 and "<summary>Signed fill-rate bias at the other horizons" in zoo
    why = pp.section(html, "why")
    assert why.count('<details class="item why"') == 6 and " open>" not in why.split("</summary>", 1)[0]
    vis = "".join(v.text)
    assert "At-price events" not in vis and "Exact queue trajectory" not in vis  # traces are collapsed


# ---- the answer block --------------------------------------------------------------------------------------------


def test_answer_sentences_match_the_summaries(bundle: Any, rendered: tuple[Any, str]) -> None:
    _, html = rendered
    ans = _answers(html)
    assert [a["href"] for a in ans] == [
        "#leaderboard",
        "#zoo",
        "#verdict",
        "#calibration",
        "#learned_model",
        "#data_quality",
    ]
    for a in ans:
        assert a["cls"] in ("good", "warn", "crit", "info", "muted") and a["icon"] and a["label"].isupper()
    zdoc = rc.json_load(bundle.zoo / "summary.json")

    def key(e: dict) -> tuple:
        v = e["fill_rate_bias"][H60]["value"]
        return (abs(v), e["brier"][H60], e["model_id"])

    best = sorted(zdoc["leaderboard"], key=key)[0]
    fb = best["fill_rate_bias"][H60]
    exact = best["exact_fill_rate"][H60]
    s0 = ans[0]["s"]
    assert s0.startswith(f"At 60 s, the exact replay fills {F.pct(exact)} of ")
    assert rc.esc(best["model_id"]) in s0
    assert f"fills {F.pct(best['model_fill_rate'][H60])} ({F.pp(fb['value'])}, 95% CI {F.pp(fb['ci_lo'])} to " in s0
    noise = fb["ci_lo"] <= 0 <= fb["ci_hi"]
    narrow = -ZOO_MARGIN <= fb["ci_lo"] and fb["ci_hi"] <= ZOO_MARGIN
    want = "WITHIN NOISE" if noise and narrow else ("INCONCLUSIVE (WIDE CI)" if noise else "BIASED")
    assert ans[0]["label"] == want
    over = [e for e in zdoc["leaderboard"] if e["fill_rate_bias"][H60]["ci_lo"] > 0]
    under = [e for e in zdoc["leaderboard"] if e["fill_rate_bias"][H60]["ci_hi"] < 0]
    n = len(zdoc["leaderboard"])
    if over:
        assert ans[1]["s"].startswith(f"Yes: {len(over)} of {n} models with a 95% CI fill significantly more often")
        assert "than the exact FIFO replay of the same orders" in ans[1]["s"]
        assert ans[1]["s"].count("A backtest with them overstates passive fill rates.") == 1
        assert "live order" not in ans[1]["s"]  # what was measured: fill rates against the exact replay
        assert ans[1]["label"] == "OPTIMISTIC MODELS" and ans[1]["cls"] == "crit"
        top = max(over, key=lambda e: e["fill_rate_bias"][H60]["value"])
        assert f"{rc.esc(top['model_id'])} {F.pp(top['fill_rate_bias'][H60]['value'])}" in ans[1]["s"]
    if under:
        assert f"{len(under)} of {n} fill significantly less often" in ans[1]["s"]
    sdoc = rc.json_load(bundle.score / "summary.json")
    t = sdoc["two_by_two"]
    tot = t["both"] + t["neither"] + t["live_only"] + t["sim_only"]
    live, sim = (t["both"] + t["live_only"]) / tot, (t["both"] + t["sim_only"]) / tot
    b = sdoc["bias"]
    s2 = ans[2]["s"]
    assert f"fills {F.pct(sim)} of {tot:,} scored orders vs {F.pct(live)} live: {F.pp(b['fill_rate_bias'])}" in s2
    assert f"95% CI {F.pp(b['ci_lo'])} to {F.pp(b['ci_hi'])}" in s2
    assert ans[2]["label"] == "GATES FAILED"  # the synthetic scorecard fails its max_abs_fill_rate_bias gate
    cal = pp.section(html, "calibration")
    assert (ans[3]["label"] == "EXPORTABLE") == ("Exportable: the fit is identified" in cal)
    cdoc = rc.json_load(bundle.cal / "calibration.json")
    assert f"from {cdoc['n_events']:,} events on {cdoc['n_shadows']:,} shadows" in ans[3]["s"]
    card = rc.json_load(bundle.hazard / "model.json")
    bss = card["scores_test"]["bss_vs_climatology"]
    k_pos = sum(1 for v in bss.values() if v["ci_lo"] is not None and v["ci_lo"] > 0)
    assert f"at {k_pos} of {len(bss)} horizons" in ans[4]["s"]
    assert f"On {card['n_orders']['test']:,} held-out orders" in ans[4]["s"]
    # the verdicts cover the scorecard and the replay; the zoo, calibration and learned-model inputs carry none
    assert ans[5]["label"] == "WARNINGS" and ans[5]["s"].startswith(
        f"Data-quality verdict warnings for {bundle.score}, {bundle.replay}. First reason: "
    )
    assert ans[5]["s"].endswith(f"{bundle.zoo}, {bundle.cal}, {bundle.hazard} carry no data-quality verdict.")
    lead = pp.section(html, "answer").split("<ol", 1)[0]
    assert "synthetic or fixture data: these answers exercise the pipeline only" in lead
    assert "with its base rate and 95% interval where the inputs carry them" in lead
    assert 'zero-impact shadow orders, which never move the book or take liquidity (see <a href="#limits">' in lead
    # the warning count in the lead is the length of the (collapsed, badged) list it links to
    cfg = pp.section(html, "config")
    wl = re.search(r'<details class="more warns" id="config-warnings"><summary>(.*?)</summary>(.*?)</details>', cfg)
    assert wl and "WARNINGS</span>" in wl.group(1)
    n_warn = wl.group(2).count("<li>")
    assert n_warn and f'<a href="#config-warnings">{n_warn} warning(s) across ' in lead
    assert f"{n_warn} warning(s) across " in wl.group(1)


def _zoo_with(tmp: Path, edit: Any) -> Path:
    d = tmp / "zoo"
    rc.make_zoo_dir(d, n=80)
    doc = rc.json_load(d / "summary.json")
    edit(doc)
    (d / "summary.json").write_text(json.dumps(doc), encoding="utf-8")
    return d


def test_answer_without_base_rates_or_optimistic_models(tmp_path: Path) -> None:
    def old(doc: dict) -> None:  # a 0.1.0 summary: no model / exact fill rates; every CI straddles 0 or is below
        for e in doc["leaderboard"]:
            e.pop("model_fill_rate", None)
            e.pop("exact_fill_rate", None)
            for cv in e["fill_rate_bias"].values():
                cv["ci_hi"] = max(cv["ci_hi"], 0.0) if cv["value"] <= 0 else cv["ci_hi"]
                cv["ci_lo"] = min(cv["ci_lo"], 0.0)

    d = _zoo_with(tmp_path, old)
    _, html = _render(tmp_path / "a", [d])
    a = _answers(html)
    assert a[0]["s"].startswith("At 60 s, ") and "the exact replay fills" not in a[0]["s"]
    assert "has the smallest fill-rate bias" in a[0]["s"]
    # the widened CIs contain 0 but reach beyond the margin: inconclusive, never a green "within noise"
    assert a[0]["label"] == "INCONCLUSIVE (WIDE CI)" and a[0]["cls"] == "info"
    assert "its 95% CI contains 0 but reaches beyond ±2.0 pp, too wide to show the model is accurate." in a[0]["s"]
    assert a[1]["s"].startswith("No: no model fills significantly more often than the exact FIFO replay")
    assert a[1]["label"] == "NONE OPTIMISTIC" and a[1]["cls"] == "good"
    lb = pp.section(html, "leaderboard")
    assert "fill rate 60 s</th>" not in lb  # no rate column without rates
    assert pp.leaderboard_rows(html)[0] != "exact fill rate (truth)"


def _set_bias(e: dict, v: float, lo: float | None, hi: float | None) -> None:
    e["fill_rate_bias"][H60] = {"value": v, "ci_lo": lo, "ci_hi": hi}


def test_answer_never_reads_a_missing_ci_as_within_noise(tmp_path: Path) -> None:
    def no_ci(doc: dict) -> None:  # one cluster: the bootstrap gives no interval for any model
        for e in doc["leaderboard"]:
            for cv in e["fill_rate_bias"].values():
                cv["ci_lo"] = cv["ci_hi"] = None

    _, html = _render(tmp_path / "a", [_zoo_with(tmp_path, no_ci)])
    a = _answers(html)
    assert [x["label"] for x in a] == ["NO CI", "NO CI"] and {x["cls"] for x in a} == {"info"}
    assert "(-" in a[0]["s"] or "(+" in a[0]["s"]
    assert (
        ", no CI); no 95% interval could be computed (for example a single cluster), so it cannot be called "
        in (a[0]["s"])
    )
    assert "and lies within" not in a[0]["s"] and "6 of 6 models have no CI." in a[0]["s"]
    assert a[1]["s"] == "Cannot tell: no model has a 95% CI at 60 s."
    assert not any(x["cls"] == "good" for x in a)

    def some_ci(doc: dict) -> None:  # the closest model keeps a narrow CI; two others lose theirs
        lb = sorted(doc["leaderboard"], key=lambda e: abs(e["fill_rate_bias"][H60]["value"]))
        _set_bias(lb[0], 0.001, -0.004, 0.006)
        for e in lb[1:3]:
            e["fill_rate_bias"][H60]["ci_lo"] = e["fill_rate_bias"][H60]["ci_hi"] = None
        for e in lb[3:]:  # significantly below the exact replay
            _set_bias(e, -0.2, -0.25, -0.15)

    _, html = _render(tmp_path / "b", [_zoo_with(tmp_path / "b", some_ci)])
    a = _answers(html)
    assert a[0]["label"] == "WITHIN NOISE" and "2 of 6 models have no CI." in a[0]["s"]
    assert a[1]["label"] == "PARTLY JUDGED" and a[1]["cls"] == "info"
    assert a[1]["s"].startswith("None of the 4 models with a 95% CI fills significantly more often")
    assert (
        "3 of 4 fill significantly less often" in a[1]["s"]
        and "2 model(s) have no CI and are not judged." in (a[1]["s"])
    )


def test_answer_names_statistical_ties_and_judges_ci_width(tmp_path: Path) -> None:
    def ties(doc: dict) -> None:
        lb = sorted(doc["leaderboard"], key=lambda e: e["model_id"])
        _set_bias(lb[0], -0.001, -0.004, 0.003)  # the same row twice: the ranking tie breaks on Brier, then id
        _set_bias(lb[1], -0.001, -0.004, 0.003)
        _set_bias(lb[2], 0.004, -0.001, 0.009)
        for e in lb[3:]:
            _set_bias(e, 0.1, 0.05, 0.15)

    d = _zoo_with(tmp_path, ties)
    _, html = _render(tmp_path / "a", [d])
    a = _answers(html)
    doc = rc.json_load(d / "summary.json")
    ids = sorted(e["model_id"] for e in doc["leaderboard"])
    tied = sorted(ids[:2], key=lambda m: (next(e for e in doc["leaderboard"] if e["model_id"] == m)["brier"][H60], m))
    assert a[0]["label"] == "WITHIN NOISE" and a[0]["cls"] == "good"
    assert f"closest model by point estimate is {tied[0]}, which" in a[0]["s"]
    assert "its 95% CI contains 0 and lies within ±2.0 pp." in a[0]["s"]
    assert f" 3 of 6 models have a 95% CI that contains 0: {tied[0]}, {tied[1]}, {ids[2]}." in a[0]["s"]
    assert a[1]["label"] == "OPTIMISTIC MODELS" and a[1]["s"].startswith("Yes: 3 of 6 models with a 95% CI fill")

    def wide(doc: dict) -> None:  # contains 0, but one end is past the margin: underpowered, not accurate
        lb = doc["leaderboard"]
        for e in lb:
            _set_bias(e, 0.3, 0.2, 0.4)
        _set_bias(lb[0], 0.0, -0.05, 0.05)

    _, html = _render(tmp_path / "b", [_zoo_with(tmp_path / "b", wide)])
    a = _answers(html)
    assert a[0]["label"] == "INCONCLUSIVE (WIDE CI)" and "It is the only one of the 6 models" in a[0]["s"]

    def biased(doc: dict) -> None:
        for i, e in enumerate(doc["leaderboard"]):
            _set_bias(e, 0.05 + 0.01 * i, 0.04 + 0.01 * i, 0.06 + 0.01 * i)

    _, html = _render(tmp_path / "c", [_zoo_with(tmp_path / "c", biased)])
    a = _answers(html)
    assert a[0]["label"] == "BIASED" and "; even the closest model is biased." in a[0]["s"]
    assert "None of the 6 models has a 95% CI that contains 0." in a[0]["s"]


def test_answer_points_at_zoo_warnings_naming_the_closest_model(tmp_path: Path) -> None:
    holder: dict[str, str] = {}

    def warned(doc: dict) -> None:
        lb = sorted(doc["leaderboard"], key=lambda e: e["model_id"])
        _set_bias(lb[0], 0.0, -0.004, 0.004)
        for e in lb[1:]:
            _set_bias(e, 0.1, 0.05, 0.15)
        holder["m"] = lb[0]["model_id"]
        doc["warnings"] = [
            f"hbt_312_abort: {lb[0]['model_id']}: 261 of 1000 shadow pair(s) aborted",
            f"{lb[0]['model_id']}x: unrelated model with a longer id",
            "unrelated warning",
        ]

    _, html = _render(tmp_path / "a", [_zoo_with(tmp_path, warned)])
    a = _answers(html)[0]
    assert a["s"].endswith(f" 1 zoo warning(s) name {holder['m']}; see the zoo drill-down.")


def test_warnings_naming_matches_whole_model_ids() -> None:
    ws = [
        "hbt_312_abort: hbt_power[n=1]: 5 of 9 shadow pair(s)",
        "hbt_power3[n=1] diverged",
        "model hbt_power ran without parameters",
        None,
    ]
    assert warnings_naming(ws, "hbt_power[n=1]") == 1  # type: ignore[arg-type]
    assert warnings_naming(ws, "hbt_power3[n=1]") == 1  # type: ignore[arg-type]
    assert warnings_naming(ws, "hbt_power") == 1  # type: ignore[arg-type]
    assert warnings_naming(None, "x") == 0 and warnings_naming([], "x") == 0


def test_answer_when_no_shadow_is_labelled_at_the_sort_horizon(tmp_path: Path) -> None:
    def unlabelled(doc: dict) -> None:
        for e in doc["leaderboard"]:
            e["fill_rate_bias"][H60] = {"value": None, "ci_lo": None, "ci_hi": None}

    _, html = _render(tmp_path / "a", [_zoo_with(tmp_path, unlabelled)])
    a = _answers(html)
    assert len(a) == 1 and a[0]["label"] == "NO LABELS" and a[0]["cls"] == "muted"
    assert a[0]["s"] == "No shadow has an exact label at 60 s, so no model's fill-rate bias can be measured there."


def test_answer_scorecard_states(tmp_path: Path) -> None:
    def edit(name: str, fn: Any) -> list[dict[str, str]]:
        root = tmp_path / name / "score"
        rc.make_score_dir(root, n=200, with_why=False)
        doc = rc.json_load(root / "summary.json")
        fn(doc)
        (root / "summary.json").write_text(json.dumps(doc), encoding="utf-8")
        return [a for a in _answers(_render(tmp_path / name, [root], max_why=0)[1]) if a["href"] == "#verdict"]

    def unscored(doc: dict) -> None:
        doc["n_matched"] = 0

    def blocked(doc: dict) -> None:
        doc["verdict"]["failed_gates"] = ["own_orders_in_l2_depth"]

    def fills_only(doc: dict) -> None:
        doc["fills_only"] = True

    def equivalent(doc: dict) -> None:
        doc["spec"].pop("gates", None)
        doc["verdict"]["failed_gates"] = []
        doc["verdict"]["gates_passed"] = True
        doc["bias"]["direction"] = "no_significant_bias"
        doc["equivalence"]["fill_rate_equivalent"] = True
        doc["equivalence"]["tost_p"] = 0.0  # printed as "<1e-300": the sentence must not read "p = <1e-300"

    def underfills(doc: dict) -> None:
        equivalent(doc)
        doc["bias"]["direction"] = "sim_underfills"
        doc["equivalence"]["fill_rate_equivalent"] = False

    def overfills(doc: dict) -> None:
        underfills(doc)
        doc["bias"]["direction"] = "sim_overfills"

    def small_overfill(doc: dict) -> None:  # significant (CI above 0) but inside the TOST margin: not red
        equivalent(doc)
        doc["bias"].update(direction="sim_overfills", fill_rate_bias=0.003, ci_lo=0.001, ci_hi=0.005)

    def inconclusive(doc: dict) -> None:
        equivalent(doc)
        doc["equivalence"]["fill_rate_equivalent"] = False

    got = {name: edit(name, fn) for name, fn in [("u", unscored), ("b", blocked), ("f", fills_only)]}
    assert got["u"][0]["label"] == "NOT SCORED" and "none of the 200 live orders" in got["u"][0]["s"]
    assert got["b"][0]["label"] == "BLOCKED" and got["b"][0]["cls"] == "crit"
    assert got["f"][0]["label"] == "FILLS ONLY" and "Not answerable for fill rates" in got["f"][0]["s"]
    eq = edit("e", equivalent)[0]
    assert eq["label"] == "EQUIVALENT" and eq["cls"] == "good" and "; equivalent within ±" in eq["s"]
    assert "(TOST p <1e-300)." in eq["s"] and "= <" not in eq["s"]
    assert edit("w", underfills)[0]["label"] == "SIM UNDERFILLS"
    o = edit("o", overfills)[0]
    assert o["label"] == "SIM OVERFILLS" and o["cls"] == "crit" and "not shown equivalent within ±2.0 pp." in o["s"]
    so = edit("s", small_overfill)[0]
    assert so["label"] == "BIASED, WITHIN MARGIN" and so["cls"] == "warn"
    assert "+0.3 pp (95% CI +0.1 pp to +0.5 pp)" in so["s"] and "; equivalent within ±2.0 pp (TOST p " in so["s"]
    inc = edit("i", inconclusive)[0]
    assert inc["label"] == "INCONCLUSIVE" and "not shown equivalent within ±" in inc["s"]


def test_answer_referee_and_none_for_a_position_table(tmp_path: Path, repo_root: Path) -> None:
    if not (repo_root / "tests" / "fixtures" / "referee" / "02_level_shrink_caps_queue").is_dir():
        pytest.skip("referee fixtures missing (make fixtures)")
    from report.test_report_audit import _referee_dir

    a = _referee_dir(tmp_path, repo_root, "16_crossed_at_live_takes_opposite", break_it=False)
    b = _referee_dir(tmp_path, repo_root, "02_level_shrink_caps_queue", break_it=True)
    _, html = _render(tmp_path / "r", [a, b])
    ans = _answers(html)
    assert len(ans) == 1 and ans[0]["href"] == "#referee"
    assert ans[0]["q"] == "Does toysim 1.0 reproduce the exact FIFO fills?"
    assert ans[0]["s"] == "No: 2 of 16 referee bug classes run, 1 failed: 02. level shrink caps queue."
    _, html = _render(tmp_path / "ok", [a])
    assert _answers(html)[0]["s"] == "Yes: 1 of 16 referee bug classes run, 0 failed."
    # a lone position table answers no question: no answer block and no skip link
    root = tmp_path / "pos"
    root.mkdir()
    rc.write(
        root,
        "position.parquet",
        "qt.score_position",
        {"arm": ["live", "sim"], "instrument_id": [7, 7], "ts": [rc.OPEN, rc.OPEN + 1], "position": [1, 1]},
    )
    summary, html = _render(tmp_path / "p", [root])
    assert "answer" not in summary.sections and 'class="skip"' not in html


def _referee_run(tmp: Path, repo_root: Path, name: str, dataset: str, sim: str, break_it: bool) -> Path:
    """A referee report on the class-16 fixture's expected fills, recorded against any `dataset` path and simulator
    (broken: the simulator filled nothing)."""
    import pyarrow.parquet as pq

    from queuetruth.score.referee import compare_referee

    fx = repo_root / "tests" / "fixtures" / "referee" / "16_crossed_at_live_takes_opposite"
    res = pq.read_table(fx / "expected_results.parquet")
    fills = pq.read_table(fx / "expected_fills.parquet")
    rep = compare_referee(
        res,
        fills,
        fills.slice(0, 0) if break_it else fills,
        {},
        spec={"dataset": {"path": dataset}},
        simulator={"name": sim, "version": "1.0"},
    )
    out = tmp / name
    out.mkdir(parents=True)
    (out / "referee_report.json").write_text(rep.model_dump_json(indent=2), encoding="utf-8")
    return out


def test_answer_referee_counts_runs_outside_the_bug_classes(tmp_path: Path, repo_root: Path) -> None:
    fx = repo_root / "tests" / "fixtures" / "referee" / "16_crossed_at_live_takes_opposite"
    if not fx.is_dir():
        pytest.skip("referee fixtures missing (make fixtures)")
    cls_pass = _referee_run(tmp_path, repo_root, "a", str(fx / "dataset"), "toysim", break_it=False)
    own_fail = _referee_run(tmp_path, repo_root, "b", "/data/my_own_day/dataset", "toysim", break_it=True)
    _, html = _render(tmp_path / "r1", [cls_pass, own_fail])
    (a,) = _answers(html)
    # a failing run on the user's own data is never dropped because another run resolved to a bug class
    assert a["label"] == "FAIL" and a["cls"] == "crit"
    assert a["s"].startswith("No: 1 of 16 referee bug classes run, 0 failed; 0 of 1 other referee run(s) pass, ")
    assert "orders disagree with the exact replay on whether they filled." in a["s"]
    # one answer per simulator: a second simulator's pass does not hide or merge with the first one's failure
    other = _referee_run(tmp_path, repo_root, "c", "/data/my_own_day/dataset", "othersim", break_it=False)
    _, html = _render(tmp_path / "r2", [own_fail, other])
    got = {x["q"]: (x["label"], x["s"]) for x in _answers(html)}
    assert got["Does othersim 1.0 reproduce the exact FIFO fills?"][0] == "PASS"
    assert got["Does othersim 1.0 reproduce the exact FIFO fills?"][1].startswith("Yes: 1 of 1 referee run(s) pass, 0 ")
    assert got["Does toysim 1.0 reproduce the exact FIFO fills?"][0] == "FAIL"


def _hazard_with(tmp: Path, edit: Any) -> Path:
    d = tmp / "hazard"
    rc.make_hazard_dir(d)
    doc = rc.json_load(d / "model.json")
    edit(doc)
    (d / "model.json").write_text(json.dumps(doc), encoding="utf-8")
    return d


def test_answer_learned_model_without_ci_or_privileged(tmp_path: Path) -> None:
    def no_ci(doc: dict) -> None:
        for v in doc["scores_test"]["bss_vs_climatology"].values():
            v["ci_lo"] = v["ci_hi"] = None

    (a,) = _answers(_render(tmp_path / "a", [_hazard_with(tmp_path / "a", no_ci)])[1])
    # a positive point estimate without an interval is neither skill nor its absence
    assert a["label"] == "NO CI" and a["cls"] == "info" and "has no 95% CI at any of its " in a["s"]
    assert "a point estimate that alone does not show skill." in a["s"]

    def one_without(doc: dict) -> None:
        bss = doc["scores_test"]["bss_vs_climatology"]
        v = bss[sorted(bss, key=int)[0]]
        v["ci_lo"] = v["ci_hi"] = None

    (a,) = _answers(_render(tmp_path / "b", [_hazard_with(tmp_path / "b", one_without)])[1])
    n = len(rc.json_load(tmp_path / "b" / "hazard" / "model.json")["scores_test"]["bss_vs_climatology"])
    assert a["label"] == "PARTLY JUDGED" and f"at {n - 1} of the {n - 1} horizons with a CI (1 has none)" in a["s"]

    def privileged(doc: dict) -> None:
        doc["privileged"] = True

    (a,) = _answers(_render(tmp_path / "c", [_hazard_with(tmp_path / "c", privileged)])[1])
    assert a["label"] == "UPPER BOUND (PRIVILEGED)" and a["cls"] == "info"
    assert a["s"].endswith(" It uses exact-queue features: an upper bound, not deployable.")

    def no_skill(doc: dict) -> None:
        for v in doc["scores_test"]["bss_vs_climatology"].values():
            v["ci_lo"] = -0.01

    (a,) = _answers(_render(tmp_path / "d", [_hazard_with(tmp_path / "d", no_skill)])[1])
    assert a["label"] == "NO SKILL" and a["cls"] == "crit"


def test_answer_data_quality_speaks_only_for_the_inputs_it_covers(tmp_path: Path) -> None:
    sc = tmp_path / "score"
    rc.make_score_dir(sc, n=120)
    doc = rc.json_load(sc / "summary.json")
    doc["data_quality"] = json.loads(rc.dq("clean").model_dump_json())
    (sc / "summary.json").write_text(json.dumps(doc), encoding="utf-8")
    zoo = tmp_path / "zoo"
    rc.make_zoo_dir(zoo, n=80)
    a = next(x for x in _answers(_render(tmp_path / "r", [sc, zoo])[1]) if x["href"] == "#data_quality")
    assert a["label"] == "CLEAN WHERE CHECKED" and a["cls"] == "info"
    assert a["s"] == f"Data-quality verdict clean for {sc}. {zoo} carries no data-quality verdict."
    _, html = _render(tmp_path / "s", [sc])
    a = next(x for x in _answers(html) if x["href"] == "#data_quality")
    assert a["label"] == "CLEAN" and a["cls"] == "good"


def test_compact_leaderboard_labels_the_base_row_and_never_rounds_a_significant_bias_to_zero(tmp_path: Path) -> None:
    h10 = str(10 * S)

    def tiny(doc: dict) -> None:
        lb = doc["leaderboard"]
        for e in lb:
            _set_bias(e, 0.1, 0.05, 0.15)
        _set_bias(lb[0], 0.0, -0.001, 0.001)
        lb[0]["fill_rate_bias"][h10] = {"value": 0.0003, "ci_lo": 0.0001, "ci_hi": 0.0005}

    d = _zoo_with(tmp_path, tiny)
    _, html = _render(tmp_path / "a", [d])
    doc = rc.json_load(d / "summary.json")
    assert h10 in doc["leaderboard"][0]["fill_rate_bias"]
    table = pp.section(html, "leaderboard").split("<table", 1)[1].split("</table>", 1)[0]
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S)
    base = next(r for r in rows if "exact fill rate (truth)" in r)
    cells = re.findall(r"<td[^>]*>([^<]*)</td>", base)
    # the bias columns of the base row carry the exact fill rate, labelled as such
    assert all(c == "" or c.startswith("exact ") or c.endswith("%") for c in cells) and any(
        c.startswith("exact ") for c in cells
    )
    mid = rc.esc(doc["leaderboard"][0]["model_id"]).replace("[", "<wbr>[").replace(";", ";<wbr>")  # wrap points
    row = next(r for r in rows if f'<th scope="row">{mid}</th>' in r)
    assert "+0.03 pp *" in row and "+0.0 pp *" not in row
    assert "Sorted by" not in table.split("<caption>", 1)[1].split("</caption>", 1)[0]


# ---- theme, print, accessibility ---------------------------------------------------------------------------------


def test_theme_switch_without_javascript(rendered: tuple[Any, str]) -> None:
    _, html = rendered
    radios = re.findall(r'<input type="radio" name="qt-theme" id="([^"]+)" value="(\w+)"( checked)?>', html)
    assert [(i, v, c) for i, v, c in radios] == [
        (THEME_IDS["auto"], "auto", " checked"),
        (THEME_IDS["light"], "light", ""),
        (THEME_IDS["dark"], "dark", ""),
    ]
    for i, _, _ in radios:
        assert f'<label for="{i}">' in html
    assert "<legend>Color theme</legend>" in html
    css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    assert f":root:has(#{THEME_IDS['light']}:checked)" in css and f":root:has(#{THEME_IDS['dark']}:checked)" in css
    assert "prefers-color-scheme: dark" in css


def test_print_never_uses_the_dark_tokens() -> None:
    css = page.CSS
    # every dark token block sits inside an `@media screen` rule, so print falls back to the light :root tokens
    for m in re.finditer(re.escape(DARK_TOKENS), css):
        opened = css.rfind("@media", 0, m.start())
        assert css.startswith("@media screen", opened), css[opened : opened + 40]
    pr = css[css.index("@media print") :]
    for hidden in ("nav.toc", "fieldset.theme", "button.copy", ".skip"):
        assert hidden in pr.split("{display:none}", 1)[0]
    assert "break-inside:avoid" in pr and "print-color-adjust:exact" in pr
    # provenance always prints: closed `prov` blocks are opened by the print stylesheet, and browsers without
    # `::details-content` print the hidden fallback copy instead
    assert "details.prov::details-content{content-visibility:visible;display:contents}" in pr
    assert "@supports not selector(::details-content){.print-prov[hidden]{display:block}}" in pr


def test_print_carries_the_inputs_hashes_and_limits(bundle: Any, rendered: tuple[Any, str]) -> None:
    _, html = rendered
    head = re.search(r'<header id="header"[^>]*>(.*?)</header>', html, re.S).group(1)
    assert '<details class="more prov" id="inputs"><summary><h2>Inputs</h2>' in head
    assert '<details class="more prov" id="limits"><summary><h2>Scope and limits</h2>' in head
    fb = re.search(r'<div class="print-prov" hidden>(.*?)</div>', head, re.S)
    assert fb
    limits = re.search(r'id="limits">.*?</summary>(.*?)</details>', head, re.S).group(1)
    assert limits in fb.group(1)  # the same statements
    for d in (bundle.score, bundle.zoo):
        for p in sorted(d.iterdir()):
            if p.suffix in (".json", ".parquet"):
                assert f"sha256 {rc.sha256(p)}" in fb.group(1)
    assert "Inputs (sha256)" not in "".join(_visible(html).text)  # the fallback copy is hidden on screen


def test_accessible_markup(rendered: tuple[Any, str]) -> None:
    summary, html = rendered
    assert '<html lang="en">' in html
    assert html.index('class="skip"') < html.index("<main>")
    for sid in summary.sections[1:]:
        m = re.search(rf'<section id="{sid}"[^>]*aria-labelledby="([^"]+)"', html)
        assert m and f'<h2 id="{m.group(1)}">' in html
    # heading levels never skip (h1 > h2 > h3 > h4) in document order
    levels = [int(x) for x in re.findall(r"<h([1-6])[ >]", html.split("<main>", 1)[1])]
    assert levels[0] == 1
    for a, b in itertools.pairwise(levels):
        assert b <= a + 1, (a, b)
    lb = pp.section(html, "leaderboard")
    first = lb.split("<table", 1)[1].split("</table>", 1)[0]
    assert "<caption>" in first and '<th scope="row">' in first and '<th scope="col"' in first
    # status is never color alone: every badge carries an icon (hidden from screen readers) and a text label
    for _cls, icon, label in re.findall(
        r'<span class="badge (\w+)"><span aria-hidden="true">([^<]+)</span> ([^<]+)</span>', html
    ):
        assert icon.strip() and label.strip()


def test_page_helpers() -> None:
    d = page.details("a<b", "<p>x</p>", note="n&m", sid="s1")
    assert (
        d
        == '<details class="more" id="s1"><summary>a&lt;b <span class="note">n&amp;m</span></summary><p>x</p></details>'
    )
    assert (
        page.details("t", ["<i>", "</i>"], cls="", is_open=True)
        == "<details open><summary>t</summary><i></i></details>"
    )
    assert page.details("x", "", summary_html="<h3>X</h3>", cls="item").startswith(
        '<details class="item"><summary><h3>X</h3></summary>'
    )
    t = page.table(["m", "v"], [("a", "1"), ("b<", "2")], numeric=(1,), row_header=True)
    assert '<tr><th scope="row">a</th><td class="n">1</td></tr>' in t and '<th scope="row">b&lt;</th>' in t
    assert '<th scope="row">' not in page.table(["m"], [("a",)])
    sec = page.section("x", "T", ["b"], lead="l", cls="c")
    assert sec == '<section id="x" class="c" aria-labelledby="x-h"><h2 id="x-h">T</h2><p class="lead">l</p>b</section>'
    shell = page.shell("t", "body", "d", skip_to="answer")
    assert '<body>\n<a class="skip" href="#answer">Skip to the answer</a>\n<main>' in shell
    assert '<a class="skip"' not in page.shell("t", "body", "d")
