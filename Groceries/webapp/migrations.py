"""Versioned schema migrations for the webapp's own tables (#66).

Before this, ten ensure_*_table() functions ran from ~40 request handlers and a
context processor, so every page view executed CREATE/ALTER TABLE (ACCESS
EXCLUSIVE locks) and /planner re-ran a constraint swap on meal_plan_slots. The
schema now changes in exactly one place, once, before gunicorn starts:
init_schema.py -> migrate().

How it works: `schema_version` records which numbered migrations a database has
had. migrate() applies the missing ones in order, each recorded in the same
transaction as its DDL, so a failure leaves the version where it was and the
next start retries it. A transaction-scoped advisory lock serialises concurrent
starters (two containers, or someone running init_schema.py by hand).

Migration 1 is the baseline: the ensure_*_table() functions exactly as they
stood at #66. They were written to be idempotent against every schema they had
ever met (CREATE ... IF NOT EXISTS, ADD COLUMN IF NOT EXISTS, a guarded
constraint swap), which is what makes it safe to stamp an existing, populated
database as version 1 by running them once more. Verified against a restored
copy of the household database - see the #66 commit.

Adding a change: append (N, description, function) below. Do NOT edit an
ensure_* function to change the schema - a database already at version 1 will
never run it again. Destructive steps (DROP, ALTER TYPE) belong here and only
here, where review can see them (CONTRIBUTING §8).
"""

import app

# Arbitrary constant; only needs to be unique among this database's advisory locks.
_LOCK_KEY = 660066

MIGRATIONS = [
    (1, "baseline: every app table as of #66", app.ensure_app_schema),
]


def current_version(cur):
    cur.execute("SELECT to_regclass('schema_version') IS NOT NULL")
    if not cur.fetchone()[0]:
        return 0
    cur.execute("SELECT coalesce(max(version), 0) FROM schema_version")
    return cur.fetchone()[0]


def migrate(cur):
    """Apply every pending migration in order. Returns the versions applied.

    The caller commits: the version rows and the DDL they describe land in one
    transaction, so they cannot disagree.
    """
    versions = [v for v, _, _ in MIGRATIONS]
    if versions != sorted(set(versions)):
        raise RuntimeError(f"MIGRATIONS must be strictly increasing, got {versions}")
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TIMESTAMP NOT NULL DEFAULT now()
        );
    """)
    have = current_version(cur)
    applied = []
    for version, description, apply in MIGRATIONS:
        if version <= have:
            continue
        apply(cur)
        cur.execute(
            "INSERT INTO schema_version (version, description) VALUES (%s, %s)",
            (version, description),
        )
        applied.append(version)
    return applied
