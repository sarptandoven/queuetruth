"""Library of the local nightly runner (`scripts/nightly/nightly.py`): environments, gates, history and the page.

Standard library only, so the runner itself starts on any CPython >= 3.11 without the queuetruth venv.
"""

SCHEMA_RUN = "qt.nightly.run"
SCHEMA_HISTORY = "qt.nightly.history"
SCHEMA_VERSION = 1

# process exit codes of `nightly.py run` (precedence 2 > 1 > 4 > 3 > 0)
EXIT_OK = 0  # every check passed and no tracked number changed against the earlier runs of the same mode
EXIT_FAIL = 1  # a required check failed one of its gates
EXIT_ERROR = 2  # a required check could not run (environment, download, crash, timeout, disk), or a runner error
#                 (lock held, unreadable history, setup failed, interrupted)
EXIT_CHANGED = 3  # every check passed, but a tracked number or a check status changed against the earlier runs
EXIT_ADVISORY = 4  # only an advisory check (the NautilusTrader release candidate) failed or errored

EXIT_MEANING = {
    EXIT_OK: "ok: every check passed, nothing changed against the earlier runs",
    EXIT_FAIL: "fail: a required check failed a gate",
    EXIT_ERROR: "error: a required check could not run, or the runner could not",
    EXIT_CHANGED: "changed: every check passed, but numbers or statuses changed against the earlier runs",
    EXIT_ADVISORY: "advisory: only the advisory check failed or errored",
}

STATUSES = ("pass", "fail", "error")
