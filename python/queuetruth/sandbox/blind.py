"""Blind episodes: the agent plays an operator-pinned episode without being able to tell which instrument or day it is
on, so it cannot recall how that day went.

    from queuetruth.sandbox.blind import EpisodeBook

    book = EpisodeBook()                                   # the operator
    eid = book.pin({"dataset": {"path": "synth"}, "instrument_id": 1, "session_date": "2026-06-15",
                    "start": "13:35", "end": "14:35", "latency": "1ms"})
    book.save("episodes.json")                             # private: it holds the secrets

    with book.start(eid) as sb:                            # the agent knows only `eid`
        obs = sb.observe()                                 # times since the open, rebased prices
        sb.submit("buy", 2, price=sb.to_price(obs.book.bid[0]))
        sb.advance(by="5s")
        fin = sb.finish()                                  # blind aggregates for the agent
    card = sb.scorecard                                    # the operator's scorecard, in true values

Operator side. `EpisodeBook.pin(spec)` validates an ordinary `qt.sandbox_spec` (dataset, instrument, day, window,
settings) and returns an opaque episode id `ep_<16 hex>`; each episode gets a random 32-byte secret. The book is
saved as a `qt.sandbox_episodes` document, which only the operator may read. `start(episode_id)` opens the episode;
the agent never names a dataset, symbol or date.

Agent side. A `BlindSandbox` wraps an ordinary `Sandbox` and maps every value the agent can see through a
`BlindTransform`:

- times are ns since the session open (`time_local` reads `+HH:MM:SS.ffffff`), durations are unchanged;
- prices are moved by a whole number of ticks, `price_offset_ticks = base - reference`: `reference` is the price in
  ticks of the best bid (else the best ask, else the last print) in view when the episode opens, and `base` is drawn
  from the secret in `[BASE_MIN_TICKS, BASE_MAX_TICKS]`, so the first rebased price says nothing about the true
  level. The tick size and the fixed-point exponents are kept, so prices stay on the grid and orders make sense;
- sizes are unchanged; money that does not depend on the price level (PnL, fees, markouts in currency and ticks,
  execution edge) is unchanged, and cash, notional and every float price are rebased; basis points are null;
- the symbol, venue, date, rule set, time zone, dataset path and every id (dataset, spec, run, log hashes) are
  hidden. Ack receipts name the entries of the agent's own blind log, a hash chain of its own;
- the collar price of a market order is not shown (it is a fraction of the true price level);
- error messages are rewritten in blind units, and a message that would still name a hidden value is replaced by
  a generic one (`QT-*` code and `details.reason` kept).

Orders the agent sends are mapped back exactly (`price - offset`, `time + open`), so the operator's episode is the
same episode a non-blind agent sending the true values would play: the same orders, fills, log entries (but the
`open` entry, which carries the blind commitment) and scorecard numbers. Scoring is in true values: `scorecard` and
the files `finish(out=...)` writes are the operator's. The certificate's `blind` field and the log's `open` entry hold
`{episode_id, transform_sha256}`; `transform_sha256` is the sha256 of the transform with a salt from the secret, so it
commits to the transform without revealing it, and `reveal()` / `check_reveal` open it afterwards.

A pin whose fees have a basis-point part is refused: a fee proportional to the notional gives the price level away.
Design: docs/design/extensions/agent-sandbox.md (S.11).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from queuetruth._version import __version__
from queuetruth.errors import QtError
from queuetruth.sandbox._util import (
    config_error,
    digest,
    money,
    parse_duration,
    parse_time,
    rel_time,
    sha256_text,
    to_fixed,
    usage_error,
    wire,
)
from queuetruth.sandbox.models import (
    EPISODE_ID_PATTERN,
    SandboxAck,
    SandboxBlindFinish,
    SandboxBlindOrderLog,
    SandboxBlindSession,
    SandboxEpisodes,
    SandboxObservation,
    SandboxScorecard,
    SandboxSpec,
)
from queuetruth.sandbox.session import RULES, Sandbox, normalize_spec
from queuetruth.sandbox.tape import Contain, open_source

#: Range of the rebased reference price, in ticks.
BASE_MIN_TICKS = 1_000
BASE_MAX_TICKS = 9_000
#: `prev` of the first entry of an agent's blind log.
BLIND_GENESIS = sha256_text("queuetruth.sandbox.blind_order_log.v1")
#: Settings of the spec a blind agent is shown (the rest names the instrument-day).
VISIBLE_SETTINGS = (
    "latency_ns",
    "jitter_ns",
    "feed_latency_ns",
    "depth_levels",
    "max_trades",
    "fees",
    "markout_horizons_ns",
    "market_collar_bps",
    "max_order_qty",
    "max_position",
    "max_open_orders",
    "self_trade_prevention",
    "max_steps",
)
BLIND_RULES = (
    *RULES,
    "Blind episode: times are ns since the session open, prices are shifted by a hidden whole number of ticks (the "
    "tick size is real, so send prices on the grid you see), sizes and money are real; the instrument and the day "
    "are hidden.",
)
_TIME_TEXT = re.compile(r"[0-9]+:[0-9]{2}(:[0-9]{2})?")
_LONG_INT = re.compile(r"[0-9]{15,}")
_EPISODE = re.compile(EPISODE_ID_PATTERN)


def _derive(secret: bytes, label: str) -> bytes:
    return hmac.new(secret, label.encode("utf-8"), hashlib.sha256).digest()


def _real(raw: int, exp: int) -> Decimal:
    return Decimal(int(raw)).scaleb(exp)


def _shift_float(v: float | None, delta: Decimal) -> float | None:
    """A float price moved by an exact decimal amount (`None` stays `None`)."""
    return None if v is None else float(Decimal(repr(float(v))) + delta)


class BlindTransform:
    """The per-episode map between true and blind values (module docstring)."""

    def __init__(self, *, time_origin_ns: int, price_offset_ticks: int, tick_size: int, price_exp: int, salt: str):
        self.time_origin_ns = int(time_origin_ns)
        self.price_offset_ticks = int(price_offset_ticks)
        self.tick_size = int(tick_size)
        self.price_exp = int(price_exp)
        self.salt = salt
        #: The offset in raw fixed-point units.
        self.offset_raw = self.price_offset_ticks * self.tick_size

    @classmethod
    def derive(cls, secret_hex: str, *, origin_ns: int, reference_raw: int, tick_size: int, price_exp: int):
        """The transform of an episode from its secret and the reference price in view at its start."""
        secret = bytes.fromhex(secret_hex)
        tick = max(int(tick_size), 1)
        span = BASE_MAX_TICKS - BASE_MIN_TICKS + 1
        base = BASE_MIN_TICKS + int.from_bytes(_derive(secret, "price_base"), "big") % span
        ref_ticks = int(reference_raw) // tick
        return cls(
            time_origin_ns=origin_ns,
            price_offset_ticks=base - ref_ticks,
            tick_size=tick,
            price_exp=price_exp,
            salt=_derive(secret, "salt").hex(),
        )

    def reveal(self) -> dict[str, Any]:
        """The transform with its salt: what the commitment opens to."""
        return {
            "kind": "qt.sandbox_blind_transform.v1",
            "time_origin_ns": self.time_origin_ns,
            "price_offset_ticks": self.price_offset_ticks,
            "tick_size": self.tick_size,
            "price_exp": self.price_exp,
            "salt": self.salt,
        }

    def commitment(self) -> str:
        """sha256 of the canonical JSON of `reveal()`."""
        return digest(self.reveal())

    # true -> blind
    def t(self, ns: int | None) -> int | None:
        """A time as ns since the session open."""
        return None if ns is None else int(ns) - self.time_origin_ns

    def p(self, raw: int | None) -> int | None:
        """A raw price rebased."""
        return None if raw is None else int(raw) + self.offset_raw

    def p_float(self, v: float | None) -> float | None:
        """A float price (`avg_fill_price`, `mark_price`, `vwap_price`) rebased."""
        return _shift_float(v, _real(self.offset_raw, self.price_exp))

    # blind -> true
    def t_back(self, rel: int) -> int:
        """A time since the open as absolute ns."""
        return int(rel) + self.time_origin_ns

    def p_back(self, raw: int) -> int:
        """A rebased raw price as the true one."""
        return int(raw) - self.offset_raw


def check_reveal(reveal: dict[str, Any], transform_sha256: str) -> bool:
    """Whether a revealed transform opens the commitment of a blind certificate."""
    return digest(reveal) == transform_sha256


# ---- document transforms ------------------------------------------------------------------------------------------


def _level(tr: BlindTransform, lv: Any) -> Any:
    return None if lv is None else [tr.p(lv[0]), lv[1], lv[2]]


def _stats(tr: BlindTransform, s: dict[str, Any]) -> dict[str, Any]:
    return {
        **s,
        "vwap_price": tr.p_float(s["vwap_price"]),
        "high": tr.p(s["high"]),
        "low": tr.p(s["low"]),
        "last": tr.p(s["last"]),
        "first_ts": tr.t(s["first_ts"]),
        "last_ts": tr.t(s["last_ts"]),
    }


def blind_order(tr: BlindTransform, o: dict[str, Any]) -> dict[str, Any]:
    """An `OrderView` in blind units; a market order's collar price is not shown."""
    return {
        **o,
        "price": None if o["type"] == "market" else tr.p(o["price"]),
        "ts_submit": tr.t(o["ts_submit"]),
        "ts_activated": tr.t(o["ts_activated"]),
        "ts_done": tr.t(o["ts_done"]),
        "cancel_sent_ns": tr.t(o["cancel_sent_ns"]),
        "avg_fill_price": tr.p_float(o["avg_fill_price"]),
    }


def _fill(tr: BlindTransform, f: dict[str, Any]) -> dict[str, Any]:
    return {**f, "ts_event": tr.t(f["ts_event"]), "ts_known": tr.t(f["ts_known"]), "price": tr.p(f["price"])}


def _cash(tr: BlindTransform, cash: float, qty: int, size_exp: int, mult: float) -> float:
    """Cash of the same fills at rebased prices: `cash - qty * offset * multiplier` (exact in decimals)."""
    return float(Decimal(repr(float(cash))) - money(tr.offset_raw * int(qty), tr.price_exp + size_exp, mult))


def blind_observation(tr: BlindTransform, obs: dict[str, Any], *, size_exp: int, mult: float) -> dict[str, Any]:
    """A `qt.sandbox_observation` in blind units."""
    b = obs["book"]
    pos = obs["position"]
    return {
        **obs,
        "clock_ns": tr.t(obs["clock_ns"]),
        "visible_ns": tr.t(obs["visible_ns"]),
        "time_local": rel_time(tr.t(obs["clock_ns"])),
        "book": {
            **b,
            "ts_event": tr.t(b["ts_event"]),
            "bid": _level(tr, b["bid"]),
            "ask": _level(tr, b["ask"]),
            "bids": [_level(tr, x) for x in b["bids"]],
            "asks": [_level(tr, x) for x in b["asks"]],
        },
        "trades": [{**x, "ts_event": tr.t(x["ts_event"]), "price": tr.p(x["price"])} for x in obs["trades"]],
        "trades_since_last": _stats(tr, obs["trades_since_last"]),
        "session_trades": _stats(tr, obs["session_trades"]),
        "orders": [blind_order(tr, o) for o in obs["orders"]],
        "fills": [_fill(tr, f) for f in obs["fills"]],
        "markouts": [{**m, "mo_bps": None} for m in obs["markouts"]],
        "position": {
            **pos,
            "cash": _cash(tr, pos["cash"], pos["qty"], size_exp, mult),
            "mark_price": tr.p_float(pos["mark_price"]),
        },
    }


# ---- the operator's book ------------------------------------------------------------------------------------------


class EpisodeBook:
    """The operator's pinned blind episodes (module docstring). `pin` adds one, `save` / `load` keep them in a
    `qt.sandbox_episodes` file, `start` opens one for an agent by its id."""

    def __init__(self, pins: dict[str, dict[str, Any]] | None = None) -> None:
        #: episode id -> {"spec": spec dict, "secret": 64 hex}.
        self.pins: dict[str, dict[str, Any]] = dict(pins or {})

    def __contains__(self, episode_id: object) -> bool:
        return episode_id in self.pins

    @property
    def episode_ids(self) -> list[str]:
        """The pinned episode ids, in pinning order."""
        return list(self.pins)

    def pin(
        self,
        spec: SandboxSpec | dict[str, Any],
        *,
        secret: str | None = None,
        episode_id: str | None = None,
        contain: Contain | None = None,
    ) -> str:
        """Pins an episode and returns its opaque id. `spec` is an ordinary sandbox spec (friendly values allowed);
        it is validated against the dataset now and stored with absolute times. `secret` (64 hex) and `episode_id`
        default to fresh random values; give them only to reproduce a pin."""
        raw = spec.model_dump(mode="json") if isinstance(spec, BaseModel) else dict(spec)
        first = normalize_spec(raw, None)
        SandboxSpec.model_validate(first)
        src = open_source(
            first["dataset"]["path"],
            instrument_id=first.get("instrument_id"),
            symbol=first.get("symbol"),
            session_date=first.get("session_date"),
            contain=contain,
        )
        full = SandboxSpec.model_validate(normalize_spec(raw, int(src.instrument["midnight_utc_ns"])))
        fees = full.fees
        if fees.maker_bps or fees.taker_bps:
            raise config_error(
                "a blind episode cannot charge fees in basis points: a fee proportional to the notional gives the "
                "price level away",
                "use maker_per_unit / taker_per_unit",
            )
        eid = episode_id if episode_id is not None else "ep_" + secrets.token_hex(8)
        if not _EPISODE.fullmatch(eid):
            raise config_error(f"episode_id {eid!r} is not ep_ and 16 lower-case hex digits")
        if eid in self.pins:
            raise config_error(f"episode {eid} is already pinned")
        key = secret if secret is not None else secrets.token_hex(32)
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise config_error("secret: 64 lower-case hex digits")
        self.pins[eid] = {"spec": full.model_dump(mode="json"), "secret": key}
        return eid

    def document(self) -> SandboxEpisodes:
        """The `qt.sandbox_episodes` document (holds the secrets)."""
        return SandboxEpisodes.model_validate(
            {
                "schema": "qt.sandbox_episodes",
                "schema_version": 1,
                "episodes": [{"episode_id": k, **v} for k, v in self.pins.items()],
            }
        )

    def save(self, path: str | Path) -> Path:
        """Writes the book (owner read and write only: it holds the secrets)."""
        p = Path(path)
        text = json.dumps(self.document().model_dump(mode="json"), indent=2) + "\n"
        p.write_text(text, encoding="utf-8")
        p.chmod(0o600)
        return p

    @classmethod
    def load(cls, path: str | Path) -> EpisodeBook:
        """Reads a book written by `save`."""
        p = Path(path)
        try:
            doc = SandboxEpisodes.model_validate(json.loads(p.read_text(encoding="utf-8")))
        except FileNotFoundError:
            raise QtError("QT-INPUT-001", f"{p}: not found") from None
        except (OSError, ValueError) as e:
            raise QtError("QT-INPUT-002", f"{p}: not a qt.sandbox_episodes document: {e}") from None
        return cls({e.episode_id: {"spec": e.spec, "secret": e.secret} for e in doc.episodes})

    def start(
        self,
        episode_id: str,
        *,
        session_id: str | None = None,
        workdir: str | Path | None = None,
        contain: Contain | None = None,
        stepping: str = "session",
    ) -> BlindSandbox:
        """Opens a pinned episode for an agent. An unknown id is `QT-USAGE-001`, with no hint of what is pinned."""
        pin = self.pins.get(episode_id) if isinstance(episode_id, str) else None
        if pin is None:
            raise usage_error("unknown episode id", "ask the operator for the id of a pinned episode")
        return BlindSandbox.open(
            episode_id,
            pin["spec"],
            pin["secret"],
            session_id=session_id,
            workdir=workdir,
            contain=contain,
            stepping=stepping,
        )


# ---- the agent's sandbox -----------------------------------------------------------------------------------------


def _reference(obs: SandboxObservation) -> int:
    """The price the rebasing is anchored to: best bid, else best ask, else the last print in view."""
    b = obs.book
    for lv in (b.bid, b.ask):
        if lv is not None:
            return int(lv[0])
    for stats in (obs.trades_since_last, obs.session_trades):
        if stats.last is not None:
            return int(stats.last)
    raise config_error(
        "a blind episode needs a quote or a print in view at its start (the rebasing is anchored to it)",
        "pin a later start",
    )


class BlindSandbox:
    """An episode played blind (module docstring). The agent's methods mirror `Sandbox`'s in blind units;
    `scorecard` (after `finish`) and `inner` are the operator's."""

    def __init__(self, inner: Sandbox, episode_id: str, transform: BlindTransform) -> None:
        #: The operator's episode, in true values. An in-process agent could read it: drive a blind agent through
        #: the MCP tools from another process.
        self.inner = inner
        self.episode_id = episode_id
        self.transform = transform
        #: The operator's scorecard, set by `finish`.
        self.scorecard: SandboxScorecard | None = None
        i = inner.instrument
        self._size_exp = int(i["size_exp"])
        self._mult = float(i["contract_multiplier"])
        self.instrument = {
            "tick_size": i["tick_size"],
            "lot_size": i["lot_size"],
            "price_exp": i["price_exp"],
            "size_exp": i["size_exp"],
            "contract_multiplier": i["contract_multiplier"],
            "currency": i["currency"],
        }
        self._secrets = self._secret_strings()
        self._rows0 = inner.log.entries[0]["rows_seen"]
        self._entries: list[dict[str, Any]] = []
        #: Inner log seq -> the request as the agent sent it (submit and rejected entries).
        self._requests: dict[int, dict[str, Any]] = {}
        self._messages: dict[int, str] = {}
        first = self._obs(inner.session.observation)
        self._mirror(observation=first)
        spec = inner.spec.model_dump(mode="json")
        self.session = SandboxBlindSession.model_validate(
            {
                "schema": "qt.sandbox_blind_session",
                "schema_version": 1,
                "session_id": inner.session_id,
                "episode_id": episode_id,
                "engine_version": __version__,
                "time_origin": "session_open",
                "instrument": self.instrument,
                "settings": {k: spec[k] for k in VISIBLE_SETTINGS},
                "start_ns": transform.t(inner.start_ns),
                "end_ns": transform.t(inner.end_ns),
                "transform_sha256": transform.commitment(),
                "rules": list(BLIND_RULES),
                "observation": first.model_dump(mode="json"),
            }
        )

    @classmethod
    def open(
        cls,
        episode_id: str,
        spec: dict[str, Any],
        secret: str,
        *,
        session_id: str | None = None,
        workdir: str | Path | None = None,
        contain: Contain | None = None,
        stepping: str = "session",
    ) -> BlindSandbox:
        """Opens the pinned `spec` blind (use `EpisodeBook.start`)."""
        made: dict[str, BlindTransform] = {}

        def hook(sb: Sandbox, obs: SandboxObservation) -> dict[str, Any]:
            inst = sb.instrument
            tr = BlindTransform.derive(
                secret,
                origin_ns=inst["open_ns"],
                reference_raw=_reference(obs),
                tick_size=inst["tick_size"],
                price_exp=inst["price_exp"],
            )
            made["t"] = tr
            return {"episode_id": episode_id, "transform_sha256": tr.commitment()}

        try:
            inner = Sandbox.open(
                spec, workdir=workdir, session_id=session_id, contain=contain, stepping=stepping, blind=hook
            )
        except QtError as e:
            # The pin is the operator's: a failure to open names nothing of it to the agent.
            raise QtError(e.code, "the episode could not be opened", "ask the operator", None) from None
        return cls(inner, episode_id, made["t"])

    def __enter__(self) -> BlindSandbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Ends the episode without a scorecard."""
        self.inner.close()

    # ---- state the MCP tools read ----------------------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self.inner.closed

    @property
    def clock_ns(self) -> int:
        """The clock, in ns since the open."""
        return int(self.transform.t(self.inner.clock_ns) or 0)

    @property
    def end_ns(self) -> int:
        """The episode end, in ns since the open."""
        return int(self.transform.t(self.inner.end_ns) or 0)

    @property
    def step(self) -> int:
        return self.inner.step

    @property
    def n_orders(self) -> int:
        return len(self.inner.orders)

    @property
    def n_open(self) -> int:
        return sum(1 for o in self.inner.orders if not o.final)

    def to_price(self, raw: int) -> Decimal:
        """A raw (rebased) price as an exact decimal."""
        return _real(int(raw), self.transform.price_exp)

    def to_raw(self, price: str | Decimal | int) -> int:
        """A decimal (rebased) price as the raw integer the documents use."""
        return to_fixed(wire(price), self.transform.price_exp, "price")

    # ---- errors --------------------------------------------------------------------------------------------------

    def _secret_strings(self) -> list[str]:
        i = self.inner.instrument
        src = self.inner.src
        out = [
            i["session_date"],
            i["session_date"].replace("-", ""),
            self.inner.dataset_id,
            self.inner.spec_id,
            str(self.inner.spec.dataset.path),
            str(getattr(src, "root", "")),
        ]
        if len(i["symbol"]) >= 2:
            out.append(i["symbol"])
        if len(i["venue"]) >= 3:
            out.append(i["venue"])
        return [s for s in out if s]

    def _leaks(self, text: str) -> bool:
        low = text.lower()
        if any(s.lower() in low for s in self._secrets):
            return True
        return bool(_TIME_TEXT.search(text) or _LONG_INT.search(text))

    def _error(self, e: QtError, prices: dict[int, int]) -> QtError:
        """`e` in blind units: the true prices the call passed in replaced by the agent's, a message or hint that
        would still name a hidden value replaced by a generic one; only `reason` and `order_id` details kept."""
        msg, hint = e.message, e.hint
        for true, blind in prices.items():
            pat = re.compile(rf"(?<![0-9]){true}(?![0-9])")
            msg = pat.sub(str(blind), msg)
            hint = None if hint is None else pat.sub(str(blind), hint)
        details = {k: v for k, v in (e.details or {}).items() if k in ("reason", "order_id")}
        if self._leaks(msg) or any(str(v) in self._secrets for v in details.values()):
            reason = details.get("reason")
            msg = "the request was rejected" + (f" ({reason})" if reason else "")
        if hint is not None and self._leaks(hint):
            hint = None
        return QtError(e.code, msg, hint, details or None)

    # ---- the agent's log -----------------------------------------------------------------------------------------

    def _entry_data(self, e: dict[str, Any], observation: SandboxObservation | None) -> dict[str, Any]:
        tr, d, seq = self.transform, e["data"], e["seq"]
        kind = e["kind"]
        if kind == "open":
            assert observation is not None
            return {
                "episode_id": self.episode_id,
                "start_ns": tr.t(d["start_ns"]),
                "end_ns": tr.t(d["end_ns"]),
                "transform_sha256": tr.commitment(),
                "observation_sha256": digest(observation.model_dump(mode="json")),
            }
        if kind == "submit":
            market = d["request"]["type"] == "market"
            return {
                "request": self._requests[seq],
                "order_id": d["order_id"],
                "price": None if market else tr.p(d["price"]),
                "latency_ns": d["latency_ns"],
            }
        if kind == "cancel":
            return {k: d[k] for k in ("order_id", "qty", "new_qty", "by")}
        if kind == "reject":
            return {
                "action": d["action"],
                "request": self._requests[seq],
                "code": d["code"],
                "message": self._messages[seq],
            }
        if kind == "advance":
            assert observation is not None
            return {"to_ns": tr.t(d["to_ns"]), "observation_sha256": digest(observation.model_dump(mode="json"))}
        if kind == "finish":
            return {"finish_ns": tr.t(d["finish_ns"])}
        raise QtError("QT-ENGINE-001", f"blind log: unknown entry kind {kind!r}")

    def _mirror(self, observation: SandboxObservation | None = None) -> None:
        """Appends the blind entry of every operator log entry not mirrored yet (one for one, same `seq`)."""
        from queuetruth.sandbox.log import entry_hash

        for e in self.inner.log.entries[len(self._entries) :]:
            entry: dict[str, Any] = {
                "seq": e["seq"],
                "kind": e["kind"],
                "clock_ns": self.transform.t(e["clock_ns"]),
                "visible_ns": self.transform.t(e["visible_ns"]),
                "rows_seen": e["rows_seen"] - self._rows0,
                "data": self._entry_data(e, observation),
                "prev": self._entries[-1]["hash"] if self._entries else BLIND_GENESIS,
            }
            entry["hash"] = entry_hash(entry)
            self._entries.append(entry)

    def order_log(self) -> dict[str, Any]:
        """The agent's blind log (`qt.sandbox_blind_order_log` as a dict)."""
        return SandboxBlindOrderLog.model_validate(
            {
                "schema": "qt.sandbox_blind_order_log",
                "schema_version": 1,
                "episode_id": self.episode_id,
                "entries": self._entries,
                "root": self._entries[-1]["hash"] if self._entries else BLIND_GENESIS,
            }
        ).model_dump(mode="json")

    # ---- documents -----------------------------------------------------------------------------------------------

    def _obs(self, obs: SandboxObservation) -> SandboxObservation:
        doc = blind_observation(self.transform, obs.model_dump(mode="json"), size_exp=self._size_exp, mult=self._mult)
        return SandboxObservation.model_validate(doc)

    def _ack(self, ack: SandboxAck) -> SandboxAck:
        doc = ack.model_dump(mode="json")
        entry = self._entries[doc["log_seq"]]
        doc.update(
            clock_ns=self.transform.t(doc["clock_ns"]),
            order=blind_order(self.transform, doc["order"]),
            log_hash=entry["hash"],
        )
        return SandboxAck.model_validate(doc)

    # ---- the agent's API -----------------------------------------------------------------------------------------

    def observe(self) -> SandboxObservation:
        """The current observation, in blind units."""
        try:
            obs = self.inner.observe()
        except QtError as e:
            raise self._error(e, {}) from None
        return self._obs(obs)

    def advance(self, *, by: int | str | None = None, to: int | str | None = None) -> SandboxObservation:
        """Moves the clock by a duration (`by`, unchanged) or to a time since the open (`to`: ns, or `HH:MM[:SS]`
        elapsed since the open, optionally written `+HH:MM`)."""
        target: int | None = None
        if to is not None:
            if isinstance(to, str):
                rel = parse_time(to.strip().removeprefix("+"), 0, "to")
            elif isinstance(to, int) and not isinstance(to, bool):
                rel = int(to)
            else:
                raise config_error(f"to: expected a time since the open (ns or 'HH:MM'), got {type(to).__name__}")
            if by is None and not self.inner.closed and rel <= self.clock_ns:
                raise usage_error(
                    f"to ({rel_time(rel)}) is not after the clock ({rel_time(self.clock_ns)})",
                    "the clock only moves forward",
                )
            target = self.transform.t_back(rel)
        if by is not None:
            parse_duration(by, "by")
        try:
            obs = self.inner.advance(by=by, to=target)
        except QtError as e:
            raise self._error(e, {}) from None
        blind = self._obs(obs)
        self._mirror(observation=blind)
        return blind

    def submit(
        self,
        side: str,
        qty: int | str,
        *,
        type: str = "limit",
        price: int | str | Decimal | None = None,
        peg_offset_ticks: int | None = None,
        tif: str = "day",
        client_order_id: str | None = None,
    ) -> SandboxAck:
        """Sends an order at the clock; `price` is a rebased price (decimal string or raw integer), mapped back
        exactly. Everything else as `Sandbox.submit`."""
        request = {
            "side": wire(side),
            "qty": wire(qty),
            "type": wire(type),
            "price": wire(price),
            "peg_offset_ticks": wire(peg_offset_ticks),
            "tif": wire(tif),
            "client_order_id": wire(client_order_id),
        }
        true_price: Any = request["price"]
        prices: dict[int, int] = {}
        if isinstance(true_price, str | int) and not isinstance(true_price, bool):
            try:
                rebased = to_fixed(true_price, self.transform.price_exp, "price")
            except QtError:
                rebased = None  # the sandbox meets the same error on the same text, and logs it
            if rebased is not None:
                true_price = self.transform.p_back(rebased)
                prices[true_price] = rebased
        n = len(self.inner.log.entries)
        try:
            ack = self.inner.submit(
                request["side"],
                request["qty"],
                type=request["type"],
                price=true_price,
                peg_offset_ticks=request["peg_offset_ticks"],
                tif=request["tif"],
                client_order_id=request["client_order_id"],
            )
        except QtError as e:
            err = self._error(e, prices)
            self._record(n, request, err)
            raise err from None
        self._record(n, request, None)
        return self._ack(ack)

    def cancel(self, order_id: str, qty: int | str | None = None) -> SandboxAck:
        """Cancels an order, or `qty` of it (as `Sandbox.cancel`)."""
        request = {"order_id": wire(order_id), "qty": wire(qty)}
        n = len(self.inner.log.entries)
        try:
            ack = self.inner.cancel(order_id, qty)
        except QtError as e:
            err = self._error(e, {})
            self._record(n, request, err)
            raise err from None
        self._record(n, request, None)
        return self._ack(ack)

    def _record(self, n: int, request: dict[str, Any], err: QtError | None) -> None:
        """Mirrors the entry an instruction appended (if any) with the agent's request and blind message."""
        if len(self.inner.log.entries) > n:
            self._requests[n] = request
            if err is not None:
                self._messages[n] = err.message
            self._mirror()

    def finish(self, *, out: str | Path | None = None) -> SandboxBlindFinish:
        """Ends the episode. The operator's scorecard (true values, blind certificate) is kept in `scorecard` and
        written to `out` when given; the agent gets its aggregates in blind units."""
        try:
            card = self.inner.finish(out=out)
        except QtError as e:
            raise self._error(e, {}) from None
        self.scorecard = card
        self._mirror()
        if out is not None:
            reveal = {"episode_id": self.episode_id, "transform": self.transform.reveal()}
            (Path(out) / "blind.json").write_text(json.dumps(reveal, indent=2) + "\n", encoding="utf-8")
        return self.finish_summary(card)

    def finish_summary(self, card: SandboxScorecard) -> SandboxBlindFinish:
        """The agent's `qt.sandbox_blind_finish` of the operator's scorecard."""
        tr = self.transform
        doc = card.model_dump(mode="json")
        p = doc["pnl"]
        pe, se = tr.price_exp, self._size_exp
        notional = sum(abs(int(tr.p(f["price"]) or 0)) * int(f["qty"]) for f in doc["fills"])
        pnl = {
            **p,
            "cash": _cash(tr, p["cash"], p["position_end"], se, self._mult),
            "mark_ns": tr.t(p["mark_ns"]),
            "mark_price": tr.p_float(p["mark_price"]),
            "notional_traded": float(money(notional, pe + se, self._mult)),
        }
        withheld = "a warning was withheld (it names a hidden value)"
        warnings = [withheld if self._leaks(w) else w for w in doc["warnings"]]
        return SandboxBlindFinish.model_validate(
            {
                "schema": "qt.sandbox_blind_finish",
                "schema_version": 1,
                "session_id": self.inner.session_id,
                "episode_id": self.episode_id,
                "engine_version": doc["engine_version"],
                "start_ns": tr.t(doc["start_ns"]),
                "end_ns": tr.t(doc["end_ns"]),
                "finish_ns": tr.t(doc["finish_ns"]),
                "steps": doc["steps"],
                "n_orders": len(doc["orders"]),
                "n_fills": len(doc["fills"]),
                "n_fills_maker": sum(1 for f in doc["fills"] if f["liquidity"] == "maker"),
                "pnl": pnl,
                "fill_rates": doc["fill_rates"],
                "markouts": [{**m, "mean_bps": None} for m in doc["markouts"]],
                "adverse_selection": doc["adverse_selection"],
                "execution_cost": doc["execution_cost"],
                "consistency": doc["consistency"],
                "diagnostics": doc["diagnostics"],
                "data_quality": doc["data_quality"],
                "warnings": warnings,
                "transform_sha256": tr.commitment(),
                "log_root": self._entries[-1]["hash"],
            }
        )


__all__ = [
    "BASE_MAX_TICKS",
    "BASE_MIN_TICKS",
    "BLIND_GENESIS",
    "BLIND_RULES",
    "VISIBLE_SETTINGS",
    "BlindSandbox",
    "BlindTransform",
    "EpisodeBook",
    "blind_observation",
    "blind_order",
    "check_reveal",
]
