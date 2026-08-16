import datetime
import os
import re

import psycopg2
import psycopg2.extras
from flask import Flask, abort, redirect, render_template, request, url_for

DB_HOST = os.getenv("DB_HOST", "db")
DB_NAME = os.getenv("DB_NAME", "grocery_db")
DB_USER = os.getenv("DB_USER", "user")
DB_PASS = os.getenv("DB_PASS", "password")

SORT_COLUMNS = {
    "unit_price": "unit_price ASC NULLS LAST",
    "product": "product ASC",
    "store": "store ASC",
    "price": "price ASC",
}

app = Flask(__name__)


def get_connection():
    return psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)


def price_data_available(cur):
    """grocery_prices_latest only exists once collector.py has run at least
    one scrape - the webapp doesn't own that schema. Pages that join
    against it need to degrade gracefully (no price data yet) rather than
    500 on a fresh deployment with no scrape history."""
    cur.execute("SELECT to_regclass('grocery_prices_latest') IS NOT NULL AS table_exists")
    return cur.fetchone()["table_exists"]


def ensure_staples_table(cur):
    # Not a trip list (see /staples vs the future grocery list, #22) - just
    # a short "we always buy these" pin list, keyed to exact scraped
    # product/store rows for v1 (see #4; #25 will let this match across
    # stores by product identity instead, later).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS staples (
            id SERIAL PRIMARY KEY,
            product TEXT NOT NULL,
            store TEXT NOT NULL,
            pinned_at TIMESTAMP DEFAULT now(),
            UNIQUE (product, store)
        );
    """)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/prices")
def prices():
    query = request.args.get("q", "").strip()
    sort = request.args.get("sort", "unit_price")
    order_by = SORT_COLUMNS.get(sort, SORT_COLUMNS["unit_price"])

    sql = f"""
        SELECT product, store, price, size, unit_price, unit, datetime
        FROM grocery_prices_latest
        WHERE product ILIKE %s OR store ILIKE %s
        ORDER BY {order_by}
        LIMIT 500
    """
    like_query = f"%{query}%"

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if price_data_available(cur):
                cur.execute(sql, (like_query, like_query))
                rows = cur.fetchall()
            else:
                rows = []
    finally:
        conn.close()

    return render_template("prices.html", rows=rows, query=query, sort=sort)


@app.route("/staples")
def staples():
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            ensure_staples_table(cur)
            conn.commit()
            if price_data_available(cur):
                cur.execute("""
                    SELECT s.product, s.store, p.price, p.size, p.unit_price, p.unit, p.datetime
                    FROM staples s
                    LEFT JOIN grocery_prices_latest p ON p.product = s.product AND p.store = s.store
                    ORDER BY s.product, s.store
                """)
            else:
                cur.execute("""
                    SELECT s.product, s.store, NULL AS price, NULL AS size, NULL AS unit_price, NULL AS unit, NULL AS datetime
                    FROM staples s
                    ORDER BY s.product, s.store
                """)
            rows = cur.fetchall()
    finally:
        conn.close()
    return render_template("staples.html", rows=rows)


@app.route("/staples/pin", methods=["POST"])
def pin_staple():
    product = request.form["product"]
    store = request.form["store"]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            ensure_staples_table(cur)
            cur.execute(
                "INSERT INTO staples (product, store) VALUES (%s, %s) ON CONFLICT (product, store) DO NOTHING",
                (product, store),
            )
        conn.commit()
    finally:
        conn.close()
    return redirect(request.referrer or url_for("prices"))


@app.route("/staples/unpin", methods=["POST"])
def unpin_staple():
    product = request.form["product"]
    store = request.form["store"]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM staples WHERE product = %s AND store = %s", (product, store))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("staples"))


def ensure_list_table(cur):
    # One persistent list (see #22) - not one row per planning session or
    # per week. Weekly-plan ingredients (#28/#29, not built yet) merge into
    # this same table rather than creating a new one.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS grocery_list_items (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            qty TEXT,
            checked BOOLEAN NOT NULL DEFAULT FALSE,
            added_at TIMESTAMP DEFAULT now(),
            checked_at TIMESTAMP
        );
    """)


@app.route("/list")
def grocery_list():
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            ensure_list_table(cur)
            conn.commit()
            cur.execute("SELECT id, name, qty, checked FROM grocery_list_items ORDER BY checked, added_at")
            items = cur.fetchall()

            # Naive exact-name match against the latest scrape - not #25's
            # real cross-store matching (not built yet), just enough to show
            # "cheapest store" when a list item happens to match a product
            # name verbatim. Unmatched items still show, just without this.
            # grocery_prices_latest only exists once a scrape has actually
            # run - skip matching entirely rather than 500ing on a fresh
            # deployment with no scrape history yet.
            if price_data_available(cur):
                for item in items:
                    cur.execute("""
                        SELECT store, price, unit_price, unit
                        FROM grocery_prices_latest
                        WHERE product ILIKE %s
                        ORDER BY unit_price ASC NULLS LAST
                        LIMIT 1
                    """, (item["name"],))
                    item["match"] = cur.fetchone()
            else:
                for item in items:
                    item["match"] = None
    finally:
        conn.close()
    return render_template("list.html", items=items)


@app.route("/list/add", methods=["POST"])
def add_list_item():
    name = request.form.get("name", "").strip()
    qty = request.form.get("qty", "").strip()
    if name:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                ensure_list_table(cur)
                cur.execute("INSERT INTO grocery_list_items (name, qty) VALUES (%s, %s)", (name, qty or None))
            conn.commit()
        finally:
            conn.close()
    return redirect(url_for("grocery_list"))


@app.route("/list/check", methods=["POST"])
def check_list_item():
    item_id = request.form["id"]
    checked = request.form["checked"] == "1"
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE grocery_list_items SET checked = %s, checked_at = CASE WHEN %s THEN now() ELSE NULL END WHERE id = %s",
                (checked, checked, item_id),
            )
            # Restock pantry when checking off (buying), not when un-checking
            # (see #30) - undo doesn't reverse the restock, matching that
            # manual pantry corrections are always available rather than
            # trying to make this perfectly symmetric.
            if checked:
                ensure_pantry_table(cur)
                cur.execute("SELECT name, qty FROM grocery_list_items WHERE id = %s", (item_id,))
                item = cur.fetchone()
                if item:
                    restock_pantry(cur, item[0], item[1])
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("grocery_list"))


@app.route("/list/remove", methods=["POST"])
def remove_list_item():
    item_id = request.form["id"]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM grocery_list_items WHERE id = %s", (item_id,))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("grocery_list"))


def ensure_pantry_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pantry_items (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            amount NUMERIC,
            unit TEXT,
            updated_at TIMESTAMP DEFAULT now()
        );
    """)


_QTY_LINE = re.compile(r"^\s*([\d.]+)\s*(\S*)\s*$")


def restock_pantry(cur, name, qty_text):
    """Adds a checked-off shopping-list item's quantity to pantry (#30).
    Only handles a clean "<number> <unit>" qty - anything else (blank, a
    merged "X + Y" string, free text) is skipped rather than guessed at;
    the item just doesn't auto-restock and can be corrected by hand."""
    if not qty_text:
        return
    match = _QTY_LINE.match(qty_text)
    if not match or not match.group(1):
        return
    amount, unit = match.groups()
    amount = float(amount)
    unit = unit or None

    cur.execute("SELECT id, amount, unit FROM pantry_items WHERE lower(name) = lower(%s)", (name,))
    existing = cur.fetchone()
    if existing and existing[2] == unit:
        cur.execute(
            "UPDATE pantry_items SET amount = %s, updated_at = now() WHERE id = %s",
            (float(existing[1] or 0) + amount, existing[0]),
        )
    elif not existing:
        cur.execute(
            "INSERT INTO pantry_items (name, amount, unit) VALUES (%s, %s, %s)",
            (name, amount, unit),
        )
    # else: existing pantry row has a different unit - don't guess how to
    # combine them, leave it for a manual correction.


@app.route("/pantry")
def pantry():
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            ensure_pantry_table(cur)
            conn.commit()
            cur.execute("SELECT id, name, amount, unit FROM pantry_items ORDER BY name")
            items = cur.fetchall()
    finally:
        conn.close()
    return render_template("pantry.html", items=items)


@app.route("/pantry/set", methods=["POST"])
def set_pantry_item():
    name = request.form.get("name", "").strip()
    amount = request.form.get("amount") or None
    unit = request.form.get("unit", "").strip() or None
    if name:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                ensure_pantry_table(cur)
                cur.execute("SELECT id FROM pantry_items WHERE lower(name) = lower(%s)", (name,))
                existing = cur.fetchone()
                if existing:
                    cur.execute(
                        "UPDATE pantry_items SET amount = %s, unit = %s, updated_at = now() WHERE id = %s",
                        (amount, unit, existing[0]),
                    )
                else:
                    cur.execute(
                        "INSERT INTO pantry_items (name, amount, unit) VALUES (%s, %s, %s)",
                        (name, amount, unit),
                    )
            conn.commit()
        finally:
            conn.close()
    return redirect(url_for("pantry"))


@app.route("/pantry/remove", methods=["POST"])
def remove_pantry_item():
    item_id = request.form["id"]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pantry_items WHERE id = %s", (item_id,))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("pantry"))


@app.route("/list/where-to-buy")
def where_to_buy():
    """The payoff feature (#31): for each unchecked list item, the cheapest
    store; plus a "shop one store" vs "split across stores" total
    comparison. v1 - cheapest per item only, no store-count minimization,
    and matching is the same naive ILIKE stand-in used elsewhere (#25's
    real cross-store matching isn't built yet)."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            ensure_list_table(cur)
            conn.commit()
            cur.execute("SELECT id, name, qty FROM grocery_list_items WHERE checked = FALSE ORDER BY added_at")
            items = cur.fetchall()

            per_item = []
            unmatched = []
            all_stores = set()

            if price_data_available(cur):
                for item in items:
                    cur.execute("""
                        SELECT store, price, size, unit_price, unit
                        FROM grocery_prices_latest
                        WHERE product ILIKE %s
                        ORDER BY unit_price ASC NULLS LAST, price ASC
                    """, (item["name"],))
                    matches = cur.fetchall()
                    if not matches:
                        unmatched.append(item)
                        continue
                    by_store = {m["store"]: m for m in matches}
                    per_item.append({"item": item, "cheapest": matches[0], "by_store": by_store})
                    all_stores.update(by_store.keys())
            else:
                unmatched = list(items)
    finally:
        conn.close()

    split_total = sum(float(p["cheapest"]["price"]) for p in per_item) if per_item else None

    store_totals = []
    for store in sorted(all_stores):
        total = 0.0
        covered = 0
        for p in per_item:
            match = p["by_store"].get(store)
            if match:
                total += float(match["price"])
                covered += 1
        store_totals.append({
            "store": store, "total": total, "covered": covered,
            "of_total": len(per_item), "covers_all": covered == len(per_item),
        })
    store_totals.sort(key=lambda s: (not s["covers_all"], s["total"]))

    return render_template(
        "where_to_buy.html", per_item=per_item, unmatched=unmatched,
        split_total=split_total, store_totals=store_totals,
    )


RECIPE_UNIT_WORDS = (
    "cups?|tbsp|tablespoons?|tsp|teaspoons?|oz|ounces?|lbs?|pounds?|"
    "g|grams?|kg|ml|l|liters?|cloves?|cans?|pinch|dash|each|ea|slices?|pieces?"
)
_INGREDIENT_LINE = re.compile(
    rf"^\s*([\d./]+)?\s*({RECIPE_UNIT_WORDS})?\s*(.*?)\s*$", re.IGNORECASE
)
# Canonical form for each recognized unit - the regex above matches plurals
# and synonyms ("cup"/"cups", "tbsp"/"tablespoon"/"tablespoons") but the raw
# matched text would keep them distinct, which breaks aggregating "2 cups"
# with "1 cup" across recipes in the planner (#28). Normalize once here.
_UNIT_CANONICAL = {
    "cup": "cup", "cups": "cup",
    "tbsp": "tbsp", "tablespoon": "tbsp", "tablespoons": "tbsp",
    "tsp": "tsp", "teaspoon": "tsp", "teaspoons": "tsp",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "g": "g", "gram": "g", "grams": "g",
    "kg": "kg", "ml": "ml",
    "l": "l", "liter": "l", "liters": "l",
    "clove": "clove", "cloves": "clove",
    "can": "can", "cans": "can",
    "pinch": "pinch", "dash": "dash",
    "each": "each", "ea": "each",
    "slice": "slice", "slices": "slice",
    "piece": "piece", "pieces": "piece",
}


def parse_ingredient_line(line):
    """Best-effort split of a free-text ingredient line into (amount, unit,
    name). Falls back to putting the whole line in `name` if it doesn't
    look like "<amount> <unit> <name>" - this is deliberately simple
    (recipe-unit conversion is #36's job, not this)."""
    match = _INGREDIENT_LINE.match(line)
    amount, unit, name = match.groups()
    if not name:
        return None, None, line.strip()
    canonical_unit = _UNIT_CANONICAL.get(unit.lower()) if unit else None
    return amount, canonical_unit, name


def ensure_recipes_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS recipes (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            notes TEXT,
            servings INTEGER,
            created_at TIMESTAMP DEFAULT now()
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS recipe_ingredients (
            id SERIAL PRIMARY KEY,
            recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            amount TEXT,
            unit TEXT,
            sort_order INTEGER NOT NULL
        );
    """)


def _save_ingredients(cur, recipe_id, ingredients_text):
    cur.execute("DELETE FROM recipe_ingredients WHERE recipe_id = %s", (recipe_id,))
    for i, line in enumerate(ingredients_text.splitlines()):
        if not line.strip():
            continue
        amount, unit, name = parse_ingredient_line(line)
        cur.execute(
            "INSERT INTO recipe_ingredients (recipe_id, name, amount, unit, sort_order) VALUES (%s, %s, %s, %s, %s)",
            (recipe_id, name, amount, unit, i),
        )


@app.route("/recipes")
def recipes():
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            ensure_recipes_tables(cur)
            conn.commit()
            cur.execute("""
                SELECT r.id, r.name, r.servings, count(i.id) AS ingredient_count
                FROM recipes r
                LEFT JOIN recipe_ingredients i ON i.recipe_id = r.id
                GROUP BY r.id
                ORDER BY r.name
            """)
            rows = cur.fetchall()
    finally:
        conn.close()
    return render_template("recipes.html", rows=rows)


@app.route("/recipes/new", methods=["GET", "POST"])
def new_recipe():
    if request.method == "GET":
        return render_template("recipe_form.html", recipe=None, ingredients_text="")

    name = request.form.get("name", "").strip()
    notes = request.form.get("notes", "").strip()
    servings = request.form.get("servings") or None
    ingredients_text = request.form.get("ingredients", "")
    if not name:
        abort(400, "Recipe name is required")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            ensure_recipes_tables(cur)
            cur.execute(
                "INSERT INTO recipes (name, notes, servings) VALUES (%s, %s, %s) RETURNING id",
                (name, notes or None, servings),
            )
            recipe_id = cur.fetchone()[0]
            _save_ingredients(cur, recipe_id, ingredients_text)
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("view_recipe", recipe_id=recipe_id))


@app.route("/recipes/<int:recipe_id>")
def view_recipe(recipe_id):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM recipes WHERE id = %s", (recipe_id,))
            recipe = cur.fetchone()
            if not recipe:
                abort(404)
            cur.execute(
                "SELECT name, amount, unit FROM recipe_ingredients WHERE recipe_id = %s ORDER BY sort_order",
                (recipe_id,),
            )
            ingredients = cur.fetchall()
    finally:
        conn.close()
    return render_template("recipe_detail.html", recipe=recipe, ingredients=ingredients)


@app.route("/recipes/<int:recipe_id>/edit", methods=["GET", "POST"])
def edit_recipe(recipe_id):
    conn = get_connection()
    try:
        if request.method == "GET":
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM recipes WHERE id = %s", (recipe_id,))
                recipe = cur.fetchone()
                if not recipe:
                    abort(404)
                cur.execute(
                    "SELECT name, amount, unit FROM recipe_ingredients WHERE recipe_id = %s ORDER BY sort_order",
                    (recipe_id,),
                )
                lines = []
                for ing in cur.fetchall():
                    parts = [p for p in (ing["amount"], ing["unit"], ing["name"]) if p]
                    lines.append(" ".join(parts))
            return render_template("recipe_form.html", recipe=recipe, ingredients_text="\n".join(lines))

        name = request.form.get("name", "").strip()
        notes = request.form.get("notes", "").strip()
        servings = request.form.get("servings") or None
        ingredients_text = request.form.get("ingredients", "")
        if not name:
            abort(400, "Recipe name is required")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE recipes SET name = %s, notes = %s, servings = %s WHERE id = %s",
                (name, notes or None, servings, recipe_id),
            )
            _save_ingredients(cur, recipe_id, ingredients_text)
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("view_recipe", recipe_id=recipe_id))


@app.route("/recipes/<int:recipe_id>/delete", methods=["POST"])
def delete_recipe(recipe_id):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM recipes WHERE id = %s", (recipe_id,))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("recipes"))


DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def ensure_planner_table(cur):
    # Dinner only for v1 ("dinner first" per #28) - `meal` column exists so
    # breakfast/lunch can be added later without a schema change, but the
    # UI only ever writes 'dinner' for now.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS meal_plan_slots (
            id SERIAL PRIMARY KEY,
            week_start DATE NOT NULL,
            day_of_week INTEGER NOT NULL,
            meal TEXT NOT NULL DEFAULT 'dinner',
            recipe_id INTEGER REFERENCES recipes(id) ON DELETE SET NULL,
            UNIQUE (week_start, day_of_week, meal)
        );
    """)


def week_start_for(d):
    """Sunday on or before the given date."""
    return d - datetime.timedelta(days=(d.weekday() + 1) % 7)


@app.route("/planner")
def planner():
    week_param = request.args.get("week")
    if week_param:
        week_start = datetime.date.fromisoformat(week_param)
    else:
        week_start = week_start_for(datetime.date.today())

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            ensure_planner_table(cur)
            ensure_recipes_tables(cur)
            conn.commit()
            cur.execute("""
                SELECT s.day_of_week, r.id AS recipe_id, r.name AS recipe_name
                FROM meal_plan_slots s
                JOIN recipes r ON r.id = s.recipe_id
                WHERE s.week_start = %s AND s.meal = 'dinner'
            """, (week_start,))
            assigned = {row["day_of_week"]: row for row in cur.fetchall()}
            cur.execute("SELECT id, name FROM recipes ORDER BY name")
            all_recipes = cur.fetchall()
    finally:
        conn.close()

    days = [{"index": i, "name": DAY_NAMES[i], "date": week_start + datetime.timedelta(days=i),
             "recipe": assigned.get(i)} for i in range(7)]

    return render_template(
        "planner.html", week_start=week_start, days=days, all_recipes=all_recipes,
        prev_week=week_start - datetime.timedelta(days=7),
        next_week=week_start + datetime.timedelta(days=7),
    )


@app.route("/planner/set", methods=["POST"])
def set_planner_slot():
    week_start = request.form["week_start"]
    day_of_week = int(request.form["day_of_week"])
    recipe_id = request.form.get("recipe_id") or None

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            ensure_planner_table(cur)
            if recipe_id:
                cur.execute("""
                    INSERT INTO meal_plan_slots (week_start, day_of_week, meal, recipe_id)
                    VALUES (%s, %s, 'dinner', %s)
                    ON CONFLICT (week_start, day_of_week, meal)
                    DO UPDATE SET recipe_id = EXCLUDED.recipe_id
                """, (week_start, day_of_week, recipe_id))
            else:
                cur.execute(
                    "DELETE FROM meal_plan_slots WHERE week_start = %s AND day_of_week = %s AND meal = 'dinner'",
                    (week_start, day_of_week),
                )
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("planner", week=week_start))


def get_week_ingredients(cur, week_start):
    """Combined (name, unit, amount) list for a planned week. Sums amounts
    where numeric and sharing a unit, otherwise lists them separately -
    see #28, exact unit math is explicitly OK to be sloppy for v1 (real
    recipe-unit handling is #36's job later)."""
    cur.execute("""
        SELECT i.name, i.amount, i.unit
        FROM meal_plan_slots s
        JOIN recipe_ingredients i ON i.recipe_id = s.recipe_id
        WHERE s.week_start = %s AND s.meal = 'dinner'
        ORDER BY i.name
    """, (week_start,))
    ingredient_rows = cur.fetchall()

    grouped = {}
    for row in ingredient_rows:
        key = (row["name"].lower(), row["unit"])
        grouped.setdefault(key, {"name": row["name"], "unit": row["unit"], "amounts": []})
        if row["amount"]:
            grouped[key]["amounts"].append(row["amount"])

    combined = []
    for entry in grouped.values():
        total = 0.0
        all_numeric = True
        for amt in entry["amounts"]:
            try:
                total += float(amt)
            except ValueError:
                all_numeric = False
                break
        if entry["amounts"] and all_numeric:
            display_amount = str(total).rstrip("0").rstrip(".") if "." in str(total) else str(total)
        elif entry["amounts"]:
            display_amount = " + ".join(entry["amounts"])
        else:
            display_amount = ""
        combined.append({"name": entry["name"], "unit": entry["unit"], "amount": display_amount})
    combined.sort(key=lambda e: e["name"].lower())
    return combined


def apply_pantry(cur, combined):
    """Annotates each combined ingredient with pantry coverage (#30) - skip
    or reduce items the pantry already has enough of, always showing what
    was skipped/reduced rather than silently dropping it. Only compares
    when both the need and the pantry have a clean numeric amount and the
    exact same unit; anything else is left as a full need - conservative
    on purpose, never guesses its way into subtracting the wrong thing."""
    ensure_pantry_table(cur)
    for ing in combined:
        ing["pantry_have"] = None
        ing["need_amount"] = ing["amount"]
        try:
            needed = float(ing["amount"])
        except (TypeError, ValueError):
            continue
        cur.execute("SELECT amount, unit FROM pantry_items WHERE lower(name) = lower(%s)", (ing["name"],))
        row = cur.fetchone()
        if not row or row["amount"] is None or row["unit"] != ing["unit"]:
            continue
        have = float(row["amount"])
        ing["pantry_have"] = have
        remaining = max(0.0, needed - have)
        ing["need_amount"] = str(remaining).rstrip("0").rstrip(".") if "." in str(remaining) else str(remaining)
    return combined


@app.route("/planner/ingredients")
def planner_ingredients():
    week_start = request.args.get("week") or week_start_for(datetime.date.today())
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            ensure_planner_table(cur)
            conn.commit()
            combined = get_week_ingredients(cur, week_start)
            apply_pantry(cur, combined)
    finally:
        conn.close()
    return render_template("planner_ingredients.html", week_start=week_start, combined=combined)


_QTY_LINE = re.compile(r"^\s*([\d.]+)\s*(\S*)\s*$")


def merge_qty(existing_qty, new_amount, new_unit):
    """Combines a grocery-list item's free-text qty with a new amount/unit
    from the planner. Sums when both are numeric and share a unit,
    otherwise concatenates rather than guessing or dropping data."""
    new_qty = f"{new_amount} {new_unit}".strip() if new_amount else (new_unit or "")
    if not existing_qty:
        return new_qty or None
    if not new_qty:
        return existing_qty

    existing_match = _QTY_LINE.match(existing_qty)
    if existing_match and new_amount:
        existing_amount, existing_unit = existing_match.groups()
        if existing_unit == (new_unit or ""):
            try:
                total = float(existing_amount) + float(new_amount)
                total_str = str(total).rstrip("0").rstrip(".") if "." in str(total) else str(total)
                return f"{total_str} {existing_unit}".strip()
            except ValueError:
                pass
    return f"{existing_qty} + {new_qty}"


@app.route("/planner/add_to_list", methods=["POST"])
def add_week_to_list():
    week_start = request.form["week_start"]
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            ensure_planner_table(cur)
            ensure_list_table(cur)
            conn.commit()
            combined = get_week_ingredients(cur, week_start)
            apply_pantry(cur, combined)

            for ing in combined:
                # Fully covered by pantry (#30) - don't add it to the list.
                if ing["pantry_have"] is not None and ing["need_amount"] == "0":
                    continue
                amount, unit = ing["need_amount"], ing["unit"]

                cur.execute(
                    "SELECT id, qty FROM grocery_list_items WHERE lower(name) = lower(%s) AND checked = FALSE LIMIT 1",
                    (ing["name"],),
                )
                existing = cur.fetchone()
                if existing:
                    merged_qty = merge_qty(existing["qty"], amount, unit)
                    cur.execute("UPDATE grocery_list_items SET qty = %s WHERE id = %s", (merged_qty, existing["id"]))
                else:
                    qty = f"{amount} {unit}".strip() if amount else (unit or None)
                    cur.execute("INSERT INTO grocery_list_items (name, qty) VALUES (%s, %s)", (ing["name"], qty))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("grocery_list"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
