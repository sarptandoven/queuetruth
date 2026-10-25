"""`qt.demo_summary`: the document `queuetruth demo` prints with `--format json` and writes to `<out>/demo.json`, and
`qt.demo_plan`, what `--dry-run` prints, and `qt.demo_sources`, what `--list-sources --format json` prints.

Fractions are fractions (0.0313 = 3.13 pp); horizon keys are nanoseconds as decimal strings, as in
`qt.zoo_summary`. `run_id` follows SPEC 7.3: the first 16 hex of sha256 over the canonical demo spec (format,
session date, symbols, window, shadows, seed, canonical model ids, interval settings) with the subset resolved to
the sha256 of its content (the framed ITCH messages or the pcap bytes, before gzip), and `out` and `threads`
excluded. Where the subset came from (source, URL, path) and how it was compressed are provenance, not identity:
the same content gives the same id on any machine. `timings`, `created` and the paths are the only fields that vary
between identical runs.

The models are defined in `queuetruth.schemas.demo` (registered in `queuetruth.schemas.DOC_MODELS`); this module
re-exports them.
"""

from queuetruth.schemas.demo import (
    Cell,
    DataInfo,
    DemoPlan,
    DemoSourceRow,
    DemoSources,
    DemoSummary,
    Headline,
    Interval,
    ModelRow,
    Outputs,
    Reference,
    SourceInfo,
    Timings,
    WindowInfo,
)

__all__ = [
    "Cell",
    "DataInfo",
    "DemoPlan",
    "DemoSourceRow",
    "DemoSources",
    "DemoSummary",
    "Headline",
    "Interval",
    "ModelRow",
    "Outputs",
    "Reference",
    "SourceInfo",
    "Timings",
    "WindowInfo",
]
