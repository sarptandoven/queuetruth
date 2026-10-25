"""Errors with queuetruth's stable `QT-<AREA>-<NNN>` codes (the same codes and exit codes as `queuetruth export`).

- `QT-INPUT-001`: the calibration file is missing or unreadable (exit 2).
- `QT-INPUT-002`: the file is not a `qt.calibration_result` or a `queuetruth export nautilus` snippet (exit 2).
- `QT-CONFIG-001`: the calibration carries nothing NautilusTrader can use, or an argument is out of range (exit 2).
- `QT-CALIB-001`: the calibration's trust verdict refuses it (exit 1, like a failed gate); `force=True` overrides.
- `QT-USAGE-001`: nautilus_trader is not installed, or its version is not supported (exit 2).
- `QT-ENGINE-001`: an unexpected exception in the CLI (a bug; exit 2).
"""

from __future__ import annotations

from typing import Any

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ERROR = 2


class QtNautilusError(Exception):
    """An error with a stable code, a message and an optional hint; `str(e)` is `"<code>: <message>"`."""

    def __init__(self, code: str, message: str, hint: str | None = None, details: dict[str, Any] | None = None) -> None:
        super().__init__(code, message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details: dict[str, Any] = dict(details or {})

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.code!r}, {self.message!r})"

    @property
    def exit_code(self) -> int:
        """CLI exit code: 1 for a refused calibration (`QT-CALIB-001`), else 2."""
        return EXIT_REFUSED if self.code == "QT-CALIB-001" else EXIT_ERROR

    def to_document(self) -> dict[str, Any]:
        """The `qt.error` JSON document (same shape as queuetruth's)."""
        return {
            "schema": "qt.error",
            "schema_version": 1,
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "details": dict(self.details),
        }


class CalibrationRefused(QtNautilusError):
    """`QT-CALIB-001`: the trust verdict refuses the calibration (too few events, a refusing flag, or it does not beat
    NautilusTrader's default out of sample). `details["reasons"]` lists the refusing tokens."""

    def __init__(self, message: str, reasons: list[str], hint: str | None = None) -> None:
        super().__init__("QT-CALIB-001", message, hint, {"reasons": list(reasons)})
        self.reasons = list(reasons)


class CalibrationWarning(UserWarning):
    """A caveat about using a calibration in the installed NautilusTrader (forced refusal, version-dependent queue
    rules, a per-touch probability carried to another feed)."""
