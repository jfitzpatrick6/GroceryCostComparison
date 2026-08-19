import time

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


def _timed_scrape(store_name, scrape_fn, store_id):
    """Runs one scraper, logging item count / elapsed time / ms per item.
    Lets exceptions propagate as before - a broken scraper should still
    abort the run loudly (see #24), this only adds visibility into runs
    that do complete."""
    start = time.monotonic()
    df = scrape_fn(store_id)
    elapsed = time.monotonic() - start
    count = len(df)
    ms_per_item = f"{elapsed * 1000 / count:.0f} ms/item" if count else "n/a"
    print(f"[{store_name}] {count} items in {elapsed:.1f}s ({ms_per_item})")
    return df


def get_data():
    """Calls all of the main functions for each scrape, and returns a single DataFrame.

    Each store is scraped independently - one store's scraper throwing (e.g.
    Walmart's bot wall, see #12) shouldn't discard the other stores' results
    for the run."""
    missing = [name for name in REQUIRED_STORE_ENV if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing required store id env var(s): {', '.join(missing)}")

    stores = [
        ("Aldis", aldis.main, os.getenv("ALDIS_STORE")),
        ("Tops", tops.main, os.getenv("TOPS_STORE")),
        ("BJs", BJs.main, os.getenv("BJS_STORE")),
        ("Walmart", Walmart.main, os.getenv("WALMARTSTORE")),
    ]

    run_start = time.monotonic()

    frames = []
    for store_name, scrape_fn, store_id in stores:
        try:
            df = _timed_scrape(store_name, scrape_fn, store_id)
        except Exception as e:
            print(f"[{store_name}] scrape failed, skipping this store: {e}")
            continue
        df['store'] = store_name
        df['store_id'] = store_id
        frames.append(df)

    if not frames:
        raise RuntimeError("Every store's scraper failed - nothing to store.")

    total_df = pd.concat(frames, ignore_index=True)
    total_df.dropna(how="all", inplace=True)
    total_df['Datetime'] = pd.Timestamp.now()

    total_elapsed = time.monotonic() - run_start
    print(f"Total: {len(total_df)} items across all stores in {total_elapsed:.1f}s")

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
                # Postgres refuses ALTER COLUMN TYPE on a column any view
                # depends on, even for a no-op cast (see #53) - drop the
                # view first since it gets unconditionally recreated right
                # after anyway, so every scrape after the first one doesn't
                # hard-fail here.
                cur.execute("DROP VIEW IF EXISTS grocery_prices_latest;")
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
