"""Schema-creation ordering tests for app.py's ensure_*_table() functions (#80).

The bug this guards: meal_plan_slots declares `recipe_id REFERENCES recipes(id)`,
but it is created by ensure_planner_table(), which on a fresh database ran before
anything created `recipes`. The CREATE failed with UndefinedTable, the
transaction aborted and rolled back the `recipes` table created moments later in
that same transaction, and every subsequent request repeated the identical
failure - so a first-time deploy had a permanently broken /planner, /history and
seven other routes. It went unnoticed because the dev database had been populated
for weeks.

Why a fake cursor rather than a real Postgres: CONTRIBUTING §7 requires the CI
tier to pass without a database, and CI has none. So this emulates the one piece
of DDL semantics the bug depends on - a CREATE TABLE whose REFERENCES target does
not exist yet fails, and an ALTER TABLE on a nonexistent table fails - and nothing
else. It is not a Postgres simulator and does not try to be.

That makes test_the_harness_rejects_a_missing_fk_target load-bearing rather than
decorative: if the fake ever stops enforcing the dependency, every other test
here would pass vacuously and this suite would silently stop protecting anything.
"""

import inspect
import re
import unittest

from psycopg2 import errors

import app

_CREATE_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.IGNORECASE)
_REFERENCES = re.compile(r"REFERENCES\s+(\w+)", re.IGNORECASE)
_ALTER_TABLE = re.compile(r"ALTER\s+TABLE\s+(\w+)", re.IGNORECASE)


class _SchemaTrackingCursor:
    """Stands in for a psycopg2 cursor, tracking which tables exist.

    Raises the same exception class Postgres would (UndefinedTable, a
    psycopg2.Error subclass) so a caller that catches psycopg2.Error behaves
    here as it does in production.
    """

    def __init__(self):
        self.tables = []  # in creation order
        self.executed = []

    # The other ensure_* callers in app.py pass cursor_factory=...; these
    # functions only ever use execute(), so nothing else needs implementing.
    def execute(self, sql, params=None):
        self.executed.append(sql)

        created = _CREATE_TABLE.search(sql)
        if created:
            table = created.group(1)
            for ref in _REFERENCES.findall(sql):
                if ref not in self.tables:
                    raise errors.UndefinedTable(
                        f'relation "{ref}" does not exist '
                        f"(needed by {table}; created so far: {self.tables or 'none'})"
                    )
            # IF NOT EXISTS is a no-op when the table is already there, which is
            # what makes these functions safe to call on every request.
            if table not in self.tables:
                self.tables.append(table)
            return

        for table in _ALTER_TABLE.findall(sql):
            if table not in self.tables:
                raise errors.UndefinedTable(
                    f'relation "{table}" does not exist (ALTER on a table never created)'
                )


def _ensure_functions():
    """Every ensure_*_table function in app.py, discovered rather than listed.

    Listing them by hand is how a tenth function gets added with an unsatisfied
    dependency and no test notices. Filtering on the (cur) signature keeps this
    from picking up anything that isn't one of these schema helpers.
    """
    found = []
    for name, obj in vars(app).items():
        if not name.startswith("ensure_") or not callable(obj):
            continue
        try:
            params = list(inspect.signature(obj).parameters)
        except (TypeError, ValueError):
            continue
        if params == ["cur"]:
            found.append((name, obj))
    return sorted(found)


class HarnessSelfTests(unittest.TestCase):
    def test_the_harness_rejects_a_missing_fk_target(self):
        # Without this, the whole file could pass because the fake stopped
        # enforcing anything. Asserts the harness fails on exactly the shape of
        # the #80 bug: creating a table that references one not yet created.
        cur = _SchemaTrackingCursor()
        with self.assertRaises(errors.UndefinedTable):
            cur.execute("CREATE TABLE IF NOT EXISTS child (id SERIAL, parent_id INTEGER REFERENCES parent(id))")
        self.assertEqual(cur.tables, [], "a failed CREATE must not register the table")

    def test_the_harness_accepts_a_satisfied_fk_target(self):
        cur = _SchemaTrackingCursor()
        cur.execute("CREATE TABLE IF NOT EXISTS parent (id SERIAL PRIMARY KEY)")
        cur.execute("CREATE TABLE IF NOT EXISTS child (id SERIAL, parent_id INTEGER REFERENCES parent(id))")
        self.assertEqual(cur.tables, ["parent", "child"])

    def test_the_harness_rejects_alter_on_an_unknown_table(self):
        cur = _SchemaTrackingCursor()
        with self.assertRaises(errors.UndefinedTable):
            cur.execute("ALTER TABLE nope ADD COLUMN IF NOT EXISTS x TEXT")

    def test_the_harness_finds_the_real_ensure_functions(self):
        # Guards the discovery in _ensure_functions(): if it silently matched
        # nothing, every test below would pass without testing any code.
        names = [name for name, _ in _ensure_functions()]
        self.assertIn("ensure_planner_table", names)
        self.assertIn("ensure_recipes_tables", names)
        self.assertGreaterEqual(len(names), 9, f"expected the known schema helpers, got {names}")


class SchemaCreationOrderTests(unittest.TestCase):
    def test_ensure_planner_table_creates_recipes_before_meal_plan_slots(self):
        """The direct #80 regression. Fails on the unfixed code with
        UndefinedTable: relation "recipes" does not exist."""
        cur = _SchemaTrackingCursor()
        app.ensure_planner_table(cur)
        self.assertIn("recipes", cur.tables)
        self.assertIn("meal_plan_slots", cur.tables)
        self.assertLess(
            cur.tables.index("recipes"),
            cur.tables.index("meal_plan_slots"),
            "recipes must be created before the table whose FK points at it",
        )

    def test_every_ensure_function_survives_an_empty_database(self):
        """Each schema helper must be safe as the very first thing run against a
        brand-new database, in any order. This is the general form of the #80
        bug: a fresh deploy hits whichever route the user opens first, and there
        is no guaranteed order. A fresh cursor per function so each one really
        does start from nothing."""
        names = [name for name, _ in _ensure_functions()]
        for name, fn in _ensure_functions():
            with self.subTest(function=name):
                cur = _SchemaTrackingCursor()
                fn(cur)  # must not raise
                self.assertTrue(cur.tables, f"{name} created nothing")
        # Sanity check on the route order that actually broke: /planner called
        # ensure_planner_table first, so that pairing is the one that matters.
        self.assertIn("ensure_planner_table", names)

    def test_ensure_functions_are_idempotent(self):
        """They run on nearly every request (#66), so calling one twice against
        the same database must be a no-op rather than an error."""
        for name, fn in _ensure_functions():
            with self.subTest(function=name):
                cur = _SchemaTrackingCursor()
                fn(cur)
                first = list(cur.tables)
                fn(cur)
                self.assertEqual(cur.tables, first, f"{name} changed the schema on its second run")

    def test_planner_route_order_now_works_from_empty(self):
        """Replays /planner's exact ensure sequence from an empty database.
        Before the fix this was the failing path; the sequence is unchanged -
        the fix made ensure_planner_table satisfy its own dependency."""
        cur = _SchemaTrackingCursor()
        app.ensure_planner_table(cur)
        app.ensure_recipes_tables(cur)
        app.ensure_planner_extras_table(cur)
        app.ensure_cook_depletions_table(cur)
        for table in ("recipes", "recipe_ingredients", "meal_plan_slots", "meal_plan_extras", "cook_depletions"):
            self.assertIn(table, cur.tables)


if __name__ == "__main__":
    unittest.main()
