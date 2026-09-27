"""Schema-creation tests for app.py's ensure_*_table() functions (#80, #82).

Two related bugs, same root cause - schema creation scattered through request
handlers, so "does this table exist yet" depended on which URL was opened first:

#80 was an ordering bug. meal_plan_slots declares
`recipe_id REFERENCES recipes(id)`, but it is created by ensure_planner_table(),
which on a fresh database ran before anything created `recipes`. The CREATE
failed with UndefinedTable, the transaction aborted and rolled back the `recipes`
table created moments later in that same transaction, and every subsequent
request repeated the identical failure - so a first-time deploy had a permanently
broken /planner, /history and seven other routes.

#82 was an absence bug. Three routes - GET /recipes/<id>, GET /recipes/<id>/edit
and POST /recipes/<id>/delete - query `recipes` while calling no ensure_*
function at all, so they 500'd on a fresh database and worked only because some
other route had been visited first. Fixed by ensure_app_schema(), which
init_schema.py runs once at container startup.

Both went unnoticed because the dev database had been populated for weeks.

Why a fake cursor rather than a real Postgres: CONTRIBUTING §7 requires the CI
tier to pass without a database, and CI has none. So this emulates the one piece
of DDL semantics the bugs depended on - a CREATE TABLE whose REFERENCES target
does not exist yet fails, and an ALTER TABLE on a nonexistent table fails - and
nothing else. It is not a Postgres simulator and does not try to be; in
particular it models no transactions, so the rollback that made #80 *permanent*
is not covered here. That was verified against a real empty postgres:16 instead,
per the PR description.

That makes test_the_harness_rejects_a_missing_fk_target load-bearing rather than
decorative: if the fake ever stops enforcing the dependency, every other test
here would pass vacuously and this suite would silently stop protecting anything.
"""

import ast
import inspect
import re
import unittest
from typing import ClassVar

from psycopg2 import errors

import app

_CREATE_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.IGNORECASE)
_REFERENCES = re.compile(r"REFERENCES\s+(\w+)", re.IGNORECASE)
_ALTER_TABLE = re.compile(r"ALTER\s+TABLE\s+(\w+)", re.IGNORECASE)
# Table names app.py reads or writes. Used by the "does anything create this?"
# invariant test below; deliberately matches the four keywords that introduce a
# table name in this codebase's SQL rather than trying to be a SQL parser.
_SQL_TABLE = re.compile(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+([a-z_][a-z0-9_]*)", re.IGNORECASE)
_SQL_STATEMENT = re.compile(
    r"\b(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE\s+TABLE|ALTER\s+TABLE)\b",
    re.IGNORECASE,
)
# Words that can legitimately follow FROM/INTO/UPDATE/JOIN in real SQL without
# being a table name. Kept minimal and explicit on purpose: the point of the
# invariant test is to notice a table nobody creates, so silently swallowing
# names would defeat it. `set` is the only one this codebase produces today,
# from `ON CONFLICT ... DO UPDATE SET`.
_SQL_KEYWORDS = {"set"}


def _docstring_constants(tree):
    """The AST nodes that are docstrings, so they can be excluded from the scan.

    A docstring is not SQL, but it can quote SQL - ensure_app_schema's mentions
    "CREATE TABLE IF NOT EXISTS" in prose, which made it look like a statement,
    and the phrase "not from a request hook" in that same docstring then
    contributed a table named "a". Excluding docstrings removes that whole class
    of false positive instead of playing whack-a-mole with stopwords.
    """
    nodes = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            nodes.add(id(first.value))
    return nodes


def _tables_in_source(source):
    """The extraction half of _referenced_tables, split out so it can be tested
    against fabricated source rather than only against app.py."""
    tree = ast.parse(source)
    docstrings = _docstring_constants(tree)
    found = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in docstrings:
            continue
        if not _SQL_STATEMENT.search(node.value):
            continue
        found.update(m.lower() for m in _SQL_TABLE.findall(node.value))
    # `SET` is captured by the UPDATE branch on `ON CONFLICT ... DO UPDATE SET`,
    # which is valid SQL and not a table. Keywords are the only legitimate
    # capture left once prose is excluded, so this list stays short and explicit.
    return found - _SQL_KEYWORDS


def _referenced_tables(module):
    """Table names read or written by SQL in `module`'s string literals.

    Scans AST string constants that look like SQL, not the raw source, and skips
    docstrings. Matching the raw file also picked up ordinary English prose in
    comments - "from the", "into a", "update the" - which yielded eighteen bogus
    table names and made the invariant test useless.
    """
    return _tables_in_source(inspect.getsource(module))


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


class EnsureAppSchemaTests(unittest.TestCase):
    """#82: the whole app-owned schema must be creatable in one call, from nothing.

    Three routes (GET /recipes/<id>, GET /recipes/<id>/edit, POST
    /recipes/<id>/delete) query `recipes` while calling no ensure_* function at
    all, so they 500'd on a fresh database and worked only because some other
    route had been visited first. init_schema.py now calls ensure_app_schema()
    once at container startup, so navigation order no longer decides whether a
    table exists.
    """

    # Tables app.py may query that this app does not own and must not create.
    # ClassVar because these are shared constants, not per-instance mutable
    # state - which is what ruff's RUF012 is protecting against.
    NOT_APP_OWNED: ClassVar[set] = {
        # A view collector.py creates on the first scrape. Routes guard access
        # with price_data_available() precisely because the webapp doesn't own
        # this schema and it may not exist yet.
        "grocery_prices_latest",
        "grocery_prices",
        # Postgres system catalogs, queried by the DO block in
        # ensure_planner_table and by price_data_available().
        "pg_constraint",
        "pg_class",
    }

    EXPECTED_TABLES: ClassVar[set] = {
        "recipes",
        "recipe_ingredients",
        "meal_plan_slots",
        "meal_plan_extras",
        "cook_depletions",
        "profiles",
        "staples",
        "grocery_list_items",
        "pantry_items",
        "ingredient_conversions",
    }

    def test_creates_every_app_owned_table_from_empty(self):
        cur = _SchemaTrackingCursor()
        app.ensure_app_schema(cur)
        self.assertEqual(set(cur.tables), self.EXPECTED_TABLES)

    def test_recipes_exists_before_the_table_that_references_it(self):
        cur = _SchemaTrackingCursor()
        app.ensure_app_schema(cur)
        self.assertLess(cur.tables.index("recipes"), cur.tables.index("meal_plan_slots"))

    def test_is_idempotent(self):
        # init_schema.py is documented as safe to re-run by hand, and the
        # container will restart against an already-populated database.
        cur = _SchemaTrackingCursor()
        app.ensure_app_schema(cur)
        first = list(cur.tables)
        app.ensure_app_schema(cur)
        self.assertEqual(cur.tables, first)

    def test_every_table_any_route_queries_is_created_or_explicitly_not_ours(self):
        """The invariant that would have caught #82 in CI.

        Extracts every table name appearing after FROM/INTO/UPDATE/JOIN in
        app.py's source and asserts each is either created by
        ensure_app_schema() or listed in NOT_APP_OWNED. Without this, the next
        route written against a table nobody creates passes review, passes CI,
        and 500s on someone's first deploy - which is exactly how #82 happened.

        Deliberately source-level rather than request-level: it needs no
        database, so it runs in the required CI tier, and it covers routes that
        are awkward to drive with a test client (a DELETE needing a valid id, a
        branch behind a form post).
        """
        source_tables = _referenced_tables(app)
        self.assertTrue(source_tables, "found no table references - the extraction has stopped matching")

        cur = _SchemaTrackingCursor()
        app.ensure_app_schema(cur)
        created = set(cur.tables)

        unaccounted = source_tables - created - self.NOT_APP_OWNED
        self.assertEqual(
            unaccounted,
            set(),
            "these tables are queried by app.py but created by nothing: "
            f"{sorted(unaccounted)}. Add them to ensure_app_schema(), or to "
            "NOT_APP_OWNED with a comment saying whose they are.",
        )

    def test_the_routes_that_broke_get_their_dependency(self):
        # Named explicitly so the link to #82 stays visible: these are the three
        # routes that 500'd, and `recipes` is what all of them need.
        cur = _SchemaTrackingCursor()
        app.ensure_app_schema(cur)
        self.assertIn("recipes", cur.tables)
        self.assertIn("recipe_ingredients", cur.tables)

    def test_the_invariant_arithmetic_has_teeth(self):
        # Guards the guard. An earlier version of this test just asserted that a
        # made-up name wasn't in two sets, which is trivially true and proved
        # nothing. This instead runs the same set arithmetic the real invariant
        # uses, on a referenced-set that includes a table nothing creates, and
        # asserts it is reported. If this ever passes while the invariant test
        # has gone vacuous, the invariant is not protecting anything.
        cur = _SchemaTrackingCursor()
        app.ensure_app_schema(cur)
        created = set(cur.tables)
        fabricated = created | {"a_table_nothing_creates"}
        self.assertEqual(fabricated - created - self.NOT_APP_OWNED, {"a_table_nothing_creates"})

    def test_table_extraction_finds_real_sql_and_ignores_prose(self):
        # The invariant is only as good as the extraction feeding it, so test
        # the extraction directly against source written for the purpose rather
        # than only against app.py (where a silent failure would just look like
        # everything being fine).
        source = '''
def a_route(cur):
    """Docstrings quote SQL: SELECT * FROM not_a_real_table."""
    cur.execute("SELECT id FROM widgets WHERE x = %s", (1,))
    cur.execute("INSERT INTO gadgets (a) VALUES (%s)", (1,))
    cur.execute("UPDATE doohickeys SET a = 1")
    cur.execute("DELETE FROM thingamajigs WHERE id = %s", (1,))
    cur.execute("SELECT 1 FROM inventory i JOIN stock s ON s.id = i.id")
    cur.execute("INSERT INTO t (a) VALUES (1) ON CONFLICT (a) DO UPDATE SET b = 2")
    # a comment mentioning FROM prose_tables is not SQL and must be ignored
'''
        self.assertEqual(
            _tables_in_source(source),
            {"widgets", "gadgets", "doohickeys", "thingamajigs", "inventory", "stock", "t"},
        )


if __name__ == "__main__":
    unittest.main()
