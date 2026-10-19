"""Migration runner: applies `migrations/NNNN_*.sql` in order, each once and in its own transaction, recording
`(version, name, sha256)` in `schema_migrations`; a changed applied file is an error. Afterwards the `qt_app` and
`qt_worker` passwords are set from `QT_DATABASE_URL_APP` and `QT_DATABASE_URL_WORKER` (never stored in a committed SQL
file). Only the runner (and `create-tenant`) uses the owner DSN; the api and the worker never get it."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
log = logging.getLogger("queuetruth_service.db")


@dataclass(frozen=True)
class Migration:
    """One migration file."""

    version: int
    name: str
    path: Path

    @property
    def sha256(self) -> str:
        """Content hash of the file."""
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


def migrations() -> list[Migration]:
    """All migrations in version order."""
    out = []
    for p in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        out.append(Migration(int(p.name[:4]), p.stem[5:], p))
    versions = [m.version for m in out]
    if len(set(versions)) != len(versions):
        raise RuntimeError("duplicate migration versions")
    return out


#: DSN setting -> the role it must name.
APP_ROLES = {"QT_DATABASE_URL_APP": "qt_app", "QT_DATABASE_URL_WORKER": "qt_worker"}


def _role_password(dsn: str | None, setting: str) -> str | None:
    """The password of an app-role DSN (None without one); the DSN must name the expected role."""
    if not dsn:
        return None
    info = psycopg.conninfo.conninfo_to_dict(dsn)
    user = info.get("user")
    if user is not None and user != APP_ROLES[setting]:
        raise RuntimeError(f"{setting} must connect as {APP_ROLES[setting]}, not {user!r}")
    password = info.get("password")
    return str(password) if password else None


def migrate(owner_dsn: str, app_dsn: str | None = None, worker_dsn: str | None = None) -> list[str]:
    """Applies pending migrations and sets the app-role passwords; returns the names applied."""
    passwords = {
        "qt_app": _role_password(app_dsn, "QT_DATABASE_URL_APP"),
        "qt_worker": _role_password(worker_dsn, "QT_DATABASE_URL_WORKER"),
    }
    applied: list[str] = []
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        ok = conn.execute("select rolsuper or rolbypassrls from pg_roles where rolname = current_user").fetchone()
        if not ok or not ok[0]:
            raise RuntimeError("the migration role must be a superuser or have BYPASSRLS (FORCE RLS applies to owners)")
        conn.execute(
            "create table if not exists schema_migrations (version int primary key, name text not null, "
            "sha256 text not null, applied_at timestamptz not null default now())"
        )
        conn.execute("select pg_advisory_lock(727274)")
        try:
            done = {v: h for v, h in conn.execute("select version, sha256 from schema_migrations").fetchall()}
            for m in migrations():
                if m.version in done:
                    if done[m.version] != m.sha256:
                        raise RuntimeError(f"applied migration {m.path.name} was modified")
                    continue
                with conn.transaction():
                    conn.execute(m.path.read_text())
                    conn.execute(
                        "insert into schema_migrations (version, name, sha256) values (%s, %s, %s)",
                        (m.version, m.name, m.sha256),
                    )
                applied.append(m.path.name)
                log.info("applied migration %s", m.path.name)
            for role, password in passwords.items():
                if password:
                    conn.execute(
                        sql.SQL("alter role {} password {}").format(sql.Identifier(role), sql.Literal(password))
                    )
        finally:
            conn.execute("select pg_advisory_unlock(727274)")
    return applied
