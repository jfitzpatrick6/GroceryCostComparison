import os

import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request

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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
