"""Shared importer plumbing: the importer signature, the `ImporterDef` every importer module declares, the typed
options base, and small field mappers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, get_args, get_origin

from pydantic import BaseModel, ConfigDict, StrictBool, StrictFloat, StrictInt, StrictStr, ValidationError

from queuetruth.errors import QtError
from queuetruth.live._common import Record, dump_raw, input_error, read_json_records
from queuetruth.live.builder import ImportResult, LogBuilder
from queuetruth.live.imap import InstrumentMap


@dataclass
class ImportOptions:
    """Importer options: account override, instrument map and venue-specific settings."""

    account: str | None = None
    imap: InstrumentMap | None = None
    extra: dict[str, Any] = field(default_factory=dict)


Importer = Callable[[list[str], ImportOptions], ImportResult]

#: Option value types. Values pass to the importer as given (no coercion): the importer reads text such as `"5"` or
#: `"-2"` itself and names the option in its own error. These types only refuse what no importer reads (a list where
#: a name belongs, a boolean where a number belongs).
Text = StrictStr | None
Int = StrictInt | StrictStr | None
Num = StrictInt | StrictFloat | StrictStr | None
Flag = StrictBool | StrictStr | None
Paths = StrictStr | Path | list[StrictStr | Path] | None

#: The kinds of importer: a venue's own records, a simulator's or backtester's output (log venue `sim:<name>`), or a
#: trading bot's live run (log venue = the exchange it traded on).
#: Where every importer's options are listed (generated from the registry).
_OPTIONS_HINT = "every importer's options: docs/reference/importers.md"

ImporterKind = Literal["venue", "sim", "bot"]
KINDS: tuple[str, ...] = ("venue", "sim", "bot")


class ImporterOptions(BaseModel):
    """Options of an importer. Each importer declares a subclass with its own option fields; the shared fields
    (`account`, `imap`) are inherited. Unknown options fail `QT-CONFIG-001` with the list of known ones."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True, frozen=True)

    #: Fields every importer has; they are `ImportOptions.account` / `.imap`, not entries of `ImportOptions.extra`.
    SHARED: ClassVar[frozenset[str]] = frozenset({"account", "imap"})

    #: account label of the log (default: the importer's own, e.g. `main` or `sim`).
    account: Text = None
    #: the parsed instrument map (`queuetruth.live.imap.InstrumentMap`), or None.
    imap: InstrumentMap | None = None

    @classmethod
    def option_names(cls) -> frozenset[str]:
        """The importer's own option names (the keys of `ImportOptions.extra` it reads)."""
        return frozenset(cls.model_fields) - cls.SHARED

    def import_options(self) -> ImportOptions:
        """The `ImportOptions` the importer function takes: only the options that were given, values as given."""
        extra = {k: getattr(self, k) for k in self.model_fields_set - self.SHARED}
        extra = {k: (v.model_dump() if isinstance(v, BaseModel) else v) for k, v in extra.items()}
        return ImportOptions(account=self.account, imap=self.imap, extra=extra)


def _args(annotation: Any) -> tuple[Any, ...]:
    """The members of a union, with `Annotated` metadata (`StrictStr`) taken off; `list[...]` / `dict[...]` stay."""
    if get_origin(annotation) is Annotated:
        return _args(get_args(annotation)[0])
    if get_origin(annotation) in (list, dict) or not get_args(annotation):
        return (annotation,)
    return tuple(x for a in get_args(annotation) for x in _args(a))


def type_text(annotation: Any) -> str:
    """An option's type in words (`integer or text`)."""
    words: list[str] = []
    for a in _args(annotation):
        if a is type(None):
            continue
        base = get_origin(a) or a
        name = getattr(base, "__name__", str(base))
        name = {"str": "text", "int": "integer", "float": "number", "bool": "true/false", "dict": "object"}.get(
            name, name
        )
        name = {"FixOptions": "FIX options", "Path": "path"}.get(name, name)
        if name not in words:
            words.append(name)
    return " or ".join(words)


@dataclass(frozen=True)
class ImporterDef:
    """One importer, declared next to its code (`IMPORTER = ImporterDef(...)`, or a tuple `IMPORTERS` when a module
    holds several) and collected by `queuetruth.live.importers.registry`. Third parties register theirs through the
    `queuetruth.importers` entry-point group.

    - `name`: lowercase letters, digits and `_`. The `LiveImportSpec.venue` value is `name` for venues and bots and
      `sim_<name>` for simulators; the CLI spelling (`--venue`) uses `-` for `_`.
    - `kind`: `venue`, `sim` or `bot` (see `ImporterKind`).
    - `run`: `(input paths, ImportOptions) -> ImportResult`.
    - `options`: the typed options model (an `ImporterOptions` subclass).
    - `formats`: the input formats it reads, in words (`user-data stream JSON lines`, `Flex XML`, ...).
    - `patterns`: file patterns of typical inputs (`*.jsonl`, `*.messages.log`, ...).
    - `description`: one line for listings and help.
    - `docs`: the docs page (path under docs/, e.g. `how-to/import-live-logs.md#binance`).
    - `rank`: list position (CLI choices, schema enum); built-ins use 10 to 180, others default to 1000.
    """

    name: str
    kind: ImporterKind
    run: Importer
    options: type[ImporterOptions] = ImporterOptions
    formats: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    description: str = ""
    docs: str = ""
    rank: int = 1000
    #: `builtin`, or the distribution and entry point of a plugin
    origin: str = field(default="builtin", compare=False)

    @property
    def venue(self) -> str:
        """The `LiveImportSpec.venue` value (`sim_<name>` for simulators)."""
        return f"sim_{self.name}" if self.kind == "sim" else self.name

    @property
    def cli_venue(self) -> str:
        """The `live import --venue` spelling."""
        return self.venue.replace("_", "-")

    @property
    def builtin(self) -> bool:
        return self.origin == "builtin"

    def validate(
        self, account: str | None = None, imap: InstrumentMap | None = None, **options: Any
    ) -> ImporterOptions:
        """The typed options of one import; `QT-CONFIG-001` names a wrong or unknown option and lists the known ones."""
        known = sorted(self.options.option_names())
        unknown = sorted(set(options) - set(known))
        if unknown:
            raise QtError(
                "QT-CONFIG-001",
                f"{self.name} importer: unknown option(s) {unknown}; known: {known}",
                _OPTIONS_HINT,
            )
        try:
            return self.options.model_validate({"account": account, "imap": imap, **options})
        except ValidationError as e:
            err = e.errors()[0]
            name = str(err["loc"][0])
            f = self.options.model_fields.get(name)
            nested = f is not None and any(
                isinstance(a, type) and issubclass(a, BaseModel) for a in _args(f.annotation)
            )
            if f is None or nested:  # a field of a nested model (`fix.delimiter`): pydantic's own words
                what = f"option {'.'.join(str(x) for x in err['loc'])}: {err['msg']}"
            else:
                what = f"option {name}: expected {type_text(f.annotation)}, got {repr(options.get(name))[:60]}"
            raise QtError("QT-CONFIG-001", f"{self.name} importer: {what}", _OPTIONS_HINT) from None

    def import_options(
        self, account: str | None = None, imap: InstrumentMap | None = None, **options: Any
    ) -> ImportOptions:
        """`validate`, as the `ImportOptions` the importer function takes."""
        return self.validate(account, imap, **options).import_options()

    def __call__(self, paths: list[str], opts: ImportOptions) -> ImportResult:
        return self.run(paths, opts)


def json_records(paths: list[str]) -> list[Record]:
    """Records of every JSON / JSON-lines input in order (file index = position in `paths`)."""
    out: list[Record] = []
    for i, p in enumerate(paths):
        out.extend(read_json_records(Path(p), i))
    return out


def unwrap_envelope(obj: Any, *keys: str) -> list[Any]:
    """Follows the first matching envelope keys (e.g. `result` -> `list`) and returns a list of items."""
    cur = obj
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
    if isinstance(cur, list):
        return cur
    return [cur]


def sub_records(rec: Record, *keys: str) -> list[Record]:
    """Items of an enveloped / array record as records of their own (raw = the item's JSON)."""
    items = unwrap_envelope(rec.obj, *keys)
    if len(items) == 1 and items[0] is rec.obj:
        return [rec]
    out = []
    for i, item in enumerate(items):
        out.append(
            Record(
                item,
                dump_raw(item),
                f"{rec.location}/{i}",
                rec.file_idx,
                rec.line_idx * 100_000 + i,
                recv_ns=rec.recv_ns,
                send_ns=rec.send_ns,
                direction=rec.direction,
                wrapped=rec.wrapped,
            )
        )
    return out


def _kind(v: Any) -> str:
    return {dict: "an object", list: "an array", str: "a string", bool: "a boolean"}.get(type(v), "a number")


def obj_field(v: Any, what: str, location: str) -> dict[str, Any]:
    """A nested JSON object of a record: `v`, `{}` for null, `QT-INPUT-002` for any other JSON type (an importer
    must never call `.get` on an array or a string a hostile file put where an object belongs)."""
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    raise input_error(location, f"{what} must be an object, got {_kind(v)}")


def list_field(v: Any, what: str, location: str) -> list[Any]:
    """A nested JSON array of a record: `v`, `[]` for null, `QT-INPUT-002` for any other JSON type."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    raise input_error(location, f"{what} must be an array, got {_kind(v)}")


def side_of(v: Any) -> str | None:
    """Venue side text -> `buy` / `sell`."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("buy", "b", "bid", "1", "long"):
        return "buy"
    if s in ("sell", "s", "a", "ask", "2", "5", "short"):
        return "sell"
    return None


def new_builder(venue: str, opts: ImportOptions, *, size_exp: int = -8) -> LogBuilder:
    """A `LogBuilder` with the SPEC 3.9 defaults for the venue."""
    return LogBuilder(venue, account=opts.account, imap=opts.imap, price_exp=-9, size_exp=size_exp)
