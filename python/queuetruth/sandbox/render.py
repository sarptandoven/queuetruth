"""Plain-text renderings of the sandbox documents, for agents that read tool text rather than structured content:
prices and sizes as decimals, times as local times of day. The structured documents stay the source of truth."""

from __future__ import annotations

from typing import Any

from queuetruth.sandbox._util import fmt_duration, fmt_fixed, local_time, tick_decimals

MAX_LEVELS_SHOWN = 10
MAX_ITEMS_SHOWN = 12


class Fmt:
    """Formatting of one instrument's prices, sizes and times."""

    def __init__(self, instrument: dict[str, Any]) -> None:
        self.i = instrument
        self.pd = tick_decimals(int(instrument["tick_size"]), int(instrument["price_exp"]))

    def px(self, v: int | None) -> str:
        """Raw price as a decimal with the tick's decimals."""
        return fmt_fixed(v, int(self.i["price_exp"]), self.pd)

    def sz(self, v: int | None) -> str:
        """Raw size as a decimal."""
        return fmt_fixed(v, int(self.i["size_exp"]))

    def t(self, ns: int | None) -> str:
        """Local time of day."""
        return "-" if ns is None else local_time(int(ns), int(self.i["midnight_utc_ns"]))

    def money(self, v: float | None) -> str:
        """Currency amount."""
        return "n/a" if v is None else f"{v:,.2f} {self.i['currency']}".strip()

    def level(self, lv: list[int] | tuple[int, int, int] | None) -> str:
        """`price:size(count)`."""
        if lv is None:
            return "-"
        return f"{self.px(lv[0])}:{self.sz(lv[1])}({lv[2]})"


def _order_line(f: Fmt, o: dict[str, Any]) -> str:
    what = (
        "MKT"
        if o["type"] == "market"
        else (f.px(o["price"]) if o["price"] is not None else f"peg{o['peg_offset_ticks']:+d}")
    )
    line = f"{o['order_id']} {o['side'].upper()} {f.sz(o['qty'])} @ {what} {o['tif']} {o['status']}"
    if o["filled_qty"] and (o["status"] != "filled" or o["filled_qty"] != o["qty"]):
        line += f" {f.sz(o['filled_qty'])} filled"
    if o["status"] == "working":
        line += f" queue_ahead {f.sz(o['queue_ahead'])} (rank {o['rank']}) level {f.sz(o['level_qty'])}"
    if o["cancel_sent_ns"] is not None and o["status"] in ("pending", "working"):
        line += f" (cancel sent {f.t(o['cancel_sent_ns'])})"
    if o["reason"]:
        line += f" [{o['reason']}]"
    return line


def observation_text(obs: dict[str, Any], instrument: dict[str, Any], end_ns: int | None = None) -> str:
    """Text of a `qt.sandbox_observation`."""
    f = Fmt(instrument)
    head = f"step {obs['step']} | {obs['time_local']}"
    if obs["visible_ns"] != obs["clock_ns"]:
        head += f" (market seen up to {f.t(obs['visible_ns'])})"
    head += f" | {instrument['symbol'] or instrument['instrument_id']}"
    if end_ns is not None:
        head += f" | episode ends {f.t(end_ns)}"
    if obs["done"]:
        head += " | DONE"
    b = obs["book"]
    lines = [head]
    if b["bid"] is None and b["ask"] is None:
        lines.append("book: empty")
    else:
        spread = "" if b["spread_ticks"] is None else f", spread {b['spread_ticks']} tick(s)"
        lines.append(f"book: bid {f.level(b['bid'])} | ask {f.level(b['ask'])}{spread}")
        lines.append("  bids " + " ".join(f.level(x) for x in b["bids"][:MAX_LEVELS_SHOWN]))
        lines.append("  asks " + " ".join(f.level(x) for x in b["asks"][:MAX_LEVELS_SHOWN]))
    ts = obs["trades_since_last"]
    if ts["n"]:
        vwap = "n/a" if ts["vwap_price"] is None else f"{ts['vwap_price']:.{f.pd + 2}f}"
        lines.append(
            f"prints since last step: {ts['n']} (volume {f.sz(ts['volume'])}, vwap {vwap}, high {f.px(ts['high'])}, "
            f"low {f.px(ts['low'])}, last {f.px(ts['last'])})"
        )
    else:
        lines.append("prints since last step: none")
    for x in obs["fills"][:MAX_ITEMS_SHOWN]:
        lines.append(
            f"FILL {x['order_id']} {x['side'].upper()} {f.sz(x['qty'])} @ {f.px(x['price'])} {x['liquidity']} "
            f"({x['reason']}) at {f.t(x['ts_event'])} fee {x['fee']:+.4f}"
        )
    if len(obs["fills"]) > MAX_ITEMS_SHOWN:
        lines.append(f"... {len(obs['fills']) - MAX_ITEMS_SHOWN} more fill(s)")
    shown = obs["orders"][:MAX_ITEMS_SHOWN]
    if shown:
        lines.append("orders: " + "; ".join(_order_line(f, o) for o in shown))
    lines.append(f"open orders {obs['n_open']} of {obs['n_orders']}")
    mo = [m for m in obs["markouts"] if m["mo_ticks"] is not None][:MAX_ITEMS_SHOWN]
    if mo:
        lines.append(
            "markouts: "
            + ", ".join(
                f"{m['order_id']}#{m['fill_seq']} {m['horizon_ns'] / 1e9:g}s {m['mo_ticks']:+.2f} ticks" for m in mo
            )
        )
    p = obs["position"]
    lines.append(
        f"position {f.sz(p['qty'])}, cash {f.money(p['cash'])}, fees {f.money(p['fees'])}, "
        f"mark {p['mark_price'] if p['mark_price'] is not None else 'n/a'} ({p['mark_source'] or 'none'}), "
        f"PnL net {f.money(p['pnl_net'])}"
    )
    lines.extend(f"note: {w}" for w in obs["warnings"])
    return "\n".join(lines)


def _example_price(doc: dict[str, Any]) -> int:
    b = doc["observation"]["book"]
    for lv in (b["bid"], b["ask"]):
        if lv is not None:
            return int(lv[0])
    return int(doc["instrument"]["tick_size"])


def session_text(doc: dict[str, Any]) -> str:
    """Text of a `qt.sandbox_session`."""
    i = doc["instrument"]
    f = Fmt(i)
    s = doc["spec"]
    name = "sandbox session" + (f" {doc['session_id']}" if doc["session_id"] else "")
    jitter = f" (+ up to {fmt_duration(s['jitter_ns'])})" if s["jitter_ns"] else ""
    lines = [
        f"{name} on {i['symbol']} (instrument {i['instrument_id']}, {i['venue']}) "
        f"{i['session_date']}: episode {f.t(doc['start_ns'])} to {f.t(doc['end_ns'])} {i['session_tz']}",
        f"tick {f.px(i['tick_size'])}, lot {f.sz(i['lot_size'])}, multiplier {i['contract_multiplier']:g}, "
        f"currency {i['currency']}; latency {fmt_duration(s['latency_ns'])}{jitter}, "
        f"feed latency {fmt_duration(s['feed_latency_ns'])}",
        f'send prices as decimal strings on the tick grid (e.g. "{f.px(_example_price(doc))}"), sizes as integers '
        f"in units of {i['lot_size']} lot(s)",
        "rules: " + " ".join(f"({k + 1}) {r}" for k, r in enumerate(doc["rules"])),
        observation_text(doc["observation"], i, doc["end_ns"]),
    ]
    return "\n".join(lines)


def ack_text(doc: dict[str, Any], instrument: dict[str, Any]) -> str:
    """Text of a `qt.sandbox_ack`."""
    f = Fmt(instrument)
    o = doc["order"]
    verb = "submitted" if doc["action"] == "submit" else "cancel sent for"
    if doc["duplicate"]:
        verb = "already submitted (same client_order_id)"
    line = f"{verb} at {f.t(doc['clock_ns'])}: {_order_line(f, o)}; log #{doc['log_seq']} {doc['log_hash'][:16]}"
    return "\n".join([line, *(f"note: {w}" for w in doc["warnings"])])


def scorecard_text(doc: dict[str, Any]) -> str:
    """Text of a `qt.sandbox_scorecard` or of its `qt.sandbox_finish` summary."""
    i = doc["instrument"]
    f = Fmt(i)
    p = doc["pnl"]
    c = doc["consistency"]
    allr = next(r for r in doc["fill_rates"] if r["order_class"] == "all")
    passive = next(r for r in doc["fill_rates"] if r["order_class"] == "passive_limit")
    if "n_fills" in doc:
        n_fills, n_maker = doc["n_fills"], doc["n_fills_maker"]
    else:
        n_fills, n_maker = len(doc["fills"]), sum(1 for x in doc["fills"] if x["liquidity"] == "maker")
    lines = [
        f"episode {doc['run_id']} on {i['symbol']} {i['session_date']}: PnL net {f.money(p['pnl_net'])} "
        f"(gross {f.money(p['pnl_gross'])}, fees {f.money(p['fees'])}; at the touch {f.money(p['pnl_net_at_touch'])}), "
        f"end position {f.sz(p['position_end'])}",
        f"{allr['n_orders']} orders ({allr['n_live']} reached the venue), {n_fills} fills "
        f"({n_maker} maker, {n_fills - n_maker} taker), filled {f.sz(allr['filled_qty'])} of "
        f"{f.sz(allr['qty_live'])}; passive limit fill rate "
        + ("n/a" if passive["rate_any"] is None else f"{100 * passive['rate_any']:.1f}%"),
    ]
    for a in doc["adverse_selection"]:
        if a["cost_ccy"] is not None:
            lines.append(
                f"adverse selection at {a['horizon_ns'] / 1e9:g}s: {f.money(a['cost_ccy'])} over {a['n_maker_fills']} "
                f"maker fills ({100 * (a['adverse_fraction'] or 0):.0f}% moved against you)"
            )
    lines.append(
        f"consistency with one full exact replay: {'EQUAL' if c['equal'] else 'DIFFERENT'} "
        f"({c['fills_matched']}/{c['fills_reported']} reported fills matched, {c['fills_after_last_cut']} after the "
        f"last cut; {c['statuses_checked'] - c['statuses_mismatched']}/{c['statuses_checked']} order states matched; "
        f"{c['window_replays']} window replays over {c['steps']} steps)"
    )
    cert = doc["certificate"]
    lines.append(f"certificate: log root {cert['log_root'][:16]}, scorecard sha256 {cert['scorecard_sha256'][:16]}")
    if doc.get("out_dir"):
        lines.append(f"full scorecard and order log written to {doc['out_dir']}")
    lines.extend(f"note: {w}" for w in doc["warnings"])
    return "\n".join(lines)


__all__ = ["Fmt", "ack_text", "observation_text", "scorecard_text", "session_text"]
