"""Terminal text and the self-contained HTML page of a `qt.check` document (HTML blocks from `queuetruth.report`)."""

from __future__ import annotations

from typing import Any

_ROWS = ("filled in the real queue", "not filled in the real queue")


def _cnt(x: int, n: int) -> str:
    return f"{x:,} ({100 * x / n:.0f}%)" if n else f"{x:,}"


def _orders(n: int) -> str:
    return f"{n:,} order" + ("" if n == 1 else "s")


def _secs(x: float | None) -> str:
    if x is None:
        return "n/a"
    if x >= 120:
        return f"{x / 60:.1f} min"
    if x >= 1:
        return f"{x:.1f} s"
    return f"{x * 1000:.0f} ms"


def _money(x: float | None, cur: str | None) -> str:
    from queuetruth.score.pnl_gap import money

    return "n/a" if x is None else f"{money(x)}{' ' + cur if cur else ''}"


def _table(header: list[str], rows: list[list[str]], right: set[int]) -> list[str]:
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]

    def line(r: list[str]) -> str:
        return "  ".join(
            str(c).rjust(w) if i in right else str(c).ljust(w) for i, (c, w) in enumerate(zip(r, widths, strict=True))
        ).rstrip()

    return [line(header), *(line(r) for r in rows)]


def two_by_two(doc: dict[str, Any]) -> tuple[list[str], list[list[str]]]:
    t, n = doc["two_by_two"], doc["n_checked"]
    header = ["", "real queue filled", "real queue did not"]
    rows = [
        ["backtest filled", _cnt(t["both"], n), _cnt(t["phantom"], n) + " phantom"],
        ["backtest did not", _cnt(t["real_only"], n), _cnt(t["neither"], n)],
    ]
    return header, rows


def symbol_rows(doc: dict[str, Any], limit: int) -> tuple[list[str], list[list[str]]]:
    cur = (doc.get("pnl") or {}).get("currency")
    header = ["symbol", "orders", "backtest filled", "real filled", "phantom", "P&L gap per order"]
    rows = [
        [
            s["symbol"],
            f"{s['orders']:,}",
            f"{s['backtest_filled']:,}",
            f"{s['real_filled']:,}",
            f"{s['phantom']:,}",
            _money(s.get("pnl_gap_per_order"), cur),
        ]
        for s in doc["symbols"][:limit]
    ]
    return header, rows


def text(doc: dict[str, Any], *, max_symbols: int = 5, max_examples: int = 3) -> str:
    """Answer first, then the 2x2 table, time to fill, the worst symbols and `why` commands."""
    out = [doc["answer"], ""]
    h, r = two_by_two(doc)
    out += _table(h, r, {1, 2})
    t = doc["time_to_fill"]
    if t["n_both"]:
        out += [
            "",
            f"Time to fill, {_orders(t['n_both'])} filled in both: backtest median {_secs(t['backtest_median_s'])}, "
            f"real queue median {_secs(t['real_median_s'])}.",
        ]
    if doc["symbols"]:
        h, r = symbol_rows(doc, max_symbols)
        n = len(doc["symbols"])
        title = "Symbols, most overfilled first" + (f" ({max_symbols} of {n}):" if n > max_symbols else ":")
        out += ["", title, *("  " + x for x in _table(h, r, {1, 2, 3, 4, 5}))]
    ex = [e for e in doc["examples"][:max_examples] if e.get("why")]
    if ex:
        out += ["", "Why did the real queue not fill these? Trace one:"]
        out += [f"  {e['symbol']} {e['side']} {e['price']} at {e['entry']}: {e['why']}" for e in ex]
    out += ["", f"Details: {doc['files']['check']} (full summary), {doc['files']['orders']} (one row per order)"]
    return "\n".join(out)


def html(doc: dict[str, Any], orders: list[dict[str, Any]]) -> str:
    """The self-contained report page."""
    from queuetruth.report.page import code_block, section, shell, table, ul

    cur = (doc.get("pnl") or {}).get("currency")
    body = [f'<header><h1>How wrong is this backtest?</h1><p class="lead" id="answer">{_esc(doc["answer"])}</p>']
    body.append(
        f"<p class='note'>{_esc(doc['backtester'])} backtest {_esc(', '.join(doc['backtest']))}, replayed in the "
        f"exact queue of {_esc(doc['dataset'])}; fills within {_esc(doc['window'])} of entry; P&amp;L marked to the "
        f"mid at {_esc(doc['pnl'].get('markout') or 'n/a')}.</p></header>"
    )
    h, r = two_by_two(doc)
    body.append(section("orders", "Order by order", [table(h, r, numeric=(1, 2), row_header=True)]))
    t = doc["time_to_fill"]
    if t["n_both"]:
        body.append(
            section(
                "ttf",
                "Time to fill",
                [
                    f"<p>{_orders(t['n_both'])} filled in both: backtest median {_esc(_secs(t['backtest_median_s']))}, "
                    f"real queue median {_esc(_secs(t['real_median_s']))}.</p>"
                ],
            )
        )
    if doc["symbols"]:
        h, r = symbol_rows(doc, len(doc["symbols"]))
        body.append(section("symbols", "Symbols, most overfilled first", [table(h, r, numeric=(1, 2, 3, 4, 5))]))
    ex = [e for e in doc["examples"] if e.get("why")]
    if ex:
        body.append(
            section(
                "why",
                "Why did the real queue not fill these?",
                [code_block("\n".join(e["why"] for e in ex), "trace one order")],
            )
        )
    head = ["symbol", "side", "price", "qty", "entry", "backtest", "real queue", f"P&L gap{' ' + cur if cur else ''}"]
    rows = [
        [
            o["symbol"],
            o["side"],
            o["price"] or "",
            o["qty"] or "",
            o["entry"] or "",
            _secs(o["backtest_fill_after_s"]) if o["backtest_filled"] else "no fill",
            _secs(o["real_fill_after_s"]) if o["real_filled"] else "no fill",
            _money(o["pnl_gap"], None),
        ]
        for o in orders[:500]
    ]
    more = f" (first 500 of {len(orders):,}; all in orders.csv)" if len(orders) > 500 else ""
    body.append(section("all", f"Every order{more}", [table(head, rows, numeric=(2, 3, 5, 6, 7))]))
    if doc.get("notes"):
        body.append(section("notes", "Notes", [ul(doc["notes"])]))
    return shell("queuetruth check", "\n".join(body), doc["answer"], skip_to="answer")


def _esc(s: Any) -> str:
    from queuetruth.report.fmt import esc

    return esc(s)
