"""Order-placement harness for small randomized passive orders on Bitstamp. DRY-RUN BY DEFAULT.

What it does: one signed read-only call first (`account_balances`, the preflight: a live run aborts before any order
unless the venue answers it and its server signature verifies); then, per planned order, read the public best bid
and ask, place a maker-or-cancel limit order (`moc_order`) of a random notional at a random distance behind the
touch, keep it for a random time, cancel it, read its final status, wait a random gap. Every request and response is
written to a JSON-lines log with local nanosecond clocks; `matcher.py` joins that log with the public feed.

Client order ids are `qtlf-<plan tag>-<run nonce>-<index>`: the plan tag is a hash of the configuration (the plan is
a pure function of it and the seed), the nonce is drawn per run and logged in `run_start`, so a rerun of the same
command never reuses an id.

Safety design (`queuetruth`'s tooling never trades on its own; every live order is the account owner's act):

- The default mode is a dry run: the private endpoints are answered by `DryRunVenue`, an in-process simulator.
  Requests are fully built and signed (with a dummy key) but nothing is sent anywhere. Dry-run order ids are
  `dry-NNNNNN`, which can never match a real feed id.
- A live run needs all of: `live=True` (`--live`), the confirmation string `place-real-orders-on-<market>`
  (`--confirm`), the API key and secret in the environment (`BITSTAMP_API_KEY`, `BITSTAMP_API_SECRET`; never on the
  command line), explicit caps `max_order_notional` and `max_total_notional`, and maker-or-cancel orders.
  `authorize_live` checks them and returns the only object `HttpTransport` accepts; without it no code path can send
  a private request.
- Caps are enforced before every submit on the exact notional `price * amount`: at least `minimum_order_value`, at
  most `max_order_notional`, the running submitted notional at most `max_total_notional`, at most `max_open` orders
  open, and with `max_net_position` the net filled base amount (from the final `order_status` of each order) plus
  the new order's amount within that bound.
- Prices are passive by construction against the touch read just before the submit: a buy at most the best bid, a
  sell at least the best ask (then `ticks_behind` further away). The touch is a REST read and can be stale by the
  time the order arrives, so a live run always sends `moc_order`: the venue cancels an order that would take.
- Fail closed. Any failure to get a verified answer is an unknown outcome, never a rejection: a transport error of
  any kind (timeouts, `IncompleteRead`, a bad status line, an undecodable body), HTTP 408, 429 or 5xx, a body that
  is not JSON, or a server signature that does not verify. A submit with an unknown outcome stops the run: the order
  is looked up by `client_order_id`, kept as possibly open (under the id from the body when there is one) and
  cancelled in the sweep. The sweep runs on every exit (normal end, `STOP` file, exception): it cancels every order
  this run believes open, then lists the market's open orders and cancels every one whose `client_order_id` carries
  this run's prefix; when that listing fails it cancels each unresolved order by `client_order_id`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Protocol

from livefill import feed
from queuetruth.errors import QtError

HOST = "www.bitstamp.net"
CONTENT_TYPE = "application/x-www-form-urlencoded"
DRY_KEY = "dry-run-key"
DRY_SECRET = "dry-run-secret"
ENV_KEY = "BITSTAMP_API_KEY"
ENV_SECRET = "BITSTAMP_API_SECRET"
LOG_SCHEMA = "livefill.harness_log"
#: HTTP statuses that do not say whether a request took effect (besides every 5xx)
UNKNOWN_HTTP = (408, 429)


def confirm_phrase(market: str) -> str:
    """The exact `--confirm` string a live run needs."""
    return f"place-real-orders-on-{market}"


# -- clocks -----------------------------------------------------------------------------------------------------


class Clock(Protocol):
    def now_ns(self) -> int: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Wall clock and real sleeps."""

    def now_ns(self) -> int:
        return time.time_ns()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


@dataclass
class FakeClock:
    """A clock that advances only when slept on (tests, and dry runs with `--fast`)."""

    t_ns: int = 1_800_000_000_000_000_000
    step_ns: int = 1_000

    def now_ns(self) -> int:
        self.t_ns += self.step_ns
        return self.t_ns

    def sleep(self, seconds: float) -> None:
        self.t_ns += max(int(seconds * 1e9), 0)


# -- configuration and plan ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketInfo:
    """Trading rules of one market (public `GET /api/v2/markets/`)."""

    symbol: str
    base_decimals: int
    counter_decimals: int
    minimum_order_value: Decimal
    #: lower-case base currency code (the key of the base amount in `order_status` transactions)
    base_currency: str = ""
    #: lower-case counter currency code (the currency of notionals and of fees)
    counter_currency: str = ""

    @property
    def tick(self) -> Decimal:
        return Decimal(1).scaleb(-self.counter_decimals)

    @property
    def lot(self) -> Decimal:
        return Decimal(1).scaleb(-self.base_decimals)

    @classmethod
    def from_markets(cls, markets: list[dict[str, Any]], symbol: str) -> MarketInfo:
        for m in markets:
            if m.get("market_symbol") == symbol:
                if m.get("trading") not in (None, "Enabled"):
                    raise QtError("QT-USAGE-001", f"market {symbol}: trading is {m.get('trading')!r}")
                return cls(
                    symbol=symbol,
                    base_decimals=int(m["base_decimals"]),
                    counter_decimals=int(m["counter_decimals"]),
                    minimum_order_value=Decimal(str(m["minimum_order_value"])),
                    base_currency=str(m.get("base_currency", "")).lower(),
                    counter_currency=str(m.get("counter_currency", "")).lower(),
                )
        raise QtError("QT-USAGE-001", f"market {symbol}: not in the markets list")


@dataclass(frozen=True)
class HarnessConfig:
    """Experiment design. Notionals are in the counter currency (USD for usdtusd)."""

    market: str
    n_orders: int = 20
    notional_min: Decimal = Decimal("12")
    notional_max: Decimal = Decimal("25")
    #: distance behind the own-side touch, in ticks, drawn uniformly from this tuple
    ticks_behind: tuple[int, ...] = (0, 0, 0, 1, 2)
    hold_mean_s: float = 30.0
    hold_max_s: float = 180.0
    gap_mean_s: float = 5.0
    gap_max_s: float = 30.0
    side: str = "random"
    maker_or_cancel: bool = True
    max_open: int = 1
    max_order_notional: Decimal | None = None
    max_total_notional: Decimal | None = None
    #: bound on the absolute net filled amount in base units (buys minus sells), checked before each submit
    max_net_position: Decimal | None = None
    seed: int = 0
    min_request_interval_s: float = 0.25

    def __post_init__(self) -> None:
        object.__setattr__(self, "market", feed.check_market(self.market))
        if self.n_orders < 1:
            raise QtError("QT-USAGE-001", "n_orders must be >= 1")
        if not (Decimal(0) < self.notional_min <= self.notional_max):
            raise QtError("QT-USAGE-001", "need 0 < notional_min <= notional_max")
        if not self.ticks_behind or any(t < 0 for t in self.ticks_behind):
            raise QtError("QT-USAGE-001", "ticks_behind: non-negative tick counts")
        if self.side not in ("random", "buy", "sell"):
            raise QtError("QT-USAGE-001", "side: random, buy or sell")
        if self.hold_mean_s <= 0 or self.hold_max_s < self.hold_mean_s or self.gap_mean_s < 0:
            raise QtError("QT-USAGE-001", "need hold_mean_s > 0, hold_max_s >= hold_mean_s, gap_mean_s >= 0")
        if self.max_open != 1:
            raise QtError("QT-USAGE-001", "max_open: only 1 (one order at a time) is supported")
        if self.max_net_position is not None and self.max_net_position <= 0:
            raise QtError("QT-USAGE-001", "max_net_position must be positive")

    def to_doc(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: (str(v) if isinstance(v, Decimal) else list(v) if isinstance(v, tuple) else v) for k, v in d.items()}


@dataclass(frozen=True)
class PlannedOrder:
    index: int
    client_order_id: str
    side: str
    ticks_behind: int
    notional: Decimal
    hold_s: float
    gap_s: float


def plan_tag(cfg: HarnessConfig) -> str:
    """Short deterministic tag of the configuration (the plan is a pure function of it)."""
    h = hashlib.sha256(json.dumps(cfg.to_doc(), sort_keys=True).encode()).hexdigest()
    return h[:8]


def new_nonce() -> str:
    """A fresh per-run nonce (8 hex digits)."""
    return uuid.uuid4().hex[:8]


def run_tag(cfg: HarnessConfig, nonce: str) -> str:
    """`<plan tag>-<nonce>`: unique per run; every client order id of the run starts with `qtlf-<run tag>-`."""
    if not (len(nonce) == 8 and all(c in "0123456789abcdef" for c in nonce)):
        raise QtError("QT-USAGE-001", f"run nonce {nonce!r}: expected 8 lower-case hex digits")
    return f"{plan_tag(cfg)}-{nonce}"


def client_prefix(cfg: HarnessConfig, nonce: str) -> str:
    return f"qtlf-{run_tag(cfg, nonce)}-"


def make_plan(cfg: HarnessConfig, nonce: str) -> list[PlannedOrder]:
    """The randomized schedule: side, distance, notional (whole cents), hold and gap (exponential, capped) are a pure
    function of `cfg` (seeded); `nonce` only enters the client order ids."""
    rng = random.Random(cfg.seed)
    prefix = client_prefix(cfg, nonce)
    out = []
    lo, hi = int(cfg.notional_min * 100), int(cfg.notional_max * 100)
    for i in range(cfg.n_orders):
        side = cfg.side if cfg.side != "random" else rng.choice(("buy", "sell"))
        out.append(
            PlannedOrder(
                index=i,
                client_order_id=f"{prefix}{i:05d}",
                side=side,
                ticks_behind=rng.choice(cfg.ticks_behind),
                notional=Decimal(rng.randint(lo, hi)) / 100,
                hold_s=round(min(rng.expovariate(1 / cfg.hold_mean_s), cfg.hold_max_s), 3),
                gap_s=round(min(rng.expovariate(1 / cfg.gap_mean_s), cfg.gap_max_s), 3) if cfg.gap_mean_s else 0.0,
            )
        )
    return out


def order_terms(p: PlannedOrder, info: MarketInfo, best_bid: Decimal, best_ask: Decimal) -> tuple[Decimal, Decimal]:
    """(price, amount) of a planned order against the current touch: passive by construction (a buy at or below the
    best bid, a sell at or above the best ask), the amount rounded down to the lot."""
    if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
        raise QtError("QT-INPUT-002", f"{info.symbol}: unusable touch bid={best_bid} ask={best_ask}")
    if p.side == "buy":
        price = best_bid - info.tick * p.ticks_behind
    else:
        price = best_ask + info.tick * p.ticks_behind
    price = price.quantize(info.tick)
    if price <= 0:
        raise QtError("QT-INPUT-002", f"{info.symbol}: price {price} after {p.ticks_behind} ticks is not positive")
    amount = (p.notional / price).quantize(info.lot, rounding=ROUND_DOWN)
    return price, amount


# -- transports ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    text: str


class Transport(Protocol):
    def send(self, method: str, url: str, headers: dict[str, str], body: bytes) -> Response: ...


class LiveAuthorization:
    """Proof that `authorize_live` passed; the only key to `HttpTransport`."""

    __slots__ = ("api_key", "api_secret", "market", "max_order_notional", "max_total_notional")
    _token = object()

    def __init__(self, token: object, market: str, key: str, secret: str, max_order: Decimal, max_total: Decimal):
        if token is not LiveAuthorization._token:
            raise QtError("QT-USAGE-001", "LiveAuthorization is created by authorize_live only")
        self.market = market
        self.api_key = key
        self.api_secret = secret
        self.max_order_notional = max_order
        self.max_total_notional = max_total

    def __repr__(self) -> str:
        return f"LiveAuthorization(market={self.market!r}, key=***)"


def authorize_live(
    *,
    live: bool,
    confirm: str | None,
    cfg: HarnessConfig,
    env: Mapping[str, str] | None = None,
) -> LiveAuthorization:
    """Checks every live-run precondition; raises `QT-USAGE-001` naming the first one missing."""
    env = os.environ if env is None else env
    if not live:
        raise QtError("QT-USAGE-001", "live orders need --live (the default is a dry run)")
    want = confirm_phrase(cfg.market)
    if confirm != want:
        raise QtError("QT-USAGE-001", f"live orders need --confirm {want}")
    key, secret = env.get(ENV_KEY, ""), env.get(ENV_SECRET, "")
    if not key or not secret:
        raise QtError("QT-USAGE-001", f"live orders need {ENV_KEY} and {ENV_SECRET} in the environment")
    if key == DRY_KEY:
        raise QtError("QT-USAGE-001", f"{ENV_KEY} is the dry-run placeholder")
    if cfg.max_order_notional is None or cfg.max_total_notional is None:
        raise QtError("QT-USAGE-001", "live orders need explicit --max-order-notional and --max-total-notional")
    if cfg.notional_max > cfg.max_order_notional:
        raise QtError("QT-USAGE-001", "notional_max exceeds max_order_notional")
    if not cfg.maker_or_cancel:
        raise QtError(
            "QT-USAGE-001",
            "live orders must be maker-or-cancel: the touch is a REST read that can be stale on arrival, and a plain "
            "limit order at a stale touch can take liquidity (drop --no-moc)",
        )
    return LiveAuthorization(
        LiveAuthorization._token, cfg.market, key, secret, cfg.max_order_notional, cfg.max_total_notional
    )


class HttpTransport:
    """Real HTTPS requests (urllib). Constructible only with a `LiveAuthorization`."""

    def __init__(self, auth: LiveAuthorization, timeout_s: float = 10.0) -> None:
        if not isinstance(auth, LiveAuthorization):
            raise QtError("QT-USAGE-001", "HttpTransport needs a LiveAuthorization from authorize_live")
        self.timeout_s = timeout_s

    def send(self, method: str, url: str, headers: dict[str, str], body: bytes) -> Response:
        req = urllib.request.Request(url, data=body or None, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return Response(r.status, dict(r.headers.items()), r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return Response(e.code, dict(e.headers.items()), e.read().decode("utf-8", "replace"))


def sign(
    api_key: str,
    api_secret: str,
    method: str,
    url: str,
    body: str,
    nonce: str,
    timestamp_ms: int,
) -> dict[str, str]:
    """Bitstamp API v2 authentication headers. The signed string is `"BITSTAMP " + key + method + host + path +
    query + content_type + nonce + timestamp + "v2" + body`, with the content type left out (and no Content-Type
    header) when the body is empty; the signature is the hex HMAC-SHA256 under the secret."""
    u = urllib.parse.urlsplit(url)
    ctype = CONTENT_TYPE if body else ""
    msg = f"BITSTAMP {api_key}{method}{u.netloc}{u.path}{u.query}{ctype}{nonce}{timestamp_ms}v2{body}"
    sig = hmac.new(api_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    h = {
        "X-Auth": f"BITSTAMP {api_key}",
        "X-Auth-Signature": sig,
        "X-Auth-Nonce": nonce,
        "X-Auth-Timestamp": str(timestamp_ms),
        "X-Auth-Version": "v2",
    }
    if body:
        h["Content-Type"] = CONTENT_TYPE
    return h


def verify_response(api_secret: str, nonce: str, timestamp_ms: str, resp: Response) -> bool:
    """Checks `X-Server-Auth-Signature` = HMAC(nonce + timestamp + response content type + body)."""
    got = {k.lower(): v for k, v in resp.headers.items()}.get("x-server-auth-signature")
    if not got:
        return False
    ctype = {k.lower(): v for k, v in resp.headers.items()}.get("content-type", "")
    want = hmac.new(api_secret.encode(), f"{nonce}{timestamp_ms}{ctype}{resp.text}".encode(), hashlib.sha256)
    return hmac.compare_digest(want.hexdigest(), got.lower())


class DryRunVenue:
    """In-process answers for the private endpoints (no network). Checks each request's signature with the dry-run
    secret, keeps the orders it accepted and answers buy / sell / cancel_order / order_status / open_orders /
    account_balances the way the venue's documented responses are shaped. `fail` maps an endpoint name to an exception
    or a `Response` for one call, or to a list of them for successive calls (tests of the failure paths). `accept`
    (endpoint names) makes such a failure happen after the order was accepted, as a lost answer would. Orders whose
    number (1, 2, ...) is in `fill` are filled in full before their cancel arrives, with a fee of `fee_rate` of the
    notional in their `order_status` transactions."""

    def __init__(self, clock: Clock, secret: str = DRY_SECRET) -> None:
        self.clock = clock
        self.secret = secret
        self.requests: list[dict[str, Any]] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.by_client: dict[str, str] = {}
        self.fail: dict[str, Any] = {}
        self.accept: set[str] = set()
        self.fill: set[int] = set()
        self.fee_rate = Decimal("0.003")
        self._n = 0
        self._tid = 0

    def _sig_ok(self, method: str, url: str, headers: dict[str, str], body: str) -> bool:
        key = headers.get("X-Auth", "").removeprefix("BITSTAMP ")
        want = sign(key, self.secret, method, url, body, headers["X-Auth-Nonce"], int(headers["X-Auth-Timestamp"]))
        return hmac.compare_digest(want["X-Auth-Signature"], headers.get("X-Auth-Signature", ""))

    def send(self, method: str, url: str, headers: dict[str, str], body: bytes) -> Response:
        text = body.decode()
        path = urllib.parse.urlsplit(url).path
        params = dict(urllib.parse.parse_qsl(text))
        self.requests.append({"method": method, "path": path, "params": params, "headers": dict(headers)})
        name = path.strip("/").split("/")[2] if path.count("/") >= 3 else path
        if name in self.fail:
            f = self.fail[name]
            if isinstance(f, list):
                f = f.pop(0)
                if not self.fail[name]:
                    del self.fail[name]
            else:
                del self.fail[name]
            if name in self.accept:
                self._answer(name, path, params)
            if isinstance(f, Exception):
                raise f
            return f
        if not self._sig_ok(method, url, headers, text):
            return self._json(403, {"status": "error", "reason": "Invalid signature", "code": "API0005"})
        return self._answer(name, path, params)

    def _answer(self, name: str, path: str, params: dict[str, str]) -> Response:
        ts = self.clock.now_ns()
        dt = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts // 1_000_000_000)) + f".{ts // 1000 % 1_000_000:06d}"
        if name in ("buy", "sell"):
            self._n += 1
            oid = f"dry-{self._n:06d}"
            o = {
                "id": oid,
                "market": path.rstrip("/").split("/")[-1].upper(),
                "datetime": dt,
                "type": "0" if name == "buy" else "1",
                "price": params.get("price"),
                "amount": params.get("amount"),
                "client_order_id": params.get("client_order_id", ""),
            }
            self.orders[oid] = {**o, "status": "Open", "amount_remaining": params.get("amount"), "transactions": []}
            if o["client_order_id"]:
                self.by_client[o["client_order_id"]] = oid
            return self._json(200, o)
        if name == "cancel_order":
            oid = params.get("id") or self.by_client.get(params.get("client_order_id", ""), "")
            o = self.orders.get(oid)
            if o is not None and o["status"] == "Open" and int(oid.split("-")[1]) in self.fill:
                self._fill(o)
            if o is None or o["status"] != "Open":
                return self._json(200, {"status": "error", "reason": "Order not found", "code": "API0013"})
            o["status"] = "Canceled"
            keys = ("id", "amount", "price", "type", "market")
            return self._json(200, {**{k: o[k] for k in keys}, "status": "Canceled"})
        if name == "order_status":
            oid = params.get("id") or self.by_client.get(params.get("client_order_id", ""), "")
            o = self.orders.get(oid)
            if o is None:
                return self._json(200, {"status": "error", "reason": "Order not found", "code": "API0013"})
            return self._json(
                200,
                {
                    "id": o["id"],
                    "datetime": o["datetime"],
                    "type": o["type"],
                    "status": o["status"],
                    "market": o["market"],
                    "transactions": o["transactions"],
                    "amount_remaining": o["amount_remaining"],
                    "client_order_id": o["client_order_id"],
                },
            )
        if name == "open_orders":
            market = path.rstrip("/").split("/")[-1].upper()
            keys = ("id", "datetime", "type", "price", "amount", "client_order_id", "market")
            rows = [{k: o[k] for k in keys} for o in self.orders.values() if o["status"] == "Open"]
            return self._json(200, [r for r in rows if market in ("ALL", "OPEN_ORDERS", r["market"])])
        if name == "account_balances":
            return self._json(200, [{"currency": "usd", "total": "1000", "available": "1000", "reserved": "0"}])
        return self._json(404, {"status": "error", "reason": f"unknown endpoint {path}", "code": "API0000"})

    def _fill(self, o: dict[str, Any]) -> None:
        market = o["market"].lower()
        base, counter = market[:-3], market[-3:]
        price, amount = Decimal(o["price"]), Decimal(o["amount"])
        self._tid += 1
        o["status"] = "Finished"
        o["amount_remaining"] = "0"
        o["transactions"] = [
            {
                "tid": self._tid,
                "price": str(price),
                base: str(amount),
                counter: str(price * amount),
                "fee": str((price * amount * self.fee_rate).quantize(Decimal("0.00001"))),
                "datetime": o["datetime"],
                "type": 2,
            }
        ]

    @staticmethod
    def _json(status: int, doc: Any) -> Response:
        return Response(status, {"Content-Type": "application/json"}, json.dumps(doc))


# -- private API client -----------------------------------------------------------------------------------------


@dataclass
class Call:
    """One private request and its outcome; `outcome` is `ok`, `error` (the venue answered with a definite rejection)
    or `unknown` (no verified answer: the request may or may not have taken effect)."""

    endpoint: str
    params: dict[str, str]
    sent_ns: int
    recv_ns: int | None
    outcome: str
    http_status: int | None
    body: Any
    text: str | None
    error: str | None = None


class BitstampPrivate:
    """Signed private calls over a `Transport`."""

    def __init__(
        self,
        transport: Transport,
        api_key: str,
        api_secret: str,
        clock: Clock,
        nonce: Callable[[], str] = lambda: str(uuid.uuid4()),
        base_url: str = f"https://{HOST}",
        verify: bool = False,
        min_interval_s: float = 0.25,
    ) -> None:
        self.t = transport
        self.key = api_key
        self.secret = api_secret
        self.clock = clock
        self.nonce = nonce
        self.base = base_url.rstrip("/")
        self.verify = verify
        self.min_interval_s = min_interval_s
        self._last = 0

    def call(self, endpoint: str, params: dict[str, str]) -> Call:
        wait = self.min_interval_s - (self.clock.now_ns() - self._last) / 1e9
        if self._last and wait > 0:
            self.clock.sleep(wait)
        url = f"{self.base}/api/v2/{endpoint}/"
        body = urllib.parse.urlencode(params)
        sent = self.clock.now_ns()
        self._last = sent
        nonce = self.nonce()
        ts_ms = sent // 1_000_000
        headers = sign(self.key, self.secret, "POST", url, body, nonce, ts_ms)
        try:
            r = self.t.send("POST", url, headers, body.encode())
        except Exception as e:  # any failure to get an answer: the request may have taken effect
            return Call(endpoint, params, sent, None, "unknown", None, None, None, repr(e))
        recv = self.clock.now_ns()
        try:
            doc = json.loads(r.text)
        except (json.JSONDecodeError, TypeError, ValueError):
            return Call(endpoint, params, sent, recv, "unknown", r.status, None, r.text, "response is not JSON")
        if r.status in UNKNOWN_HTTP or r.status >= 500:
            return Call(endpoint, params, sent, recv, "unknown", r.status, doc, r.text, f"HTTP {r.status}")
        if self.verify and not verify_response(self.secret, nonce, str(ts_ms), r):
            return Call(endpoint, params, sent, recv, "unknown", r.status, doc, r.text, "server signature mismatch")
        bad = r.status >= 400 or (isinstance(doc, dict) and doc.get("status") == "error")
        return Call(endpoint, params, sent, recv, "error" if bad else "ok", r.status, doc, r.text)


# -- public data -------------------------------------------------------------------------------------------------


class PublicData(Protocol):
    def market_info(self, market: str) -> MarketInfo: ...

    def touch(self, market: str) -> tuple[Decimal, Decimal]: ...


class PublicRest:
    """Public REST market data (no credentials): `GET /api/v2/markets/` and `GET /api/v2/order_book/<m>/`."""

    def __init__(self, base_url: str = feed.REST_URL, timeout_s: float = 10.0) -> None:
        self.base = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _get(self, path: str) -> Any:
        req = urllib.request.Request(f"{self.base}{path}", headers={"User-Agent": "queuetruth-livefill/1"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            return json.loads(r.read().decode("utf-8"))

    def market_info(self, market: str) -> MarketInfo:
        return MarketInfo.from_markets(self._get("/api/v2/markets/"), market)

    def touch(self, market: str) -> tuple[Decimal, Decimal]:
        d = self._get(f"/api/v2/order_book/{market}/")
        return Decimal(d["bids"][0][0]), Decimal(d["asks"][0][0])


@dataclass
class StaticPublic:
    """Fixed market data (tests and offline dry runs)."""

    info: MarketInfo
    bid: Decimal
    ask: Decimal
    touches: list[tuple[Decimal, Decimal]] = field(default_factory=list)

    def market_info(self, market: str) -> MarketInfo:
        return self.info

    def touch(self, market: str) -> tuple[Decimal, Decimal]:
        return self.touches.pop(0) if self.touches else (self.bid, self.ask)


# -- the run -------------------------------------------------------------------------------------------------------


class HarnessLog:
    """Append-only JSON-lines log, flushed after every record."""

    def __init__(self, path: Path) -> None:
        self.path = path
        if path.exists() and path.stat().st_size:
            raise QtError("QT-USAGE-001", f"{path} exists; every run writes a new log")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = path.open("a")

    def write(self, rec: dict[str, Any]) -> None:
        self.f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
        self.f.flush()

    def close(self) -> None:
        self.f.close()


def _call_rec(kind: str, p: PlannedOrder | None, c: Call) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "type": kind,
        "i": p.index if p else None,
        "client_order_id": p.client_order_id if p else None,
        "endpoint": c.endpoint,
        "params": c.params,
        "sent_ns": c.sent_ns,
        "recv_ns": c.recv_ns,
        "outcome": c.outcome,
        "http_status": c.http_status,
        "body": c.body,
    }
    if c.error:
        rec["error"] = c.error
    return rec


class Harness:
    """Runs a plan against a private client (dry-run or live) and writes the log."""

    def __init__(
        self,
        cfg: HarnessConfig,
        private: BitstampPrivate,
        public: PublicData,
        log_path: Path,
        clock: Clock,
        mode: str,
        stop_file: Path | None = None,
        nonce: str | None = None,
    ) -> None:
        if mode not in ("dry_run", "live"):
            raise QtError("QT-USAGE-001", f"mode: dry_run or live, got {mode!r}")
        if mode == "live" and not isinstance(private.t, HttpTransport):
            raise QtError("QT-USAGE-001", "a live run needs the HttpTransport of a LiveAuthorization")
        if mode == "dry_run" and isinstance(private.t, HttpTransport):
            raise QtError("QT-USAGE-001", "a dry run never uses HttpTransport")
        self.cfg = cfg
        self.private = private
        self.public = public
        self.clock = clock
        self.mode = mode
        self.stop_file = stop_file
        self.nonce = nonce or new_nonce()
        self.prefix = client_prefix(cfg, self.nonce)
        self.log = HarnessLog(Path(log_path))
        self.submitted_notional = Decimal(0)
        #: net filled base amount of this run (buys minus sells), from final order statuses
        self.net_position = Decimal(0)
        self.info: MarketInfo | None = None
        #: venue order id (or `client:<client_order_id>` when the id is unknown) -> order, for orders possibly open
        self.open: dict[str, PlannedOrder] = {}
        #: orders whose submit outcome was never resolved
        self.unresolved: list[PlannedOrder] = []
        self.summary: dict[str, Any] = {
            "placed": 0,
            "rejected": 0,
            "cancelled": 0,
            "cancel_errors": 0,
            "unknown": 0,
            "skipped_caps": 0,
            "filled_base": "0",
            "net_position": "0",
            "swept": 0,
            "stopped": None,
        }

    def _call(self, endpoint: str, params: dict[str, str]) -> Call:
        """A private call that never raises: anything unexpected is an unknown outcome."""
        sent = self.clock.now_ns()
        try:
            return self.private.call(endpoint, params)
        except Exception as e:
            return Call(endpoint, params, sent, None, "unknown", None, None, None, repr(e))

    def _stop_reason(self) -> str | None:
        if self.stop_file is not None and self.stop_file.exists():
            return f"stop file {self.stop_file}"
        return None

    def _caps(self, p: PlannedOrder, notional: Decimal, amount: Decimal, info: MarketInfo) -> str | None:
        """Why the order may not be sent (None: it may). `notional` is the exact `price * amount`."""
        if notional < info.minimum_order_value:
            return f"notional {notional} below the market minimum {info.minimum_order_value}"
        cap = self.cfg.max_order_notional
        if cap is not None and notional > cap:
            return f"notional {notional} above max_order_notional {cap}"
        total = self.cfg.max_total_notional
        if total is not None and self.submitted_notional + notional > total:
            return f"total notional would reach {self.submitted_notional + notional} > max_total_notional {total}"
        net = self.cfg.max_net_position
        if net is not None:
            after = self.net_position + (amount if p.side == "buy" else -amount)
            if abs(after) > net:
                return f"net position would reach {after} (if filled) beyond max_net_position {net}"
        if len(self.open) >= self.cfg.max_open:
            return "max_open orders already open"
        return None

    def _preflight(self) -> str | None:
        """One signed read-only call; returns why the run must not start (None: it may)."""
        c = self._call("account_balances", {})
        self.log.write(_call_rec("preflight", None, c))
        if c.outcome != "ok":
            return f"preflight failed: account_balances outcome {c.outcome} ({c.error or c.http_status})"
        return None

    def run(self) -> dict[str, Any]:
        plan = make_plan(self.cfg, self.nonce)
        plan_doc = [{**asdict(p), "notional": str(p.notional)} for p in plan]
        self.log.write(
            {
                "type": "run_start",
                "schema": LOG_SCHEMA,
                "schema_version": 1,
                "mode": self.mode,
                "venue": "bitstamp",
                "market": self.cfg.market,
                "config": self.cfg.to_doc(),
                "plan_tag": plan_tag(self.cfg),
                "nonce": self.nonce,
                "run_tag": run_tag(self.cfg, self.nonce),
                "client_order_id_prefix": self.prefix,
                "plan_sha256": hashlib.sha256(json.dumps(plan_doc, sort_keys=True).encode()).hexdigest(),
                "plan": plan_doc,
                "local_ns": self.clock.now_ns(),
            }
        )
        try:
            info = self.info = self.public.market_info(self.cfg.market)
            self.log.write({"type": "market_info", **{k: str(v) for k, v in asdict(info).items()}})
            why = self._preflight()
            if why:
                self.summary["stopped"] = why
            for p in plan if not why else []:
                reason = self._stop_reason()
                if reason:
                    self.summary["stopped"] = reason
                    break
                if not self._one(p, info):
                    break
                if p.gap_s:
                    self.clock.sleep(p.gap_s)
        except Exception as e:
            self.summary["stopped"] = f"exception: {e!r}"
            self.log.write({"type": "error", "error": repr(e), "local_ns": self.clock.now_ns()})
            raise
        finally:
            self._sweep()
            self.summary["net_position"] = str(self.net_position)
            self.log.write({"type": "run_end", "summary": self.summary, "local_ns": self.clock.now_ns()})
            self.log.close()
        return self.summary

    def _one(self, p: PlannedOrder, info: MarketInfo) -> bool:
        """Places, holds and cancels one order; False stops the run."""
        bid, ask = self.public.touch(self.cfg.market)
        t_book = self.clock.now_ns()
        price, amount = order_terms(p, info, bid, ask)
        notional = price * amount  # exact (Decimal)
        self.log.write(
            {
                "type": "touch",
                "i": p.index,
                "best_bid": str(bid),
                "best_ask": str(ask),
                "local_ns": t_book,
            }
        )
        why = self._caps(p, notional, amount, info)
        if why:
            self.summary["skipped_caps"] += 1
            self.log.write({"type": "skip", "i": p.index, "reason": why, "local_ns": self.clock.now_ns()})
            return True
        params = {"amount": str(amount), "price": str(price), "client_order_id": p.client_order_id}
        if self.cfg.maker_or_cancel:
            params["moc_order"] = "True"
        self.submitted_notional += notional
        c = self._call(f"{p.side}/{self.cfg.market}", params)
        self.log.write({**_call_rec("submit", p, c), "side": p.side, "notional": str(notional)})
        if c.outcome == "error":
            self.summary["rejected"] += 1
            return True
        raw_id = c.body.get("id") if isinstance(c.body, dict) else None
        oid = str(raw_id) if raw_id not in (None, "") else None
        if c.outcome == "unknown" or not oid:
            self.summary["unknown"] += 1
            self.unresolved.append(p)
            # possibly open: keep it for the sweep under the id the body named, else under its client order id
            self.open[oid or f"client:{p.client_order_id}"] = p
            s = self._call("order_status", {"client_order_id": p.client_order_id})
            self.log.write(_call_rec("status", p, s))
            sid = s.body.get("id") if s.outcome == "ok" and isinstance(s.body, dict) else None
            if sid not in (None, ""):
                self.open.pop(f"client:{p.client_order_id}", None)
                self.open[str(sid)] = p
            self.summary["stopped"] = f"unknown outcome of order {p.client_order_id}"
            return False
        self.summary["placed"] += 1
        self.open[oid] = p
        self.clock.sleep(p.hold_s)
        self._cancel(oid, p)
        return True

    def _cancel(self, key: str, p: PlannedOrder | None) -> None:
        """Cancels an order (by venue id, or by client order id for `client:` keys) and reads its final status."""
        by = {"client_order_id": key.removeprefix("client:")} if key.startswith("client:") else {"id": key}
        c = self._call("cancel_order", by)
        self.log.write({**_call_rec("cancel", p, c), "order_id": by.get("id")})
        if c.outcome == "ok":
            self.summary["cancelled"] += 1
        else:
            self.summary["cancel_errors"] += 1
        s = self._call("order_status", by)
        self.log.write({**_call_rec("status", p, s), "order_id": by.get("id")})
        body = s.body if s.outcome == "ok" and isinstance(s.body, dict) else {}
        done = body.get("status") in ("Finished", "Canceled")
        if done and p is not None:
            self._account_fills(p, body)
        if done or c.outcome == "ok":
            self.open.pop(key, None)

    def _account_fills(self, p: PlannedOrder, status: dict[str, Any]) -> None:
        """Adds the order's filled base amount (its `order_status` transactions) to the net position."""
        info = self.info
        base = (info.base_currency if info else "") or self.cfg.market[:-3]
        filled = Decimal(0)
        for t in status.get("transactions") or []:
            v = t.get(base) if isinstance(t, dict) else None
            if v is not None:
                try:
                    filled += abs(Decimal(str(v)))
                except ArithmeticError:
                    continue
        if filled:
            self.net_position += filled if p.side == "buy" else -filled
            self.summary["filled_base"] = str(Decimal(self.summary["filled_base"]) + filled)

    def _sweep(self) -> None:
        """Cancels every order of this run that may still be open: the ones it believes open, then (listing the
        market's open orders) every order whose client order id carries this run's prefix; when that listing fails,
        each unresolved order by its client order id."""
        for key, p in list(self.open.items()):
            try:
                self._cancel(key, p)
            except Exception as e:
                self.log.write({"type": "error", "i": p.index, "order_id": key, "error": f"sweep: {e!r}"})
        by_coid = {p.client_order_id: p for p in make_plan(self.cfg, self.nonce)}
        lst = self._call(f"open_orders/{self.cfg.market}", {})
        self.log.write(_call_rec("open_orders", None, lst))
        if lst.outcome == "ok" and isinstance(lst.body, list):
            for o in lst.body:
                coid = str(o.get("client_order_id") or "") if isinstance(o, dict) else ""
                if coid.startswith(self.prefix) and o.get("id") not in (None, ""):
                    self.summary["swept"] += 1
                    self._cancel(str(o["id"]), by_coid.get(coid))
            return
        for p in self.unresolved:
            self.summary["swept"] += 1
            self._cancel(f"client:{p.client_order_id}", p)


def dry_run_harness(
    cfg: HarnessConfig, public: PublicData, log_path: Path, clock: Clock | None = None, nonce: str | None = None
) -> tuple[Harness, DryRunVenue]:
    """A harness wired to `DryRunVenue` (no network for private calls)."""
    clock = clock or SystemClock()
    venue = DryRunVenue(clock)
    private = BitstampPrivate(venue, DRY_KEY, DRY_SECRET, clock, min_interval_s=cfg.min_request_interval_s)
    return Harness(cfg, private, public, log_path, clock, "dry_run", nonce=nonce), venue


def live_harness(
    cfg: HarnessConfig, auth: LiveAuthorization, public: PublicData, log_path: Path, stop_file: Path | None = None
) -> Harness:
    """A harness that places REAL orders. Only reachable with a `LiveAuthorization`."""
    if auth.market != cfg.market:
        raise QtError("QT-USAGE-001", f"authorization is for {auth.market}, the run is for {cfg.market}")
    clock = SystemClock()
    private = BitstampPrivate(
        HttpTransport(auth),
        auth.api_key,
        auth.api_secret,
        clock,
        verify=True,
        min_interval_s=cfg.min_request_interval_s,
    )
    return Harness(cfg, private, public, log_path, clock, "live", stop_file=stop_file)


def read_log(path: str | Path) -> list[dict[str, Any]]:
    """Records of a harness log; the first must be its `run_start` (`QT-INPUT-002` otherwise)."""
    p = Path(path)
    try:
        lines = p.read_text().splitlines()
    except FileNotFoundError as e:
        raise QtError("QT-INPUT-001", f"{p}: not found") from e
    recs = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise QtError("QT-INPUT-002", f"{p} line {i + 1}: not JSON ({e.msg})") from e
    if not recs or recs[0].get("type") != "run_start" or recs[0].get("schema") != LOG_SCHEMA:
        raise QtError("QT-INPUT-002", f"{p}: not a {LOG_SCHEMA} (first record must be run_start)")
    return recs
