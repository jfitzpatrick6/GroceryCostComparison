import pandas as pd
import psycopg2
import os

import aldis
import BJs
import tops
import Walmart
import units

# Database Connection
DB_HOST = os.getenv("DB_HOST", "db")
DB_NAME = os.getenv("DB_NAME", "grocery_db")
DB_USER = os.getenv("DB_USER", "user")
DB_PASS = os.getenv("DB_PASS", "password")

# TOPS_STORE/ALDIS_STORE are intentionally not required: tops.py/aldis.py
# don't use them yet (see #43 - Instacart's white-label platform doesn't
# support safe store targeting via a simple store id the way it used to).
REQUIRED_STORE_ENV = ["BJS_STORE", "WALMARTSTORE"]


def get_data():
    """Calls all of the main functions for each scrape, and returns a single DataFrame."""
    missing = [name for name in REQUIRED_STORE_ENV if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing required store id env var(s): {', '.join(missing)}")

    tops_store = os.getenv("TOPS_STORE")
    aldi_store = os.getenv("ALDIS_STORE")
    bjs_store = os.getenv("BJS_STORE")
    walmart_store = os.getenv("WALMARTSTORE")

    aldi = aldis.main(aldi_store)
    aldi['store'] = 'Aldis'
    aldi['store_id'] = aldi_store

    top = tops.main(tops_store)
    top['store'] = 'Tops'
    top['store_id'] = tops_store

    BJ = BJs.main(bjs_store)
    BJ['store'] = 'BJs'
    BJ['store_id'] = bjs_store

    Wal = Walmart.main(walmart_store)
    Wal['store'] = 'Walmart'
    Wal['store_id'] = walmart_store

    total_df = pd.concat([aldi, top, BJ, Wal], ignore_index=True)
    total_df.dropna(how="all", inplace=True)
    total_df['Datetime'] = pd.Timestamp.now()

    return total_df


def _numeric_rate(price, size):
    """Best-effort numeric (unit_price, unit) for a row - never raises, since
    a single bad row (e.g. Walmart's non-numeric price strings, see #12)
    shouldn't break the whole insert."""
    try:
        return units.parse_unit_price(float(price), str(size))
    except (TypeError, ValueError):
        return None, None


def store_data(df):
    """Stores the scraped data in a PostgreSQL database. Raises on failure rather
    than swallowing it, so a broken run is visible instead of silently a no-op."""
    conn = psycopg2.connect(
        host=DB_HOST,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASS
    )
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS grocery_prices (
                        id SERIAL PRIMARY KEY,
                        product TEXT,
                        price NUMERIC,
                        rate TEXT,
                        size TEXT,
                        store TEXT,
                        store_id TEXT,
                        datetime TIMESTAMP
                    );
                """)
                # Idempotent - safe to run every time rather than needing a
                # one-off migration step. ADD COLUMN IF NOT EXISTS is a
                # no-op if these already exist; ALTER COLUMN TYPE...USING
                # is a harmless numeric->numeric cast if price is already
                # NUMERIC (only matters for a table created before this).
                cur.execute("ALTER TABLE grocery_prices ADD COLUMN IF NOT EXISTS unit_price NUMERIC;")
                cur.execute("ALTER TABLE grocery_prices ADD COLUMN IF NOT EXISTS unit TEXT;")
                cur.execute("ALTER TABLE grocery_prices ALTER COLUMN price TYPE NUMERIC USING price::numeric;")
                # Cheap way to get "latest scrape only" per product/store
                # without every consumer re-deriving it (see #11).
                cur.execute("""
                    CREATE OR REPLACE VIEW grocery_prices_latest AS
                    SELECT DISTINCT ON (product, store) *
                    FROM grocery_prices
                    ORDER BY product, store, datetime DESC;
                """)

                for _, row in df.iterrows():
                    unit_price, unit = _numeric_rate(row['Price'], row['Size'])
                    cur.execute("""
                        INSERT INTO grocery_prices (product, price, rate, size, store, store_id, datetime, unit_price, unit)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (row['Product'], row['Price'], row['Rate'], row['Size'], row['store'], row['store_id'], row['Datetime'], unit_price, unit))
        print(f"Inserted {len(df)} rows into grocery_prices.")
    finally:
        conn.close()


def main():
    data = get_data()
    store_data(data)


if __name__ == "__main__":
    main()
