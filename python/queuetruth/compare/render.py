"""Terminal and HTML views of a `qt.compare` document: the answer lines first, then the orders."""

from __future__ import annotations

from typing import Any

_HEAD = ["order", "symbol", "side", "qty", "price", "placed", "backtest", "live", "why"]


def _fill(r: dict[str, Any], arm: str) -> str:
    q = r.get(f"{arm}_filled_qty")
    if q in (None, "", "0") or str(q).strip("0.") == "":
        return "no fill"
    px = r.get(f"{arm}_fill_price")
    return f"{q} @ {px}" if px else str(q)


def _row(r: dict[str, Any]) -> list[str]:
    return [
        str(r.get("order") or ""),
        str(r.get("symbol") or ""),
        str(r.get("side") or ""),
        str(r.get("qty") or ""),
        str(r.get("live_price") or r.get("backtest_price") or ""),
        str(r.get("placed") or ""),
        _fill(r, "backtest"),
        _fill(r, "live"),
        str(r.get("why") or ""),
    ]


def differing(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The orders whose outcome differs between the arms, fills only one side made first."""
    rank = {"backtest_only": 0, "live_only": 1, "backtest_unpaired": 2}
    sel = [r for r in orders if r.get("outcome") in rank or r.get("category") not in ("match", "both", "neither")]
    sel = [r for r in sel if r.get("category") not in ("both_unfilled",)]
    return sorted(sel, key=lambda r: rank.get(str(r.get("outcome")), 3))


def text(doc: dict[str, Any], orders: list[dict[str, Any]], *, max_rows: int = 10) -> str:
    """The terminal report."""
    from queuetruth.cli import _table

    path = "paired by its reconciler" if doc["path"] == "reconcile" else "scored as the simulator arm"
    lines = list(doc["lines"])
    lines.append("")
    lines.append(f"{doc['backtester_label']} backtest against {_live(doc)}, {path}; times in {doc['tz']}.")
    diff = differing(orders)
    if diff:
        lines.append(f"orders that differ ({len(diff)} of {len(orders)}):")
        rows = [_row(r) for r in diff[:max_rows]]
        lines += ["  " + x for x in _table(_HEAD, rows)]
        if len(diff) > max_rows:
            lines.append(f"  ... {len(diff) - max_rows} more in {doc['files']['orders']}")
    else:
        lines.append(f"every one of the {len(orders)} orders has the same outcome in both runs")
    lines.append(f"files: {doc['files']['orders']}, {doc['files']['compare']}")
    return "\n".join(lines)


def _live(doc: dict[str, Any]) -> str:
    lv = doc["live"]
    if lv["kind"] == "canonical":
        return f"the live log {lv['log']}"
    if lv["kind"] == "bot":
        return f"the {lv['name']} bot's live run"
    return f"the {lv['name']} export (imported to {lv['log']})"


def html(doc: dict[str, Any], orders: list[dict[str, Any]]) -> str:
    """The self-contained report page."""
    from queuetruth.report.fmt import esc
    from queuetruth.report.page import section, shell, table, ul

    body = [f'<header><h1>My backtest against my live run</h1><p class="lead" id="answer">{esc(doc["answer"])}</p>']
    body.append(
        f"<p class='note'>{esc(doc['backtester_label'])} backtest {esc(', '.join(doc['backtest']))} against "
        f"{esc(_live(doc))}; times in {esc(doc['tz'])}.</p></header>"
    )
    body.append(section("answer-lines", "The answer", [ul(doc["lines"])]))
    head = [*_HEAD[:6], "backtest", "live", "outcome", "P&L gap", "why"]
    rows = [
        [
            *_row(r)[:8],
            str(r.get("outcome") or ""),
            "" if r.get("pnl_gap") is None else f"{r['pnl_gap']:+.4g}",
            str(r.get("why") or ""),
        ]
        for r in orders[:1000]
    ]
    more = f" (first 1,000 of {len(orders):,}; all in orders.csv)" if len(orders) > 1000 else ""
    body.append(section("orders", f"Every order{more}", [table(head, rows, numeric=(3, 4, 9))]))
    if doc.get("notes"):
        body.append(section("notes", "Notes", [ul(doc["notes"])]))
    return shell("queuetruth compare", "\n".join(body), doc["answer"], skip_to="answer")


__all__ = ["differing", "html", "text"]
