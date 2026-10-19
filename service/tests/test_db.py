"""Migrations, the ephemeral cluster, roles and RLS shape."""

from __future__ import annotations

import textwrap

import psycopg
import pytest
from pgqueuer.adapters.persistence.qb import QueryBuilderEnvironment
from psycopg import sql

from queuetruth_service.db.runner import MIGRATIONS_DIR, migrate, migrations

pytestmark = pytest.mark.requires_postgres


def _norm(sql: str) -> list[str]:
    return [line.strip() for line in textwrap.dedent(sql).splitlines() if line.strip() and not line.startswith("--")]


def test_pgqueuer_migration_matches_installed_pgqueuer():
    committed = (MIGRATIONS_DIR / "0003_pgqueuer.sql").read_text()
    assert _norm(committed) == _norm(QueryBuilderEnvironment().build_install_query())


def test_cluster_is_private_pg16(db):
    with psycopg.connect(db.owner) as c:
        ver = c.execute("show server_version").fetchone()[0]
        listen = c.execute("show listen_addresses").fetchone()[0]
    import os

    if not os.environ.get("QT_PG_DSN"):
        assert ver.startswith("16.") and listen == ""


def test_migrations_recorded_and_idempotent(db):
    rows = db.q("select version, name from schema_migrations order by version")
    assert [r[0] for r in rows] == [m.version for m in migrations()]
    assert migrate(db.owner) == []


def test_app_role_is_not_privileged_and_tables_force_rls(db):
    ((sup, byp),) = db.q("select rolsuper, rolbypassrls from pg_roles where rolname = 'qt_app'")
    assert not sup and not byp
    rows = db.q(
        "select relname, relrowsecurity, relforcerowsecurity from pg_class "
        "where relkind = 'r' and relnamespace = 'public'::regnamespace and relname not like 'pgqueuer%%' "
        "and relname <> 'schema_migrations'"
    )
    assert len(rows) >= 15
    assert all(r[1] and r[2] for r in rows), rows
    owned = db.q("select count(*) from pg_tables where tableowner = 'qt_app'")
    assert owned == [(0,)]


def test_app_password_set_from_dsn(db):
    try:
        migrate(db.owner, db.app + " password=s3cret-pass", db.worker + " password=s3cret-wpass")
        rows = db.q("select rolname, rolpassword is not null from pg_authid where rolname in ('qt_app', 'qt_worker')")
        assert sorted(rows) == [("qt_app", True), ("qt_worker", True)]
        with pytest.raises(RuntimeError, match="must connect as qt_app"):
            migrate(db.owner, db.owner)  # the owner DSN is not an app-role DSN
    finally:  # roles are cluster-wide: restore what the other tests log in with
        for role, dsn in (("qt_app", db.app), ("qt_worker", db.worker)):
            pw = psycopg.conninfo.conninfo_to_dict(dsn).get("password")
            value = sql.Literal(str(pw)) if pw else sql.SQL("null")
            db.q(sql.SQL("alter role {} password {}").format(sql.Identifier(role), value))  # type: ignore[arg-type]
    sql_text = "".join(p.read_text() for p in MIGRATIONS_DIR.glob("*.sql"))
    assert "s3cret" not in sql_text and "password '" not in sql_text.lower()
