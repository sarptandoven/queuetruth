# Answer-first HTML report (extension to SPEC 5.9)

Status: built (workstream `report-redesign`). This page amends SPEC 5.9; the integrator folds it into
`docs/design/spec.md`.

## Contract changes

The report keeps every 5.9 rule: one self-contained file, inline CSS and SVG, no external requests, complete without
JavaScript (the optional inline script still only adds copy buttons), a data table for every chart, at most 10 MB,
and byte-identical output for identical inputs and spec. What changes is the order and what is open by default.

Section order (`qt.report_summary.sections`), each present only when its input exists:

1. `header`: title, version line, a script-free theme switch, the provenance banner (real, vendor, assumed,
   synthetic, unknown), the integrity callout when a file changed. The inputs table with sha256 (`#inputs`) and the
   scope and limits list (`#limits`) are in the header, inside `<details>` blocks that are closed on screen and
   always printed.
2. `answer` (new): one plain sentence per question the inputs can answer, each with its base rate and 95% interval
   where the inputs carry them, a status badge (icon and text label) and a link to the section that backs it. Its
   lead states what truth is (the FIFO replay of zero-impact shadow orders) with a link to Scope and limits, and the
   number of summary warnings with a link to the warning list in `config`. Omitted when no input answers a question
   (for example a position table alone); the "skip to the answer" link is then omitted too.
3. `leaderboard` (new, split out of `zoo`): per zoo run with a leaderboard, the 5.6 ranking at the sort horizon in a
   compact table: the exact fill rate row first, then each model's fill rate and fill-rate bias with its 95% CI at
   the sort horizon, and its bias at every other horizon (`*` when the 95% CI excludes 0). Under it, closed: one
   sentence per model, and the full table (CIs at every horizon, eligible-row bias, overfill bias, cluster-robust
   discordance, McNemar, Brier, timing, the reading guide).
4. The drill-downs in the 5.9 order: `verdict` (scorecard), `data_quality`, `two_by_two`, `discordance`, `markouts`,
   `time_to_fill`, `position`, `zoo`, `calibration`, `learned_model`, `referee`, `why`, `config`.

Questions in the answer block, in order, asked once per run that answers them (the run label is appended when
several runs answer the same question; the referee question is asked once per simulator):

| Question | Input | Sentence carries | Badge |
|---|---|---|---|
| Which L2 queue model fills most like the exact replay? | zoo summary with a leaderboard | exact fill rate at the sort horizon, population size, the closest model by point estimate with its fill rate and bias and 95% CI, every model whose 95% CI contains 0 (the statistical ties), models without a CI, the number of zoo warnings that name the closest model | WITHIN NOISE (CI contains 0 and lies within the ±2 pp scorecard equivalence margin), INCONCLUSIVE (WIDE CI), BIASED, NO CI, NO LABELS |
| Would a backtest with these models overstate passive fills? | same | count of models with a CI whose 95% CI is above 0 (and below 0), the largest three by bias, models without a CI | OPTIMISTIC MODELS, NONE OPTIMISTIC, PARTLY JUDGED, NO CI |
| Does the simulator fill like live trading? | scorecard | window, sim and live fill rates from the 2x2, signed bias and CI, cluster-robust p, TOST equivalence | GATES FAILED; BIASED, WITHIN MARGIN (significant but TOST-equivalent); SIM OVERFILLS; SIM UNDERFILLS; EQUIVALENT; INCONCLUSIVE; NOT SCORED; BLOCKED; FILLS ONLY |
| Can I export the fitted parameters? | calibration result | estimate and bootstrap CI, events and shadows, the held-out comparison with the default | EXPORTABLE, NOT EXPORTABLE, NOT ASSESSED |
| Does the learned fill-time model beat the base rate? | hazard model card | held-out orders, horizons with a CI and those where the BSS CI is above 0, BSS and CI at 10 s (else the median horizon), privileged flag | BEATS BASE RATE, MIXED, PARTLY JUDGED, UPPER BOUND (PRIVILEGED), NO SKILL, NO CI |
| Does *simulator* reproduce the exact FIFO fills? | referee reports, grouped by simulator name and version | bug classes run and failed for runs on a fixture class directory; passing runs and discordant orders for every other run | PASS, FAIL (any run failed) |
| Is the data clean enough to trust these numbers? | scorecard, replay or data-quality verdicts | the inputs under each verdict, the first reason of the worst verdict, the analysed inputs that carry no verdict | CLEAN, CLEAN WHERE CHECKED, WARNINGS, UNUSABLE |

Absence of evidence is never shown as evidence: a missing 95% CI (for example a single bootstrap cluster) gives a
neutral NO CI badge and the sentence says no interval could be computed, never "within noise", "none optimistic" or
"no skill"; a CI that contains 0 but reaches beyond the margin is INCONCLUSIVE, not a pass; a significant scorecard
bias that TOST shows inside the margin is a warning, not a failure, so the badge never contradicts the sentence.

Every figure in an answer is read from the same fields the drill-down shows (`queuetruth.eval.leaderboard` helpers,
the scorecard's `two_by_two`, `bias`, `equivalence`, the calibration trust assessment), so the two cannot disagree.
When the input is synthetic or fixture data, the answer block says so in its lead. Sentences claim only what was
measured: an aggregate fill rate against the exact replay of the same shadow orders, not live orders or PnL.

Closed by default (`<details>` without `open`): the header's inputs table and scope list; the full leaderboard and
the per-model sentences; in the zoo drill-down, the forest plots and queue-decile heatmaps at every horizon except
the sort horizon, and the reliability diagrams of every model except the top-ranked one; every why trace (its summary
line is the index entry, with the order, reason and source); every summary's spec echo and recorded file hashes in
`config`; the cross-summary warning list (`#config-warnings`, its summary line a WARNINGS badge with the count); in
calibration, the stratified estimates and the export code; in position, every instrument after the first. Every
block is reachable from the table of contents and every internal link resolves to an element id on the page.

Themes and print: light tokens are the base; dark tokens apply under `@media screen and (prefers-color-scheme:
dark)`; a radio switch (Auto, Light, Dark) forces either theme through `:root:has(#qt-theme-...:checked)` without
script (a browser without `:has()` keeps the automatic theme). `data-theme` on `<html>` still forces a theme. Print
always uses the light tokens, hides the table of contents, the theme switch and the copy buttons, prints the blocks
that are open plus the header's inputs table with sha256 and the scope and limits list even when closed (class
`prov`, opened through `::details-content`; browsers without that selector print a hidden plain copy of the same
paths, hashes and statements instead), avoids breaks inside figures, callouts, answer items and table rows, and keeps
background colors. Below 760 px the sticky table of contents is one row that scrolls sideways, so its height stays
under the 52 px `scroll-margin-top` that keeps a jump target's heading clear of it.

Accessibility: a "skip to the answer" link first in the body; each section is a labelled region
(`aria-labelledby` on its `h2`); heading levels never skip in the markup; the leaderboard tables have captions,
column headers and row headers (`th scope="row"`); badges pair an icon (hidden from screen readers) with a text
label; the theme switch is a `fieldset` with a legend and labelled radios; focus is visible. Known limit: a heading
inside a `<summary>` (Inputs, Scope and limits, each config block and why trace) is exposed by Chromium and WebKit
as part of the disclosure button, so screen readers announce it as a button label rather than as a heading; the
outline is correct in the markup, not necessarily in what is announced.

## Measurements

Method: Google Chrome headless (`--headless=new`), the report loaded in a 1280 x 900 px iframe so the viewport is
exactly 900 px tall, `document.documentElement.scrollHeight` with no block expanded, divided by 900 px per screen
(the DX audit's 48,000 px was "about 53 screens"); printed pages from `--print-to-pdf --no-pdf-header-footer`
(Chrome's default paper size, margins from the page's `@page` rule) counted with `pdfinfo`. "Leaderboard table" is
the top of the first leaderboard table. Before = this branch's base (main, 0.1.1); after = this branch. Inputs: the
getting-started quickstart (`fixtures synth`, `shadows sample --n 1000`, `zoo`, `report --in qt-zoo`) and the real
2019-12-30 Nasdaq outputs in `.realdata` (`zoo/d1-sampled-faithful`; and that zoo run with `score/d1-exact` and
`calib/alloc-power3`).

| Report | Bytes before | Bytes after | Screens before | Screens after | Leaderboard table before | Leaderboard table after | Printed pages before | Printed pages after |
|---|---|---|---|---|---|---|---|---|
| Quickstart (synthetic, 16 models x 1,000 shadows) | 1,249,507 | 1,264,572 | 52.5 | 6.3 | 1.87 screens | 1.18 screens | 58 | 6 |
| Real day one, zoo only (17 models) | 1,213,761 | 1,229,308 | 28.9 | 6.5 | 2.16 screens | 1.12 screens | 37 | 8 |
| Real day one, zoo + scorecard + calibration | 1,390,332 | 1,410,738 | 42.7 | 16.6 | 11.61 screens | 1.59 screens | 54 | 19 |

The answer block starts 0.39 screens down in all three. Bytes grow about 1% (the compact leaderboard table sits next
to the full one, and the header carries a hidden print copy of the input hashes and limits); nothing is removed from
the page. The printed pages include the inputs table with every sha256 and the scope and limits list (checked with
`pdftotext`: 45 hashes in the combined report's PDF). Two renders of each report gave the same sha256 (quickstart
`6d271bd2...`, zoo only `5c2fb41e...`, combined `7ea1d8bd...`).

Narrow screens, same method with a 390 x 844 px and a 700 x 844 px iframe: the sticky table of contents is 47.5 px
tall at 390 px (one row that scrolls sideways, including its scrollbar) and 36.5 px at 700 px on the quickstart;
following every table-of-contents link puts the section heading 67 px (390 px) or 71 px (700 px) from the top,
below the nav. At 390 px the quickstart is 9,765 px tall (11.6 phone screens) and the combined report 24,440 px.
