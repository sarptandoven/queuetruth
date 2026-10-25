"""Order harness: plan, pricing, signing, the dry run end to end, failure paths and every live-use guard.

No test sends anything: the dry run uses the in-process venue, the live path is exercised against a fake
`urlopen`, and `socket` is blocked in the dry-run tests to prove no network is touched."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import socket
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path

import pytest
from livefill import cli, harness
from livefill.harness import (
    BitstampPrivate,
    DryRunVenue,
    FakeClock,
    HarnessConfig,
    HttpTransport,
    LiveAuthorization,
    MarketInfo,
    Response,
    StaticPublic,
    authorize_live,
    make_plan,
    order_terms,
    sign,
)

from queuetruth.errors import QtError

INFO = MarketInfo("usdtusd", 5, 5, Decimal("10"))
PUBLIC = StaticPublic(INFO, Decimal("0.99874"), Decimal("0.99875"))
ENV = {"BITSTAMP_API_KEY": "k-test", "BITSTAMP_API_SECRET": "s-test"}


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*a, **k):
        raise AssertionError("network access in a dry-run test")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)


def cfg(**kw) -> HarnessConfig:
    base = {"market": "usdtusd", "n_orders": 6, "seed": 3, "hold_mean_s": 5, "hold_max_s": 20, "gap_mean_s": 1}
    return HarnessConfig(**{**base, **kw})


N1, N2 = "0123abcd", "89ef4567"


def test_plan_is_deterministic_and_randomized() -> None:
    a, b = make_plan(cfg(), N1), make_plan(cfg(), N1)
    assert a == b
    c = make_plan(cfg(seed=4), N1)
    assert a != c
    assert len({p.client_order_id for p in a}) == len(a)
    assert all(p.client_order_id.startswith(f"qtlf-{harness.run_tag(cfg(), N1)}-") for p in a)
    # another run of the same plan: the same orders under new client order ids
    other = make_plan(cfg(), N2)
    assert [(p.side, p.notional, p.hold_s) for p in other] == [(p.side, p.notional, p.hold_s) for p in a]
    assert not {p.client_order_id for p in other} & {p.client_order_id for p in a}
    with pytest.raises(QtError):
        make_plan(cfg(), "not-hex!")
    big = make_plan(cfg(n_orders=400), N1)
    assert {p.side for p in big} == {"buy", "sell"}
    assert {p.ticks_behind for p in big} == {0, 1, 2}
    assert all(Decimal("12") <= p.notional <= Decimal("25") for p in big)
    assert all(0 <= p.hold_s <= 20 and 0 <= p.gap_s <= 30 for p in big)
    assert {p.side for p in make_plan(cfg(side="sell"), N1)} == {"sell"}


def test_order_terms_are_passive_and_rounded_down() -> None:
    p = make_plan(cfg(side="buy", ticks_behind=(2,)), N1)[0]
    price, amount = order_terms(p, INFO, Decimal("0.99874"), Decimal("0.99875"))
    assert price == Decimal("0.99872") and amount * price <= p.notional
    assert amount == (p.notional / price).quantize(Decimal("0.00001"), rounding="ROUND_DOWN")
    s = make_plan(cfg(side="sell", ticks_behind=(0,)), N1)[0]
    assert order_terms(s, INFO, Decimal("0.99874"), Decimal("0.99875"))[0] == Decimal("0.99875")
    for bid, ask in (("1", "1"), ("1.1", "1"), ("0", "1")):
        with pytest.raises(QtError):
            order_terms(s, INFO, Decimal(bid), Decimal(ask))


def test_config_validation() -> None:
    bad = [
        {"n_orders": 0},
        {"notional_min": Decimal("5"), "notional_max": Decimal("4")},
        {"ticks_behind": ()},
        {"ticks_behind": (-1,)},
        {"side": "both"},
        {"hold_mean_s": 0},
        {"max_open": 2},
        {"market": "BTC-USD"},
        {"max_net_position": Decimal("0")},
    ]
    for kw in bad:
        with pytest.raises(QtError):
            cfg(**kw)


def test_sign_matches_the_documented_string() -> None:
    url = "https://www.bitstamp.net/api/v2/buy/usdtusd/"
    h = sign("KEY", "SECRET", "POST", url, "amount=1&price=2", "n-1", 1700000000000)
    msg = (
        "BITSTAMP KEY" + "POST" + "www.bitstamp.net" + "/api/v2/buy/usdtusd/" + "" + "application/x-www-form-urlencoded"
        "n-1" + "1700000000000" + "v2" + "amount=1&price=2"
    )
    assert h["X-Auth-Signature"] == hmac.new(b"SECRET", msg.encode(), hashlib.sha256).hexdigest()
    assert h["X-Auth"] == "BITSTAMP KEY" and h["X-Auth-Version"] == "v2" and h["Content-Type"].startswith("app")
    empty = sign("KEY", "SECRET", "POST", url + "?a=1", "", "n-2", 5)
    msg2 = "BITSTAMP KEY" + "POST" + "www.bitstamp.net" + "/api/v2/buy/usdtusd/" + "a=1" + "n-2" + "5" + "v2"
    assert empty["X-Auth-Signature"] == hmac.new(b"SECRET", msg2.encode(), hashlib.sha256).hexdigest()
    assert "Content-Type" not in empty


def test_verify_response() -> None:
    body = '{"id": "1"}'
    good = hmac.new(b"S", ("n" + "5" + "application/json" + body).encode(), hashlib.sha256).hexdigest()
    ok = Response(200, {"Content-Type": "application/json", "X-Server-Auth-Signature": good}, body)
    assert harness.verify_response("S", "n", "5", ok)
    assert not harness.verify_response("S", "n", "6", ok)
    assert not harness.verify_response("S", "n", "5", Response(200, {"Content-Type": "application/json"}, body))


def test_dry_run_end_to_end(tmp_path: Path, no_network: None) -> None:
    c = cfg()
    clock = FakeClock()
    h, venue = harness.dry_run_harness(c, PUBLIC, tmp_path / "log.jsonl", clock)
    summary = h.run()
    assert summary["placed"] == 6 and summary["cancelled"] == 6 and summary["stopped"] is None
    assert not h.open
    paths = [r["path"] for r in venue.requests]
    assert paths.count("/api/v2/cancel_order/") == 6 and paths.count("/api/v2/order_status/") == 6
    # the preflight comes first, the open-orders sweep last
    assert paths[0] == "/api/v2/account_balances/" and paths[-1] == "/api/v2/open_orders/usdtusd/"
    assert all(r["headers"]["X-Auth"] == "BITSTAMP dry-run-key" for r in venue.requests)
    submits = [r for r in venue.requests if "/buy/" in r["path"] or "/sell/" in r["path"]]
    assert all(r["params"]["moc_order"] == "True" for r in submits)
    recs = harness.read_log(tmp_path / "log.jsonl")
    start = recs[0]
    assert start["mode"] == "dry_run" and start["market"] == "usdtusd" and len(start["plan"]) == 6
    types = [r["type"] for r in recs]
    assert types.count("submit") == 6 and types[-1] == "run_end"
    sub = next(r for r in recs if r["type"] == "submit")
    assert sub["body"]["id"].startswith("dry-") and sub["sent_ns"] < sub["recv_ns"]
    assert sub["client_order_id"].startswith(start["client_order_id_prefix"]) and len(start["nonce"]) == 8
    # the waits ran on the simulated clock
    assert clock.t_ns > 1_800_000_000_000_000_000 + 6 * 10**9
    with pytest.raises(QtError, match="exists"):
        harness.dry_run_harness(c, PUBLIC, tmp_path / "log.jsonl", clock)


def test_caps_skip_orders(tmp_path: Path, no_network: None) -> None:
    c = cfg(n_orders=5, max_total_notional=Decimal("40"), notional_min=Decimal("15"), notional_max=Decimal("15"))
    h, _ = harness.dry_run_harness(c, PUBLIC, tmp_path / "a.jsonl", FakeClock())
    s = h.run()
    assert s["placed"] == 2 and s["skipped_caps"] == 3
    low = cfg(n_orders=2, notional_min=Decimal("5"), notional_max=Decimal("6"))
    h2, venue = harness.dry_run_harness(low, PUBLIC, tmp_path / "b.jsonl", FakeClock())
    assert h2.run()["skipped_caps"] == 2 and not _submits(venue)
    reasons = [r["reason"] for r in harness.read_log(tmp_path / "b.jsonl") if r["type"] == "skip"]
    assert all("below the market minimum" in r for r in reasons)


def test_rejects_unknown_outcomes_stop_file_and_bad_signatures(tmp_path: Path, no_network: None) -> None:
    clock = FakeClock()
    h, venue = harness.dry_run_harness(cfg(), PUBLIC, tmp_path / "r.jsonl", clock)
    venue.fail["buy"] = Response(200, {}, json.dumps({"status": "error", "reason": "Insufficient funds"}))
    venue.fail["sell"] = Response(200, {}, json.dumps({"status": "error", "reason": "Insufficient funds"}))
    s = h.run()
    assert s["rejected"] >= 1
    # a timeout leaves the order's fate unknown: one status lookup, then the run stops
    h2, v2 = harness.dry_run_harness(cfg(side="buy"), PUBLIC, tmp_path / "u.jsonl", FakeClock())
    v2.fail["buy"] = TimeoutError("read timed out")
    s2 = h2.run()
    assert s2["unknown"] == 1 and s2["placed"] == 0 and s2["stopped"].startswith("unknown outcome")
    recs = harness.read_log(tmp_path / "u.jsonl")
    assert [r["type"] for r in recs if r["type"] in ("submit", "status")][:2] == ["submit", "status"]
    sub = next(r for r in recs if r["type"] == "submit")
    assert sub["outcome"] == "unknown" and "timed out" in sub["error"] and sub["recv_ns"] is None
    # an "ok" answer without an order id is not a placed order either
    h_noid, v_noid = harness.dry_run_harness(cfg(side="buy"), PUBLIC, tmp_path / "n.jsonl", FakeClock())
    v_noid.fail["buy"] = Response(200, {}, json.dumps({"datetime": "x"}))
    s_noid = h_noid.run()
    assert s_noid["unknown"] == 1 and s_noid["placed"] == 0
    # stop file: nothing placed
    stop = tmp_path / "STOP"
    stop.write_text("")
    h3, v3 = harness.dry_run_harness(cfg(), PUBLIC, tmp_path / "s.jsonl", FakeClock())
    h3.stop_file = stop
    assert h3.run()["stopped"] == f"stop file {stop}" and not _submits(v3)
    # a client signing with the wrong secret is refused by the venue simulator
    clock4 = FakeClock()
    venue4 = DryRunVenue(clock4)
    bad = BitstampPrivate(venue4, harness.DRY_KEY, "wrong-secret", clock4)
    call = bad.call("buy/usdtusd", {"amount": "1", "price": "1"})
    assert call.outcome == "error" and call.http_status == 403


def _submits(venue: DryRunVenue) -> list[dict]:
    return [r for r in venue.requests if "/buy/" in r["path"] or "/sell/" in r["path"]]


def _all_cancelled(venue: DryRunVenue) -> bool:
    return bool(venue.orders) and all(o["status"] == "Canceled" for o in venue.orders.values())


@pytest.mark.parametrize(
    "failure",
    [
        http.client.IncompleteRead(b"{"),  # the order is placed, the answer is cut off
        http.client.BadStatusLine("garbage"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        Response(502, {"Content-Type": "application/json"}, json.dumps({"status": "error", "reason": "Bad Gateway"})),
        Response(429, {}, json.dumps({"status": "error", "reason": "Too many requests"})),
    ],
)
def test_any_lost_answer_is_unknown_and_the_order_is_swept(tmp_path: Path, no_network: None, failure) -> None:
    """The venue accepts the order but its answer is lost or is not a definite rejection: the outcome is unknown, the
    submit is logged, the run stops and the sweep cancels the order (found by its client order id)."""
    h, venue = harness.dry_run_harness(cfg(side="buy"), PUBLIC, tmp_path / "x.jsonl", FakeClock())
    venue.fail["buy"] = failure
    venue.accept = {"buy"}
    s = h.run()
    assert s["unknown"] == 1 and s["rejected"] == 0 and s["stopped"].startswith("unknown outcome")
    assert _all_cancelled(venue) and len(venue.orders) == 1
    recs = harness.read_log(tmp_path / "x.jsonl")
    sub = next(r for r in recs if r["type"] == "submit")
    assert (
        sub["outcome"] == "unknown" and sub["client_order_id"] == next(iter(venue.orders.values()))["client_order_id"]
    )


def test_unknown_outcome_with_a_failed_status_lookup_is_still_swept(tmp_path: Path, no_network: None) -> None:
    """Submit and status lookup both time out, and so does the first cancel: the open-orders listing finds the order
    by this run's client order id prefix and cancels it."""
    h, venue = harness.dry_run_harness(cfg(side="buy"), PUBLIC, tmp_path / "x.jsonl", FakeClock())
    venue.fail = {
        "buy": TimeoutError("read timed out"),
        "order_status": [TimeoutError("status timed out")],
        "cancel_order": [TimeoutError("cancel timed out")],
    }
    venue.accept = {"buy"}
    s = h.run()
    assert s["unknown"] == 1 and s["swept"] == 1 and _all_cancelled(venue)
    # without a working listing, the unresolved order is cancelled by its client order id
    h2, v2 = harness.dry_run_harness(cfg(side="buy"), PUBLIC, tmp_path / "y.jsonl", FakeClock())
    v2.fail = {
        "buy": TimeoutError("read timed out"),
        "order_status": [TimeoutError("status timed out")],
        "cancel_order": [TimeoutError("cancel timed out")],
        "open_orders": TimeoutError("listing timed out"),
    }
    v2.accept = {"buy"}
    s2 = h2.run()
    assert s2["swept"] == 1 and _all_cancelled(v2)
    cancels = [r["params"] for r in v2.requests if r["path"] == "/api/v2/cancel_order/"]
    assert all(set(c) == {"client_order_id"} for c in cancels)


def test_preflight_failure_places_nothing(tmp_path: Path, no_network: None) -> None:
    h, venue = harness.dry_run_harness(cfg(), PUBLIC, tmp_path / "p.jsonl", FakeClock())
    venue.fail["account_balances"] = TimeoutError("timed out")
    s = h.run()
    assert s["stopped"].startswith("preflight failed") and not _submits(venue) and s["placed"] == 0


def test_caps_compare_the_exact_notional(tmp_path: Path, no_network: None) -> None:
    """On a market priced in whole dollars, 10 USD buys 0.00011959 BTC at 83,614: 9.99939826 USD, below the minimum
    of 10 (rounding the notional to the dollar tick would have let it through)."""
    btc = StaticPublic(MarketInfo("btcusd", 8, 0, Decimal("10")), Decimal("83614"), Decimal("83615"))
    c = HarnessConfig(
        market="btcusd",
        n_orders=2,
        notional_min=Decimal("10"),
        notional_max=Decimal("10"),
        side="buy",
        ticks_behind=(0,),
        gap_mean_s=0,
    )
    h, venue = harness.dry_run_harness(c, btc, tmp_path / "c.jsonl", FakeClock())
    assert h.run()["skipped_caps"] == 2 and not _submits(venue)
    reasons = [r["reason"] for r in harness.read_log(tmp_path / "c.jsonl") if r["type"] == "skip"]
    assert reasons and all("9.99939826 below the market minimum" in r for r in reasons)


def test_net_position_cap_counts_fills(tmp_path: Path, no_network: None) -> None:
    c = cfg(
        n_orders=3, side="buy", notional_min=Decimal("15"), notional_max=Decimal("15"), max_net_position=Decimal(20)
    )
    h, venue = harness.dry_run_harness(c, PUBLIC, tmp_path / "n.jsonl", FakeClock())
    venue.fill = {1}  # the first order fills in full before its cancel arrives
    s = h.run()
    amount = Decimal(next(iter(venue.orders.values()))["amount"])
    assert s["placed"] == 1 and s["skipped_caps"] == 2 and Decimal(s["net_position"]) == amount
    recs = harness.read_log(tmp_path / "n.jsonl")
    assert all("max_net_position" in r["reason"] for r in recs if r["type"] == "skip")
    final = [r for r in recs if r["type"] == "status" and r["body"].get("status") == "Finished"]
    assert final and Decimal(final[0]["body"]["transactions"][0]["fee"]) > 0


def test_exception_sweeps_open_orders(tmp_path: Path, no_network: None) -> None:
    class Boom(StaticPublic):
        n = 0

        def touch(self, market: str):
            self.n += 1
            if self.n == 2:
                raise RuntimeError("feed died")
            return super().touch(market)

    clock = FakeClock()
    h, venue = harness.dry_run_harness(cfg(), Boom(INFO, PUBLIC.bid, PUBLIC.ask), tmp_path / "x.jsonl", clock)
    # keep the first order open: its cancel fails once, so the sweep must cancel it
    venue.fail["cancel_order"] = Response(200, {}, json.dumps({"status": "error", "reason": "busy"}))
    with pytest.raises(RuntimeError, match="feed died"):
        h.run()
    assert all(o["status"] == "Canceled" for o in venue.orders.values())
    recs = harness.read_log(tmp_path / "x.jsonl")
    assert recs[-1]["type"] == "run_end" and "feed died" in recs[-1]["summary"]["stopped"]


def test_live_guard_refuses_every_incomplete_request() -> None:
    capped = cfg(max_order_notional=Decimal("30"), max_total_notional=Decimal("300"))
    phrase = harness.confirm_phrase("usdtusd")
    cases = [
        ({"live": False, "confirm": phrase, "cfg": capped, "env": ENV}, "--live"),
        ({"live": True, "confirm": None, "cfg": capped, "env": ENV}, "--confirm"),
        ({"live": True, "confirm": "place-real-orders-on-btcusd", "cfg": capped, "env": ENV}, "--confirm"),
        ({"live": True, "confirm": phrase, "cfg": capped, "env": {}}, "BITSTAMP_API_KEY"),
        ({"live": True, "confirm": phrase, "cfg": capped, "env": {**ENV, "BITSTAMP_API_SECRET": ""}}, "SECRET"),
        (
            {"live": True, "confirm": phrase, "cfg": capped, "env": {**ENV, "BITSTAMP_API_KEY": harness.DRY_KEY}},
            "placeholder",
        ),
        ({"live": True, "confirm": phrase, "cfg": cfg(), "env": ENV}, "max-order-notional"),
        (
            {
                "live": True,
                "confirm": phrase,
                "cfg": cfg(max_order_notional=Decimal("20"), max_total_notional=Decimal("99")),
                "env": ENV,
            },
            "exceeds",
        ),
        (
            {
                "live": True,
                "confirm": phrase,
                "cfg": cfg(max_order_notional=Decimal("30"), max_total_notional=Decimal("300"), maker_or_cancel=False),
                "env": ENV,
            },
            "maker-or-cancel",
        ),
    ]
    for kw, needle in cases:
        with pytest.raises(QtError, match=needle) as e:
            authorize_live(**kw)
        assert e.value.code == "QT-USAGE-001"
    auth = authorize_live(live=True, confirm=phrase, cfg=capped, env=ENV)
    assert "k-test" not in repr(auth) and auth.market == "usdtusd"
    with pytest.raises(QtError):
        LiveAuthorization(object(), "usdtusd", "k", "s", Decimal(1), Decimal(1))
    with pytest.raises(QtError):
        HttpTransport("not an authorization")  # type: ignore[arg-type]


def test_harness_modes_need_matching_transports(tmp_path: Path) -> None:
    clock = FakeClock()
    venue_client = BitstampPrivate(DryRunVenue(clock), harness.DRY_KEY, harness.DRY_SECRET, clock)
    with pytest.raises(QtError, match="live run needs"):
        harness.Harness(cfg(), venue_client, PUBLIC, tmp_path / "a.jsonl", clock, "live")
    capped = cfg(max_order_notional=Decimal("30"), max_total_notional=Decimal("300"))
    auth = authorize_live(live=True, confirm=harness.confirm_phrase("usdtusd"), cfg=capped, env=ENV)
    live_client = BitstampPrivate(HttpTransport(auth), "k-test", "s-test", clock)
    with pytest.raises(QtError, match="never uses HttpTransport"):
        harness.Harness(cfg(), live_client, PUBLIC, tmp_path / "b.jsonl", clock, "dry_run")
    with pytest.raises(QtError):
        harness.Harness(cfg(), venue_client, PUBLIC, tmp_path / "c.jsonl", clock, "paper")
    other = authorize_live(
        live=True,
        confirm=harness.confirm_phrase("btcusd"),
        cfg=cfg(market="btcusd", max_order_notional=Decimal("30"), max_total_notional=Decimal("300")),
        env=ENV,
    )
    with pytest.raises(QtError, match="authorization is for btcusd"):
        harness.live_harness(capped, other, PUBLIC, tmp_path / "d.jsonl")


def _fake_venue(seen: list, state: dict):
    """A fake `urlopen` for the live transport: checks every request signature with the test secret, keeps orders,
    and signs its answers (wrongly for the endpoints in `state["bad_sig"]`)."""

    class FakeResp:
        def __init__(self, status: int, body: str, headers: dict[str, str]) -> None:
            self.status, self._b, self.headers = status, body.encode(), FakeHeaders(headers)

        def read(self) -> bytes:
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeHeaders(dict):
        def items(self):  # type: ignore[override]
            return list(super().items())

    def fake_urlopen(req: urllib.request.Request, timeout: float):
        seen.append(req)
        hd = {k.lower(): v for k, v in req.header_items()}
        body = (req.data or b"").decode()
        params = dict(urllib.parse.parse_qsl(body))
        want = sign(
            "k-test", "s-test", req.get_method(), req.full_url, body, hd["x-auth-nonce"], int(hd["x-auth-timestamp"])
        )
        assert hd["x-auth-signature"] == want["X-Auth-Signature"]
        path = req.full_url.split("/api/v2/")[1]
        name = path.split("/")[0]
        orders = state.setdefault("orders", {})
        if name in ("buy", "sell"):
            state["n"] = state.get("n", 0) + 1
            oid = str(9_000 + state["n"])
            orders[oid] = {"id": oid, "status": "Open", "client_order_id": params.get("client_order_id", "")}
            doc: object = {"id": oid, "datetime": "2026-10-24 00:00:00.000000", "type": "0"}
        elif name == "cancel_order":
            o = orders.get(params.get("id", ""))
            if o is None or o["status"] != "Open":
                doc = {"status": "error", "reason": "Order not found"}
            else:
                o["status"] = "Canceled"
                doc = {"id": o["id"], "status": "Canceled"}
        elif name == "order_status":
            o = orders.get(params.get("id", "")) or next(
                (x for x in orders.values() if x["client_order_id"] == params.get("client_order_id")), None
            )
            doc = (
                {"status": "error", "reason": "Order not found"}
                if o is None
                else {"id": o["id"], "status": o["status"], "transactions": [], "client_order_id": o["client_order_id"]}
            )
        elif name == "open_orders":
            doc = [
                {"id": o["id"], "client_order_id": o["client_order_id"]}
                for o in orders.values()
                if o["status"] == "Open"
            ]
        else:
            doc = [{"currency": "usd", "total": "100", "available": "100", "reserved": "0"}]
        text = json.dumps(doc)
        secret = b"wrong" if name in state.get("bad_sig", ()) else b"s-test"
        sig = hmac.new(
            secret,
            (hd["x-auth-nonce"] + hd["x-auth-timestamp"] + "application/json" + text).encode(),
            hashlib.sha256,
        ).hexdigest()
        return FakeResp(200, text, {"Content-Type": "application/json", "X-Server-Auth-Signature": sig})

    return fake_urlopen


def _live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: dict, name: str):
    seen: list[urllib.request.Request] = []
    monkeypatch.setattr(urllib.request, "urlopen", _fake_venue(seen, state))
    monkeypatch.setattr(harness.SystemClock, "sleep", lambda self, s: None)
    capped = cfg(n_orders=2, max_order_notional=Decimal("30"), max_total_notional=Decimal("300"))
    auth = authorize_live(live=True, confirm=harness.confirm_phrase("usdtusd"), cfg=capped, env=ENV)
    h = harness.live_harness(capped, auth, PUBLIC, tmp_path / name)
    return h, seen


def _endpoints(seen: list) -> list[str]:
    return [r.full_url.split("/api/v2/")[1].split("/")[0] for r in seen]


def test_live_path_against_a_fake_urlopen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The live transport end to end, with urlopen replaced by a fake venue that checks every signature with the
    test secret and signs its responses: nothing leaves the process."""
    state: dict = {}
    h, seen = _live(tmp_path, monkeypatch, state, "live.jsonl")
    s = h.run()
    assert s["placed"] == 2 and s["cancelled"] == 2
    assert _endpoints(seen).count("cancel_order") == 2 and _endpoints(seen)[0] == "account_balances"
    assert harness.read_log(tmp_path / "live.jsonl")[0]["mode"] == "live"


def test_live_bad_server_signatures_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A submit answered with a server signature that does not verify is an unknown outcome: the order (id taken
    from the unverified body) is cancelled in the sweep. A preflight that does not verify stops the run before any
    order."""
    state: dict = {"bad_sig": {"buy", "sell"}}
    h, _ = _live(tmp_path, monkeypatch, state, "a.jsonl")
    s = h.run()
    assert s["unknown"] == 1 and s["placed"] == 0 and s["stopped"].startswith("unknown outcome")
    assert [o["status"] for o in state["orders"].values()] == ["Canceled"]
    sub = next(r for r in harness.read_log(tmp_path / "a.jsonl") if r["type"] == "submit")
    assert sub["outcome"] == "unknown" and sub["error"] == "server signature mismatch"
    state2: dict = {"bad_sig": {"account_balances"}}
    h2, seen2 = _live(tmp_path, monkeypatch, state2, "b.jsonl")
    s2 = h2.run()
    assert s2["stopped"].startswith("preflight failed") and not {"buy", "sell"} & set(_endpoints(seen2))


def test_cli_harness_guards_and_dry_run(tmp_path: Path, no_network: None, capsys, monkeypatch) -> None:
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    common = ["harness", "--market", "usdtusd", "--n-orders", "2", "--ref-bid", "0.99874", "--ref-ask", "0.99875"]
    assert cli.main([*common, "--log", str(tmp_path / "a.jsonl"), "--confirm", "x"]) == 2
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "QT-USAGE-001" and "--live" in err["message"]
    assert cli.main([*common, "--log", str(tmp_path / "b.jsonl"), "--live"]) == 2
    assert "dry-run only" in json.loads(capsys.readouterr().err)["message"]
    live = [
        "harness",
        "--market",
        "usdtusd",
        "--n-orders",
        "2",
        "--log",
        str(tmp_path / "c.jsonl"),
        "--live",
        "--confirm",
        "place-real-orders-on-usdtusd",
        "--max-order-notional",
        "30",
        "--max-total-notional",
        "60",
    ]
    assert cli.main(live) == 2
    assert "BITSTAMP_API_KEY" in json.loads(capsys.readouterr().err)["message"]
    assert not (tmp_path / "c.jsonl").exists()
    assert cli.main([*common, "--log", str(tmp_path / "d.jsonl"), "--fast"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry_run" and out["summary"]["placed"] == 2 and out["summary"]["dry_run_requests"] == 8


def test_market_info_from_markets_list() -> None:
    markets = [
        {
            "market_symbol": "usdtusd",
            "base_decimals": 5,
            "counter_decimals": 5,
            "minimum_order_value": "10.00000",
            "trading": "Enabled",
            "base_currency": "USDT",
        },
        {
            "market_symbol": "xyzusd",
            "base_decimals": 2,
            "counter_decimals": 2,
            "minimum_order_value": "10",
            "trading": "Disabled",
        },
    ]
    info = MarketInfo.from_markets(markets, "usdtusd")
    assert (info.tick, info.lot, info.base_currency) == (Decimal("0.00001"), Decimal("0.00001"), "usdt")
    with pytest.raises(QtError, match="Disabled"):
        MarketInfo.from_markets(markets, "xyzusd")
    with pytest.raises(QtError, match="not in the markets list"):
        MarketInfo.from_markets(markets, "abcusd")
