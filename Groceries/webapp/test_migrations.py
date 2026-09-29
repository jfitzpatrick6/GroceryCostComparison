"""Tests for #66: schema changes happen once, at startup, and never in a request.

Two kinds of test. The guards scan app.py's source, because the failure they
prevent - someone adding `ensure_x_table(cur)` or a CREATE TABLE to a route - is
invisible at runtime until it causes lock contention. The runner tests use a
fake cursor that models only what migrate() touches; the real-Postgres check
(fresh database, and a restored copy of the household database) is recorded in
the #66 commit, since the required tier has no database (CONTRIBUTING §7).
"""

import ast
import inspect
import re
import unittest

import app
import migrations

_DDL = re.compile(r"\b(CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+(TABLE|CONSTRAINT|COLUMN))\b", re.I)


def _functions(tree):
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]


class RequestPathGuards(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse(inspect.getsource(app))

    def test_only_schema_functions_call_ensure_functions(self):
        offenders = []
        for fn in _functions(self.tree):
            if fn.name.startswith("ensure_"):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id.startswith("ensure_")):
                    offenders.append(f"{fn.name}:{node.lineno} calls {node.func.id}")
        self.assertEqual(offenders, [], "schema changes belong in migrations.py (#66)")

    def test_no_ddl_outside_schema_functions(self):
        offenders = []
        for fn in _functions(self.tree):
            if fn.name.startswith("ensure_"):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and _DDL.search(node.value):
                    # Docstrings describe DDL; they don't run it.
                    if node is (fn.body[0].value if fn.body and isinstance(fn.body[0], ast.Expr) else None):
                        continue
                    offenders.append(f"{fn.name}:{node.lineno}")
        self.assertEqual(offenders, [])

    def test_the_context_processor_only_reads(self):
        source = inspect.getsource(app.inject_profile_switcher)
        self.assertNotIn("commit(", source)
        self.assertNotIn("ensure_", source.split('"""')[-1])


class _Cur:
    """Just enough Postgres for migrate(): a schema_version table and its rows."""

    def __init__(self, versions=None):
        self.versions = versions  # None = table absent
        self.executed = []
        self._result = None

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if "to_regclass('schema_version')" in sql:
            self._result = (self.versions is not None,)
        elif "max(version)" in sql:
            self._result = (max(self.versions or [0]),)
        elif "CREATE TABLE IF NOT EXISTS schema_version" in sql:
            if self.versions is None:
                self.versions = []
        elif sql.startswith("INSERT INTO schema_version"):
            self.versions.append(params[0])

    def fetchone(self):
        return self._result


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._saved = migrations.MIGRATIONS
        migrations.MIGRATIONS = [
            (1, "one", lambda cur: self.calls.append(1)),
            (2, "two", lambda cur: self.calls.append(2)),
        ]

    def tearDown(self):
        migrations.MIGRATIONS = self._saved

    def test_fresh_database_gets_everything_in_order(self):
        cur = _Cur()
        self.assertEqual(migrations.migrate(cur), [1, 2])
        self.assertEqual(self.calls, [1, 2])
        self.assertEqual(cur.versions, [1, 2])

    def test_only_pending_migrations_run(self):
        cur = _Cur(versions=[1])
        self.assertEqual(migrations.migrate(cur), [2])
        self.assertEqual(self.calls, [2])

    def test_up_to_date_database_runs_nothing(self):
        self.assertEqual(migrations.migrate(_Cur(versions=[1, 2])), [])
        self.assertEqual(self.calls, [])

    def test_takes_the_lock_before_reading_the_version(self):
        cur = _Cur()
        migrations.migrate(cur)
        lock = next(i for i, s in enumerate(cur.executed) if "pg_advisory_xact_lock" in s)
        read = next(i for i, s in enumerate(cur.executed) if "max(version)" in s or "to_regclass" in s)
        self.assertLess(lock, read)

    def test_out_of_order_list_is_refused(self):
        migrations.MIGRATIONS = [(2, "b", lambda c: None), (1, "a", lambda c: None)]
        with self.assertRaises(RuntimeError):
            migrations.migrate(_Cur())

    def test_baseline_is_the_ensure_functions(self):
        self.assertIs(self._saved[0][2], app.ensure_app_schema)
        self.assertEqual(self._saved[0][0], 1)


if __name__ == "__main__":
    unittest.main()
