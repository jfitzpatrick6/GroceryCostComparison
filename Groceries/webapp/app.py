import os

import psycopg2
import psycopg2.extras
from flask import Flask, redirect, render_template, request, url_for

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
            cur.execute(sql, (like_query, like_query))
            rows = cur.fetchall()
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
            cur.execute("""
                SELECT s.product, s.store, p.price, p.size, p.unit_price, p.unit, p.datetime
                FROM staples s
                LEFT JOIN grocery_prices_latest p ON p.product = s.product AND p.store = s.store
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
            for item in items:
                cur.execute("""
                    SELECT store, price, unit_price, unit
                    FROM grocery_prices_latest
                    WHERE product ILIKE %s
                    ORDER BY unit_price ASC NULLS LAST
                    LIMIT 1
                """, (item["name"],))
                item["match"] = cur.fetchone()
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
