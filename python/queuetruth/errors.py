"""Exceptions of the Python API (SPEC 0.2, 4.3): every `QT-*` error is a `QtError`; remote errors are `QtApiError`."""

from __future__ import annotations

from typing import Any

EXIT_OK = 0
EXIT_GATE = 1
EXIT_ERROR = 2


class QtError(Exception):
    """An error with a stable code `QT-<AREA>-<NNN>`, a message and an optional hint.

    `str(e)` is `"<code>: <message>"`, the same rendering as the Rust `QtError`.
    """

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
        """CLI exit code: 1 for a failed gate (`QT-GATE-001`), else 2."""
        return EXIT_GATE if self.code == "QT-GATE-001" else EXIT_ERROR

    def to_document(self) -> dict[str, Any]:
        """The `qt.error` JSON document."""
        return {
            "schema": "qt.error",
            "schema_version": 1,
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "details": dict(self.details),
        }

    @classmethod
    def from_display(cls, text: str, hint: str | None = None) -> QtError:
        """Parses `"<code>: <message>"` (the Rust `Display` form); other text becomes `QT-ENGINE-001`."""
        code, sep, message = text.partition(": ")
        if sep and code.startswith("QT-") and code.count("-") == 2:
            return cls(code, message, hint)
        return cls("QT-ENGINE-001", text, hint)


class QtApiError(QtError):
    """The HTTP API returned an RFC 9457 problem (`QT-REMOTE-001`); `.problem` holds the `qt.problem` document."""

    def __init__(self, problem: dict[str, Any], hint: str | None = None) -> None:
        self.problem: dict[str, Any] = dict(problem)
        remote = problem.get("code")
        title = problem.get("title") or "remote error"
        detail = problem.get("detail")
        status = problem.get("status")
        text = f"HTTP {status} {title}" if status is not None else str(title)
        if remote:
            text += f" ({remote})"
        if detail:
            text += f": {detail}"
        super().__init__("QT-REMOTE-001", text, hint, {"problem": self.problem})

    @property
    def status(self) -> int | None:
        """HTTP status of the problem."""
        s = self.problem.get("status")
        return s if isinstance(s, int) else None

    @property
    def remote_code(self) -> str | None:
        """The problem's own `code` (echoed from the service)."""
        c = self.problem.get("code")
        return c if isinstance(c, str) else None


__all__ = ["EXIT_ERROR", "EXIT_GATE", "EXIT_OK", "QtApiError", "QtError"]
