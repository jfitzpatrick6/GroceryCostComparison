import pandas as pd
import psycopg2
import os

import aldis
import BJs
import tops
import Walmart

# Database Connection
DB_HOST = os.getenv("DB_HOST", "db")
DB_NAME = os.getenv("DB_NAME", "grocery_db")
DB_USER = os.getenv("DB_USER", "user")
DB_PASS = os.getenv("DB_PASS", "password")


def get_data():
    """Calls all of the main functions for each scrape, and returns a single DataFrame."""
    with open("1.env", 'r') as f:
        env = f.read().split('\n')

    aldi = aldis.main(env[1].split("=")[1])
    aldi['Store'] = 'Aldis'
    top = tops.main(env[0].split("=")[1])
    top['Store'] = 'Tops'
    BJ = BJs.main(env[2].split("=")[1])
    BJ['Store'] = 'BJs'
    Wal = Walmart.main(env[3].split("=")[1])
    Wal['Store'] = 'Walmart'

    total_df = pd.concat([aldi, top, BJ, Wal], ignore_index=True)
    total_df['Datetime'] = pd.Timestamp.now()

    return total_df


def store_data(df):
    """Stores the scraped data in a PostgreSQL database."""
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASS
        )
        cur = conn.cursor()

        # Ensure table exists
        cur.execute("""
            CREATE TABLE IF NOT EXISTS grocery_prices (
                id SERIAL PRIMARY KEY,
                product TEXT,
                price NUMERIC,
                rate TEXT,
                size TEXT,
                store TEXT,
                datetime TIMESTAMP
            );
        """)
        conn.commit()

        # Insert data
        for _, row in df.iterrows():
            cur.execute("""
                INSERT INTO grocery_prices (product, price, rate, size, store, datetime)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (row['Product'], row['Price'], row['Rate'], row['Size'], row['Store'], row['Datetime']))

        conn.commit()
        cur.close()
        conn.close()

    except Exception as e:
        print("Error storing data:", e)


def main():
    data = get_data()
    store_data(data)


if __name__ == "__main__":
    main()
