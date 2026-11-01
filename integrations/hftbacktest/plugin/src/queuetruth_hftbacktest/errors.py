"""Errors with queuetruth's stable `QT-<AREA>-<NNN>` codes (the same codes and exit codes as the `queuetruth` CLI).

- `QT-INPUT-001`: an input file or directory is missing or unreadable (exit 2).
- `QT-INPUT-002`: an input is not what it should be (not a `prepare` directory, no hftbacktest rows, ...) (exit 2).
- `QT-CONFIG-001`: an argument is out of range or inconsistent (exit 2).
- `QT-CALIB-001`: no fitted queue model passes queuetruth's trust check, so there is no code to paste (exit 1, like
  a failed gate); `force=True` writes the best one under a `NOT VALIDATED` header.
- `QT-USAGE-001`: the hftbacktest python cannot be run, or the replay stopped with an hftbacktest error (exit 2).
- `QT-ENGINE-001`: an unexpected exception in the CLI (a bug; exit 2).

Errors raised by queuetruth itself (`queuetruth.errors.QtError`) are passed through unchanged; they carry the same
kind of code.
"""

from __future__ import annotations

from typing import Any

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ERROR = 2


class QtHbtError(Exception):
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


class CalibrationRefused(QtHbtError):
    """`QT-CALIB-001`: every fitted family is refused by queuetruth's trust check. `details["reasons"]` maps each
    family to its refusing reasons."""

    def __init__(self, message: str, reasons: dict[str, list[str]], hint: str | None = None) -> None:
        super().__init__("QT-CALIB-001", message, hint, {"reasons": {k: list(v) for k, v in reasons.items()}})
        self.reasons = {k: list(v) for k, v in reasons.items()}
