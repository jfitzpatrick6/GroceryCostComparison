"""Creates the webapp's own schema once, at container startup, before gunicorn
serves a single request (#82).

A separate process rather than a Flask hook or a gunicorn post_fork hook, because
it has to happen exactly once and serially. Two gunicorn workers each running
CREATE TABLE IF NOT EXISTS against an empty database race each other, and that is
not atomic in Postgres - observed during #61's concurrency testing as
DuplicateTable and UniqueViolation on the pg_class/pg_type catalog indexes.
Running it here, before any worker exists, removes the race entirely rather than
trying to win it.

Retries instead of failing fast, because the database may still be starting.
Since #71 the webapp waits on `depends_on: {db: {condition: service_healthy}}`,
so in the normal path Postgres is already accepting connections by the time this
runs - the retry loop is belt-and-braces for the cases that gate does not cover
(a manual `docker compose run`, a db that becomes unhealthy again mid-startup, or
someone running this script by hand). It gives up loudly once the budget is spent
- a container that starts serving with no schema is worse than one that exits and
gets restarted by `restart: unless-stopped`, because the first presents as a
mystery 500 on every page while the second is visible in `docker ps`.

Run directly for a manual one-off against an existing database; it is idempotent.
"""

import os
import sys
import time

import psycopg2

import app
import migrations

RETRIES = int(os.getenv("INIT_SCHEMA_RETRIES", "30"))
DELAY_SECONDS = float(os.getenv("INIT_SCHEMA_DELAY_SECONDS", "2"))
# Without this, a DB_HOST that black-holes packets (wrong host, firewall drop,
# network not up yet) blocks libpq indefinitely rather than failing, so the
# retry loop below never advances and the container sits "Up" having created
# nothing - which looks exactly like success until someone notices every page
# 500s. A refused connection or an unresolvable name already fails fast; this
# covers the case that doesn't.
CONNECT_TIMEOUT = int(os.getenv("INIT_SCHEMA_CONNECT_TIMEOUT", "5"))


def main():
    last_error = None
    for attempt in range(1, RETRIES + 1):
        conn = None
        try:
            conn = app.get_connection(connect_timeout=CONNECT_TIMEOUT)
            with conn.cursor() as cur:
                applied = migrations.migrate(cur)
                version = migrations.current_version(cur)
            conn.commit()
            done = f"applied {applied}" if applied else "nothing to apply"
            print(f"init_schema: app schema at version {version}, {done} (attempt {attempt})", flush=True)
            return 0
        except psycopg2.OperationalError as e:
            # Could not connect - the retryable case (db still starting).
            last_error = e
            print(
                f"init_schema: database not ready (attempt {attempt}/{RETRIES}): {e}",
                flush=True,
            )
            time.sleep(DELAY_SECONDS)
        except psycopg2.Error as e:
            # Connected, but the DDL itself failed. Retrying will not help, so
            # fail loudly and let the container exit rather than come up serving
            # requests that all 500.
            print(f"init_schema: schema creation failed: {e}", file=sys.stderr, flush=True)
            return 1
        finally:
            if conn is not None:
                conn.close()
    # RETRIES <= 0 means the loop body never ran and there is no last_error to
    # report; say that plainly instead of "giving up after 0 attempts: None".
    if RETRIES < 1:
        print(
            f"init_schema: INIT_SCHEMA_RETRIES={RETRIES} leaves nothing to try",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(
        f"init_schema: giving up after {RETRIES} attempts: {last_error}",
        file=sys.stderr,
        flush=True,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
