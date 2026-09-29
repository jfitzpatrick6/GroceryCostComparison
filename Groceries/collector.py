import os
import sys
import time

import pandas as pd
import psycopg2

import aldis
import BJs
import retention
import run_status
import tops
import units
import Walmart

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
    outcomes = []
    for store_name, scrape_fn, store_id in stores:
        try:
            df = _timed_scrape(store_name, scrape_fn, store_id)
        except Exception as e:
            print(f"[{store_name}] scrape failed, skipping this store: {e}")
            outcomes.append((store_name, None, e))
            continue
        outcomes.append((store_name, len(df), None))
        df['store'] = store_name
        df['store_id'] = store_id
        frames.append(df)

    # Printed before the all-failed raise below, so a FAILED run still gets
    # its one-line verdict in the log (#63).
    print(run_status.run_summary(outcomes)[1])

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
                # Every price-derived page reads through that view, and
                # DISTINCT ON + ORDER BY with no supporting index means
                # Postgres scans and sorts the entire table on each read
                # (#65). datetime DESC is load-bearing rather than
                # decorative here: the view orders product/store ascending
                # but datetime descending, and a backwards index scan
                # reverses *every* key column, so a plain (product, store,
                # datetime) index cannot produce that ordering in either
                # direction. Placed after the ALTER COLUMN TYPE above because
                # that statement rewrites the table on the one run where
                # price isn't NUMERIC yet, and a rewrite rebuilds every index
                # on the table - building afterwards avoids building it twice.
                # IF NOT EXISTS makes every scrape after the first a no-op;
                # not CONCURRENTLY because that can't run in a transaction.
                # The plain form takes only a SHARE lock on this table, which
                # blocks writers (the scraper is the only one) and not readers -
                # but that is a statement about THIS statement, not about the
                # transaction it sits in. This block opens with ADD COLUMN IF NOT
                # EXISTS and an ALTER COLUMN TYPE, and those take
                # AccessExclusiveLock even when they change nothing (measured on
                # postgres:16, and recorded in CONTRIBUTING §8), so a concurrent
                # webapp read CAN block here for the duration. Pre-existing
                # behaviour, not introduced by the index; #66 removes the whole
                # category by moving DDL out of the request path and into
                # startup. Build cost measured: 190 ms at 46k rows, 9.3 s at 4.2M.
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_grocery_prices_product_store_datetime
                    ON grocery_prices (product, store, datetime DESC);
                """)

                for _, row in df.iterrows():
                    unit_price, unit = _numeric_rate(row['Price'], row['Size'])
                    cur.execute("""
                        INSERT INTO grocery_prices
                            (product, price, rate, size, store, store_id, datetime, unit_price, unit)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        row['Product'], row['Price'], row['Rate'], row['Size'],
                        row['store'], row['store_id'], row['Datetime'],
                        unit_price, unit,
                    ))
        print(f"Inserted {len(df)} rows into grocery_prices.")

        # Retention (#65), after the scrape's own transaction has committed and
        # in a separate one. Prices are what the app can't work without and
        # retention is hygiene, so a failure here must not be able to take a
        # successful scrape down with it - #24's "one store's scraper throwing
        # shouldn't discard the others' results", applied to the ingest/cleanup
        # split instead of the store/store split. Running it inside the
        # transaction above wouldn't be safe to wrap in a try either: a failed
        # statement aborts the whole transaction, so the inserts would be lost
        # even with the exception caught. Rows this run just wrote carry the
        # newest datetime in the table, so they can never fall on the old side
        # of a cutoff derived from it.
        try:
            with conn:
                with conn.cursor() as cur:
                    retention.prune(cur)
        except Exception as e:
            print(f"[retention] prune failed - this run's prices are already committed, nothing deleted: {e}")
    finally:
        conn.close()


def newest_price_time():
    """max(datetime) in grocery_prices, or None if the table is empty or does
    not exist yet (a fresh deploy, before the first scrape has created it)."""
    conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('grocery_prices')")
            if cur.fetchone()[0] is None:
                return None
            cur.execute("SELECT max(datetime) FROM grocery_prices")
            return cur.fetchone()[0]
    finally:
        conn.close()


def main():
    # --if-stale: scheduler_entrypoint.sh runs this once at container start.
    # cron never catches up on a missed 03:00, so a host that was off or
    # rebooting then silently skipped a day, and a fresh deploy showed an empty
    # app until the next 03:00 (#63). Scrape now only if prices are actually old.
    if "--if-stale" in sys.argv[1:]:
        newest = newest_price_time()
        if not run_status.is_stale(newest):
            print(f"Startup check: newest prices are from {newest}, not stale - no catch-up scrape.")
            return
        print(f"Startup check: newest prices are from {newest or 'never'} - running a catch-up scrape.")
    data = get_data()
    store_data(data)


if __name__ == "__main__":
    main()
