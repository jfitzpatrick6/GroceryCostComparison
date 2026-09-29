import datetime
import hmac
import math
import os
import queue
import re
import secrets
import threading
import time

import psycopg2
import psycopg2.extras
import psycopg2.pool
import requests
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.exceptions import BadRequest, HTTPException

import matching

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
# Signs the session cookie, and so the CSRF token inside it (#69). It used to
# default to a constant published in this file, and a token signed with a known
# key is not a token. There is deliberately no constant fallback now.
#
# Not *required*, though, despite #69's done bar, because requiring it would make
# the next deploy of an .env without it a webapp that refuses to start - no app
# at all for the household, a worse outcome than the one being fixed. Instead
# docker-entrypoint.sh generates a random key when none is set and exports it
# before gunicorn forks, so both workers share it. The cost is that sessions
# (chosen profile, pending flash messages, CSRF tokens in open tabs) reset on a
# container restart. Set SECRET_KEY in .env to keep them across restarts.
#
# The fallback below only covers running app.py directly or importing it in
# tests: a single process, so a per-process random key is consistent.
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_urlsafe(48)
# Defence in depth for #69: browsers won't attach a Lax cookie to a cross-site
# POST, so a forged form arrives with no session and therefore no token.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


# --- CSRF (#69) ---------------------------------------------------------------
#
# No auth, by design (#35): the network is the boundary. But any page a family
# member opens on the LAN can submit a form to this app, and a browser attaches
# no proof of where it came from. Without this, a hostile page could POST to
# /recipes/<id>/delete - unrecoverable. Hand-rolled rather than Flask-WTF: this
# is the whole mechanism, and it avoids a dependency for one check.
#
# Applied to EVERY POST in before_request, not per route, because a partial
# rollout is worse than none: it looks protected. test_csrf.py asserts every
# <form method="post"> in every template carries the field.


class CSRFFailed(BadRequest):
    """A POST without a valid token. Its own type so the error page can say
    something useful ("reload and try again") instead of the generic 400 copy."""


def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = session["_csrf"] = secrets.token_urlsafe(32)
    return token


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def check_csrf():
    # Unknown URLs skip the check so they 404/405 as usual; "that form had
    # expired" for a mistyped URL would mislead whoever is debugging it.
    if request.method != "POST" or request.url_rule is None:
        return
    expected = session.get("_csrf")
    sent = request.form.get("csrf_token", "")
    if not expected or not hmac.compare_digest(expected, sent):
        raise CSRFFailed()


# --- Connection pool (#67) ------------------------------------------------------
#
# Every route used to open a fresh Postgres connection (TCP + auth) and close it,
# and the nav's context processor opened a second one on every render. Now
# get_connection() hands out pooled connections, one pool per gunicorn worker
# process, created lazily on first use - after gunicorn has forked, because a
# psycopg2 connection must never be shared across a fork.
#
# Sizing: 2 workers x 4 threads (docker-entrypoint.sh). A request can hold two
# connections at once (a route renders while its own connection is still open,
# and the nav's context processor borrows another), so 8 per worker covers every
# thread at its worst; 2 x 8 = 16, far under Postgres's default max_connections of
# 100. If the pool is ever exhausted anyway, the caller gets a plain direct
# connection rather than an error - slower, never broken.
#
# POOL_MIN is what actually gets REUSED, not a floor: psycopg2's pool keeps a
# returned connection only while fewer than `minconn` are idle, and closes the
# rest. At minconn=1 every page render still opened a fresh connection (the
# route held the one idle connection, so the nav's borrow connected anew) - the
# review of #67 caught this; measured below in the commit. 4 idle per worker
# covers two concurrent page renders without reconnecting; they're opened
# eagerly when the pool is created.
#
# The subtle part (#67): a pooled connection is not closed between uses, so an
# uncommitted transaction would otherwise leak into the next borrower. close()
# therefore ROLLS BACK before returning the connection - a route that forgot to
# commit loses its writes exactly as it did when close() really closed.
POOL_MIN, POOL_MAX = 4, 8
_pool = None
_pool_lock = threading.Lock()


class _PooledConnection:
    """A pooled psycopg2 connection whose close() returns it to the pool.

    Delegates everything else, so every existing `conn = get_connection() ...
    finally: conn.close()` call site works unchanged.
    """

    def __init__(self, pool, conn):
        self._pool, self._conn = pool, conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    # Dunder methods bypass __getattr__, so `with conn:` (psycopg2's commit-or-
    # rollback block, which does NOT close) needs explicit delegation.
    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def close(self):
        conn, self._conn = self._conn, None
        if conn is None:
            return
        broken = bool(conn.closed)
        if not broken:
            try:
                conn.rollback()
            except psycopg2.Error:
                broken = True
        self._pool.putconn(conn, close=broken)


def _get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = psycopg2.pool.ThreadedConnectionPool(
                POOL_MIN, POOL_MAX, host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS,
            )
        return _pool


def get_connection(connect_timeout=None):
    # connect_timeout set = a caller that must not hang on a black-holed host
    # (init_schema.py at startup, #82; the background USDA worker, #64). Those get
    # a direct connection: they run rarely, and a pool built with a timeout would
    # impose it on every request.
    if connect_timeout is not None:
        return psycopg2.connect(
            host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS,
            connect_timeout=connect_timeout,
        )
    pool = _get_pool()
    # A connection the server dropped (db container restarted) still looks open
    # client-side until it is used. Probe each checkout, discarding dead ones, so a
    # restart costs a reconnect here instead of a 503 per idle pooled connection.
    # Bounded: after POOL_MAX + 1 tries, or if the pool runs dry, fall back to a
    # direct connection rather than failing the request.
    for _ in range(POOL_MAX + 1):
        try:
            conn = pool.getconn()
        except psycopg2.pool.PoolError:
            break
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.rollback()
            return _PooledConnection(pool, conn)
        except psycopg2.Error:
            pool.putconn(conn, close=True)
    return psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)


def price_data_available(cur):
    """grocery_prices_latest only exists once collector.py has run at least
    one scrape - the webapp doesn't own that schema. Pages that join
    against it need to degrade gracefully (no price data yet) rather than
    500 on a fresh deployment with no scrape history."""
    cur.execute("SELECT to_regclass('grocery_prices_latest') IS NOT NULL AS table_exists")
    return cur.fetchone()["table_exists"]


# Past this many days since a store's last scrape the banner stops being a quiet
# note and becomes a visible warning. A week matches how grocery pricing actually
# works: stores set sale prices on a weekly cycle, so a comparison built on data
# older than the last cycle can confidently name a "cheapest store" that is
# simply last week's cheapest store. 7 is a judgment call, not a measured
# constant - it is short enough that a dead scheduler surfaces within a week and
# long enough that a skipped night doesn't shout.
PRICE_STALE_AFTER_DAYS = 7

# Stores the scraper attempts, mirrored from collector.py's list of
# (name, scrape_fn, store_id) tuples.
#
# Duplicated rather than imported, and that is a real cost worth stating: the two
# lists can drift. Importing collector.py is not an option because it imports
# pandas and playwright at module load and neither is installed in the webapp
# image, so `import collector` would take the whole app down at startup. Reading
# the names out of the database cannot work either, because a store that produced
# zero rows leaves no trace there - which is precisely the case this list exists
# to surface.
#
# The drift fails safe rather than loud: add a store to the scraper and forget it
# here, and the only symptom is that the banner stops flagging that store as
# missing. It cannot invent a store that isn't there. Worth a test pinning the
# list so the duplication is at least visible when it changes.
EXPECTED_STORES = ("Aldis", "BJs", "Tops", "Walmart")


def price_freshness_from_catalog(catalog):
    """How old the price data behind a comparison is, per store (#62).

    Takes the catalog matching.load_catalog() already fetched rather than running
    its own query. The view holds one row per (product, store) - the most recent -
    so the per-store max of its datetime column is exactly that store's last
    scrape time, and it is already in memory. Computing this separately would
    mean a `GROUP BY store` aggregate over the whole history table on every page
    load, to derive a number the page has already paid to fetch.

    Returns None when the catalog is empty, so a caller can tell "no price data"
    from "price data, but old" - those need different messages, and the former is
    already gated by price_data_available().

    Per store rather than one global timestamp because collector.py scrapes each
    store independently and one can fail while the others succeed (#24). A single
    "prices as of today" would then hide that one chain's numbers are six weeks
    old, which is worse than saying nothing at all: it lends the stale store the
    credibility of the fresh ones.
    """
    latest = {}
    for row in catalog:
        when = row.get("datetime")
        if when is None:
            continue
        store = row.get("store")
        if store is None:
            continue
        # Normalize to a date before comparing, not after. Comparing raw values
        # would raise TypeError if one row carried a datetime and another a bare
        # date for the same store - unreachable from Postgres today, where the
        # column is TIMESTAMP and psycopg2 returns datetime uniformly, but this
        # function's docstring and its tests both advertise date tolerance, so it
        # has to actually tolerate them rather than only claiming to.
        day = when.date() if hasattr(when, "date") else when
        if store not in latest or day > latest[store]:
            latest[store] = day
    if not latest:
        return None

    today = datetime.date.today()
    stores = []
    for store in sorted(latest):
        last_day = latest[store]
        age = (today - last_day).days
        stores.append({
            "store": store,
            "last_scrape": last_day,
            "age_days": age,
            "stale": age > PRICE_STALE_AFTER_DAYS,
        })
    return {
        "stores": stores,
        # The oldest store is what the warning should be about: a comparison is
        # only as current as its least current input.
        "oldest_age_days": max(s["age_days"] for s in stores),
        "newest_age_days": min(s["age_days"] for s in stores),
        "any_stale": any(s["stale"] for s in stores),
        "stale_after_days": PRICE_STALE_AFTER_DAYS,
        # Stores the scraper tries but that produced no rows at all. This is the
        # part of #62 that a per-store age list cannot express: a store with no
        # data has no age to show, so it simply vanishes from the banner, and the
        # page then presents a "cheapest store" answer that silently excludes a
        # chain the household shops at. Walmart is not hypothetical - it has been
        # returning zero items since #12 (bot-verification wall, deliberately not
        # circumvented), so every comparison this app has ever rendered excluded
        # it without saying so.
        "missing_stores": [s for s in EXPECTED_STORES if s not in latest],
    }


def resolve_item_match(catalog, item):
    """The price rows a list item should be priced from (#98).

    Returns the same shape both callers expect - a dict of store -> catalog row -
    so where_to_buy can compare across stores and /list can pick the cheapest.

    An item may carry pinned_product/pinned_store, set when the user chose a
    specific catalogue product via /list/pick instead of typing free text. In that
    case the match is an exact lookup, not a fuzzy one: the whole point of picking
    is that the user has already decided what "bacon" means, and re-running the
    fuzzy matcher on their own choice would reintroduce exactly the ambiguity they
    just removed ("bacon" -> "TOPS Bacon Chips", #97/#99).

    A pinned item prices from its one store only. Comparing a deliberately chosen
    product across stores would mean substituting a different product for the one
    the user picked, which is the mis-match this exists to prevent.

    Falls back to fuzzy matching when the item is not pinned, and also when a
    pinned product has vanished from the catalogue - delisted, renamed, or the
    store dropped it. A stale pin must degrade to "try to match the text" rather
    than to "no price", because the item is still on the list and still needs
    buying; silently pricing nothing would be the confident-wrong-answer failure
    in reverse.
    """
    pinned_product = item.get("pinned_product")
    pinned_store = item.get("pinned_store")
    if pinned_product and pinned_store:
        for row in catalog:
            if row["product"] == pinned_product and row["store"] == pinned_store:
                # A COPY, not the catalog row itself, and this is load-bearing
                # rather than defensive. where_to_buy calls _annotate_package_fit
                # on every row it prices, which mutates packages_needed and
                # total_cost in place. Returning the shared dict meant two list
                # items pinned to the same product aliased one object, so the last
                # annotation won for both: two pinned Wellsley Farms Bacon at 1 lb
                # and 5 lb both rendered $35.96 and the split total came to
                # $71.92 instead of $44.95 - a confident wrong number, reachable by
                # clicking Add twice. matching.match_item already copies for
                # exactly this reason; the pin path must too.
                #
                # _tokens is dropped for the same reason match_item drops it: it is
                # the matcher's working state, not something a template should see.
                # Strip EVERY underscore-prefixed field, not just _tokens: since
                # #99 catalog rows also carry _token_seq and _raw_seq, and a
                # hardcoded name here is how one gets forgotten and leaks into a
                # template context. match_item already strips this way.
                chosen = {k: v for k, v in row.items() if not k.startswith("_")}
                return {chosen["store"]: chosen}
    return matching.best_per_store(matching.match_item(catalog, item["name"]))


@app.route("/healthz")
def healthz():
    """Liveness/readiness probe (#61). Returns JSON, 200 when the database
    answers and 503 when it doesn't. docker-compose.yml points the webapp
    container's HEALTHCHECK at it (#71).

    Checks the database rather than merely answering, because "the process is
    up but Postgres is unreachable" is the failure state that actually matters
    here. A probe that only proved the process was alive would report healthy
    while every page 500s.

    Two deliberate properties:

    - It runs **no DDL**. Unlike nearly every other route this does not call
      an ensure_*_table(), so it stays cheap and side-effect free under a
      healthcheck that fires every few seconds (see #66 for why the
      per-request DDL elsewhere is a problem this route must not join).
    - It returns JSON, not a template. Rendering a template would trigger
      inject_profile_switcher - a context processor that opens its own
      connection and runs CREATE TABLE on every render - which would make the
      healthcheck itself the most expensive thing on the box.

    Returns 503 rather than 200 when the database is unreachable, which is the
    correct HTTP semantic and what a reverse proxy or a human with curl needs to
    tell "app is up, database isn't" from "app is fine". Note this is NOT what
    makes Docker restart the container: a HEALTHCHECK keys off its test
    command's *exit code*, so the usual `curl -f` treats 503 and an unhandled
    500 identically. The status distinction is for people and proxies; Docker
    only learns "not 2xx". (#71 wires this up in docker-compose.yml, using the
    image's own Python rather than curl, which python:3.12-slim does not ship.)
    """
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        finally:
            conn.close()
    except psycopg2.Error:
        return {"status": "unhealthy", "database": "unreachable"}, 503
    return {"status": "ok", "database": "reachable"}, 200


def ensure_profiles_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );
    """)


def active_profile():
    """Whoever's picked via the profile switcher, or None if nobody has.
    No password (see #35) - Tailscale is the actual access control."""
    return session.get("profile_name")


@app.context_processor
def inject_profile_switcher():
    """Makes the active profile + full profile list available in every
    template's nav, without every single route having to fetch and pass
    it through explicitly.

    Degrades to an empty profile list if the database is unreachable, and that
    is load-bearing rather than defensive: this runs on EVERY template render,
    including the error pages added for #68. If it raised, a database outage
    would make the 500 handler fail too, and the household would get a bare
    Werkzeug traceback instead of an explanation - the exact failure #68 exists
    to remove. The nav simply shows "nobody" until the database is back.

    Read-only since #66: it used to run ensure_profiles_table plus a COMMIT on
    every render, i.e. DDL under an ACCESS EXCLUSIVE lock on every page view.
    The schema now exists before gunicorn starts (migrations.py).
    """
    names = []
    try:
        conn = get_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT name FROM profiles ORDER BY name")
                names = [row["name"] for row in cur.fetchall()]
        finally:
            conn.close()
    except psycopg2.Error:
        # Logged at warning, not error: the route that triggered the render will
        # fail loudly on its own query and produce the real 500. Logging this at
        # error level would double-report every outage.
        app.logger.warning("profile list unavailable; rendering nav without it", exc_info=True)
    return {"active_profile_name": active_profile(), "all_profile_names": names}


# --- Error pages (#68) -----------------------------------------------------
#
# Before this, any unhandled exception produced Werkzeug's bare 500 page with no
# explanation and no way back, and nothing was logged beyond the access line - so
# a household member saying "the page broke" was undiagnosable. The most likely
# trigger is not exotic: the db container restarting, which is a normal event, and
# which compose's `depends_on` does not fully protect against (#71 helps but the
# webapp can still be up while Postgres is not accepting connections).
#
# One handler for Exception that dispatches on HTTPException, rather than five
# separate @errorhandler decorators. A single funnel is what guarantees nothing
# escapes unrendered; a decorator per code leaves every code nobody thought to
# list falling through to Werkzeug's default.

# Status codes worth a specific explanation rather than a generic one. Anything
# not listed still gets a page, just with the server's own reason phrase.
_ERROR_COPY = {
    400: ("That request didn't make sense",
          "A form field was missing or malformed. Go back and try again - if a "
          "button on the page produced this, it's a bug worth reporting."),
    403: ("Not allowed",
          "This app has no authentication of its own; the tailnet is the "
          "boundary (#35). If you reached this from a link, something is "
          "misconfigured rather than forbidden."),
    404: ("No such page",
          "That recipe, item or page doesn't exist - it may have been deleted, "
          "or the link may be old."),
    500: ("Something went wrong on our side",
          "The details are in the server log. If the database was restarting, "
          "waiting a few seconds and reloading usually fixes it."),
    503: ("The database isn't reachable",
          "The app is up but Postgres is not answering. Check "
          "`docker compose ps` and `docker compose logs db`."),
}


@app.errorhandler(Exception)
def handle_error(err):
    """Render every failure as a real page, and log every unexpected one.

    Two deliberate details:

    - HTTPExceptions are passed through with their own status, so a legitimate
      404 stays a 404 rather than becoming a 500. Only genuinely unexpected
      exceptions are logged with a traceback; logging every 404 would bury the
      ones that matter.
    - Rendering is wrapped, because the error page is itself a template and the
      context processor above talks to the database. If rendering fails - the
      template is missing, or something else is broken - fall back to a plain
      string so there is ALWAYS an explanation rather than a bare traceback.
    """
    if isinstance(err, CSRFFailed):
        # Almost always benign: a tab left open across a container restart (the
        # session key may have rotated) or a form from before this check existed.
        code = 400
        name, detail = ("That form had expired",
                        "Nothing was changed. If you typed something long (a recipe), press "
                        "Back and copy it first - then reload the page and submit again. This "
                        "usually means the app restarted while the page was open. If it keeps "
                        "happening on a page you just loaded, it's a bug worth reporting.")
    elif isinstance(err, HTTPException):
        code = err.code or 500
        name, detail = _ERROR_COPY.get(code, (err.name, err.description or ""))
    else:
        # OperationalError is psycopg2's "could not connect / connection lost".
        # Without this the 503 copy above was unreachable: a db restart - the
        # most likely failure, per the note above - got the generic 500 text
        # instead of the one that says what to check.
        code = 503 if isinstance(err, psycopg2.OperationalError) else 500
        name, detail = _ERROR_COPY[code]
        app.logger.exception(
            "Unhandled %s on %s %s", type(err).__name__, request.method, request.path
        )

    try:
        return render_template("error.html", code=code, title=name, detail=detail), code
    except Exception:
        app.logger.exception("rendering the error page itself failed")
        body = (
            f"<!doctype html><meta charset=utf-8><title>{code} {name}</title>"
            f"<body style=font-family:sans-serif;padding:2rem>"
            f"<h1>{code} &mdash; {name}</h1><p>{detail}</p>"
            f"<p><a href=/>Back to the app</a></p>"
        )
        return body, code


@app.route("/profiles")
def profiles():
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conn.commit()
            cur.execute("SELECT id, name FROM profiles ORDER BY name")
            rows = cur.fetchall()
    finally:
        conn.close()
    return render_template("profiles.html", rows=rows, active=active_profile())


@app.route("/profiles/add", methods=["POST"])
def add_profile():
    name = request.form.get("name", "").strip()
    if name:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO profiles (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,))
            conn.commit()
        finally:
            conn.close()
        flash(f"{name} added.", "success")
    else:
        # Was a silent no-op. The form's `required` stops this in a browser, but
        # a blank POST otherwise reloaded the page and said nothing - which reads
        # as "the button is broken".
        flash("Enter a name to add a profile.", "error")
    return redirect(url_for("profiles"))


@app.route("/profiles/remove", methods=["POST"])
def remove_profile():
    profile_id = request.form["id"]
    removed = None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Look the name up before deleting so the confirmation says WHO was
            # removed. Destructive and unrecoverable, so "removed" alone is not
            # enough to notice a mis-click in time.
            cur.execute("SELECT name FROM profiles WHERE id = %s", (profile_id,))
            row = cur.fetchone()
            removed = row[0] if row else None
            cur.execute("DELETE FROM profiles WHERE id = %s", (profile_id,))
        conn.commit()
    finally:
        conn.close()
    flash(f"Removed {removed}. Items they added keep their name." if removed
          else "That profile was already gone.", "success")
    return redirect(url_for("profiles"))


@app.route("/profiles/switch", methods=["POST"])
def switch_profile():
    session["profile_name"] = request.form.get("name") or None
    who = session["profile_name"]
    flash(f"Now shopping as {who}." if who
          else "Profile cleared - edits won't be attributed to anyone.", "success")
    return redirect(request.referrer or url_for("profiles"))


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


def group_search_results(matches, stores):
    """match_item() results -> one column per store, best candidate first (#107).

    Ordered by match score, then unit price: identity first, price only breaks
    ties, the same principle best_per_store() follows - price must not promote a
    different product over the one asked for. Not identical to it, though:
    best_per_store breaks ties on package price, this on unit price, because a
    shopper comparing options wants the better rate first. So this column's top
    row is not guaranteed to be the one /list picks. Every store in `stores` gets a
    column even when it has no match, so "not sold here" is said rather than
    shown as a missing column (the "No price match found" principle from #25).

    Deliberately NOT truncated. Measured on the live catalogue: for "chicken
    breast" at Tops the name matcher ranks deli and nugget products first, and
    the raw breasts ("TOPS Boneless Chicken Breast Skinless with Rib Meat",
    Springer Mountain) come 12th and 14th of 27. A top-8 cut would have hidden
    exactly what the shopper was looking for; a scrolling column hides nothing.
    """
    by_store = {store: [] for store in stores}
    for m in matches:
        by_store.setdefault(m["store"], []).append(m)
    columns = []
    for store in sorted(by_store):
        found = by_store[store]
        found.sort(key=lambda m: (
            -m["match_score"],
            m["unit_price"] if m.get("unit_price") is not None else float("inf"),
        ))
        columns.append({"store": store, "total": len(found), "rows": found})
    return columns


@app.route("/prices")
def prices():
    query = request.args.get("q", "").strip()
    sort = request.args.get("sort", "unit_price")
    order_by = SORT_COLUMNS.get(sort, SORT_COLUMNS["unit_price"])
    # "all" keeps the old flat, sortable substring table reachable - it is still
    # the right tool for browsing ("everything with 'chicken' in it, cheapest
    # per lb first") and for searching by store name.
    view = request.args.get("view", "")

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if not price_data_available(cur):
                return render_template("prices.html", rows=[], columns=None, query=query, sort=sort)
            # A product search goes through the same matcher as the grocery list
            # (#107). The substring search this replaces could not put the right
            # products side by side: "chicken breast" returned 88 rows sorted by
            # unit price, with nuggets, canned chunk, deli meat and a dog treat
            # interleaved with raw breasts, and Aldi's "per lb" listings (no unit
            # price) sorted to the very end.
            #
            # A store-name query ("bjs", "aldi") falls through to the substring
            # table, which is what that search has always meant. Decided BEFORE
            # loading the catalog, against the fixed scraper labels, so it costs
            # no scan - deciding after meant two full reads of the view (#65).
            # Prefix match, because the label is "Aldis" and people type "aldi".
            # The store list itself comes from the loaded catalog, not a second
            # SELECT DISTINCT over the view.
            q = query.lower().replace("'", "")
            store_search = bool(q) and any(name.lower().startswith(q) for name in EXPECTED_STORES)
            if query and view != "all" and not store_search:
                catalog = matching.cached_catalog(cur)
                stores = sorted({row["store"] for row in catalog})
                columns = group_search_results(matching.match_item(catalog, query), stores)
                return render_template(
                    "prices.html", rows=[], columns=columns, query=query, sort=sort,
                    low_confidence=matching.LOW_CONFIDENCE_SCORE,
                    freshness=price_freshness_from_catalog(catalog),
                )

            sql = f"""
                SELECT product, store, price, size, unit_price, unit, datetime
                FROM grocery_prices_latest
                WHERE product ILIKE %s OR store ILIKE %s
                ORDER BY {order_by}
                LIMIT 500
            """
            like_query = f"%{query}%"
            cur.execute(sql, (like_query, like_query))
            rows = cur.fetchall()
    finally:
        conn.close()

    return render_template("prices.html", rows=rows, columns=None, query=query, sort=sort, view=view)


@app.route("/staples")
def staples():
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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
                    SELECT s.product, s.store, NULL AS price, NULL AS size,
                           NULL AS unit_price, NULL AS unit, NULL AS datetime
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
            cur.execute(
                "INSERT INTO staples (product, store) VALUES (%s, %s) ON CONFLICT (product, store) DO NOTHING",
                (product, store),
            )
        conn.commit()
    finally:
        conn.close()
    flash(f"Pinned {product} at {store} to staples.", "success")
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
    flash(f"Unpinned {product} at {store}.", "success")
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
    cur.execute("ALTER TABLE grocery_list_items ADD COLUMN IF NOT EXISTS added_by TEXT;")
    cur.execute("ALTER TABLE grocery_list_items ADD COLUMN IF NOT EXISTS checked_by TEXT;")
    # A list item can be bound to a specific catalogue product+store instead of
    # being fuzzy-matched from its text (#98). Both columns are nullable: an
    # unpinned item - anything typed freehand, or added by the planner - has NULLs
    # and matches exactly as before. See resolve_item_match().
    cur.execute("ALTER TABLE grocery_list_items ADD COLUMN IF NOT EXISTS pinned_product TEXT;")
    cur.execute("ALTER TABLE grocery_list_items ADD COLUMN IF NOT EXISTS pinned_store TEXT;")


@app.route("/list")
def grocery_list():
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conn.commit()
            cur.execute("""
                SELECT id, name, qty, checked, added_by, checked_by,
                       pinned_product, pinned_store
                FROM grocery_list_items
                ORDER BY checked, added_at
            """)
            items = cur.fetchall()

            # Real cross-store matching (#25) - normalizes both the list
            # item name and every catalog product into significant tokens
            # and matches on token-subset containment (see matching.py),
            # not exact-string or bare substring. Reduced to the single
            # best identity match per store before picking cheapest, so a
            # loosely-related but technically-matching product (e.g. a
            # candy bar for a "milk" query) can't win on price alone.
            # Unmatched items still show, just without a match - never
            # dropped from the list. grocery_prices_latest only exists once
            # a scrape has actually run - skip matching entirely rather
            # than 500ing on a fresh deployment with no scrape history yet.
            if price_data_available(cur):
                catalog = matching.cached_catalog(cur)
                for item in items:
                    # resolve_item_match prefers an exact catalogue pin (#98) and
                    # only falls back to fuzzy matching for unpinned items or a
                    # pin whose product has left the catalogue.
                    per_store = resolve_item_match(catalog, item)
                    item["match"] = min(
                        per_store.values(), key=lambda m: (m["unit_price"] is None, m["unit_price"] or 0)
                    ) if per_store else None
            else:
                for item in items:
                    item["match"] = None

            # "Running low" suggestions (#46) - a pantry item with a
            # threshold set that's at/below it, unless it's already sitting
            # unchecked on the list (no duplicate suggestions). Suggestion
            # only, never auto-added.
            cur.execute("""
                SELECT name, amount, unit, threshold FROM pantry_items
                WHERE threshold IS NOT NULL AND amount IS NOT NULL AND amount <= threshold
                ORDER BY name
            """)
            running_low = []
            unchecked_names = {i["name"].strip().lower() for i in items if not i["checked"]}
            for row in cur.fetchall():
                row["already_on_list"] = row["name"].strip().lower() in unchecked_names
                running_low.append(row)
    finally:
        conn.close()
    return render_template(
        "list.html", items=items, running_low=running_low,
        # Presentation threshold, not a filter: below it the match is still shown,
        # just flagged as uncertain (#97). Kept in matching.py beside MIN_SCORE
        # because both are claims about what a score means.
        low_confidence=matching.LOW_CONFIDENCE_SCORE,
    )


@app.route("/list/add", methods=["POST"])
def add_list_item():
    name = request.form.get("name", "").strip()
    qty = request.form.get("qty", "").strip()
    if name:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO grocery_list_items (name, qty, added_by) VALUES (%s, %s, %s)",
                    (name, qty or None, active_profile()),
                )
            conn.commit()
        finally:
            conn.close()
        flash(f"Added {name}{f' ({qty})' if qty else ''} to your list.", "success")
    else:
        flash("Enter an item name to add it.", "error")
    return redirect(url_for("grocery_list"))


@app.route("/list/pick")
def list_pick():
    """Search the real catalogue and pick a specific product (#98).

    The free-text add box asks the user to guess retailer vocabulary, and the
    fuzzy matcher then guesses what they meant - two guesses stacked, with the
    second one invisible before #97. This is the escape hatch: look the product
    up by what it is actually called and bind the list item to it, so neither
    guess happens.

    Server-rendered rather than a JS autocomplete because the app has no
    JavaScript layer and adding one for this would mean a build step or a CDN,
    both of which the design system deliberately rules out (#101). A submitted
    search is one round trip and works with JS disabled, on any phone browser.
    """
    query = request.args.get("q", "").strip()
    rows = []
    has_prices = False
    if query:
        # Escape LIKE's own metacharacters. Without this `q=%` matches every row
        # and `q=2%` silently means "contains 2" - a wrong answer to a reasonable
        # query. ESCAPE '\' is named explicitly rather than relying on the
        # backslash default, which standard_conforming_strings can affect.
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        conn = get_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                has_prices = price_data_available(cur)
                if has_prices:
                    # Same ILIKE as /prices, so a term that finds something there
                    # finds the same things here. Ordered by unit rate then name
                    # because the point is comparing like-for-like prices, and
                    # capped so a one-letter query can't page 20k rows into a form.
                    #
                    # price IS NOT NULL is not cosmetic: a pinned row with a NULL
                    # price would make float(None) raise in where_to_buy and
                    # "%.2f"|format(None) fail in list.html, so both pages 500 -
                    # and with /list down there is no Remove button left to undo
                    # the pin. Don't offer what can't be priced.
                    #
                    # ESCAPE is written inline (the '\\' in this Python literal is
                    # one backslash in the SQL) rather than relying on LIKE's
                    # default escape character, which standard_conforming_strings
                    # can affect.
                    cur.execute("""
                        SELECT product, store, price, size, unit_price, unit
                        FROM grocery_prices_latest
                        WHERE (product ILIKE %s ESCAPE '\\' OR store ILIKE %s ESCAPE '\\')
                          AND price IS NOT NULL
                        ORDER BY (unit_price IS NULL), unit_price, product
                        LIMIT 100
                    """, (like, like))
                    rows = cur.fetchall()
        finally:
            conn.close()
    # has_prices distinguishes "your search found nothing" from "there is nothing
    # to search yet". Without it a fresh install tells the user their search term
    # was wrong, which sends them rephrasing instead of running a scrape.
    return render_template("pick.html", query=query, rows=rows, has_prices=has_prices)


@app.route("/list/add_pinned", methods=["POST"])
def add_pinned_list_item():
    """Add a list item bound to a specific catalogue product+store (#98).

    The pin is what makes the choice stick: resolve_item_match() looks the pair
    up exactly instead of re-running the fuzzy matcher on the item's text, so
    "bacon" picked as Wellsley Farms Bacon at BJs stays that product forever
    rather than drifting to whatever scores highest next scrape.
    """
    name = request.form.get("name", "").strip()
    product = request.form.get("product", "").strip()
    store = request.form.get("store", "").strip()
    qty = request.form.get("qty", "").strip()
    if name and product and store:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                # No schema work here: migrations.py creates every app table at
                # startup (#66), before any request runs.
                cur.execute(
                    "INSERT INTO grocery_list_items "
                    "(name, qty, added_by, pinned_product, pinned_store) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (name, qty or None, active_profile(), product, store),
                )
            conn.commit()
            flash(f"Added {name} to your list, pinned to {product} at {store}.", "success")
        finally:
            conn.close()
    else:
        # A missing field here means a broken form or a hand-crafted POST, not a
        # user mistake - the picker always sends all three. Say so rather than
        # silently adding nothing.
        flash("Could not add that item: the product and store it should be pinned "
              "to were missing.", "error")
    return redirect(url_for("grocery_list"))


@app.route("/list/check", methods=["POST"])
def check_list_item():
    item_id = request.form["id"]
    checked = request.form["checked"] == "1"
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE grocery_list_items SET checked = %s, checked_at = CASE WHEN %s THEN now() ELSE NULL END, "
                "checked_by = CASE WHEN %s THEN %s ELSE checked_by END WHERE id = %s",
                (checked, checked, checked, active_profile(), item_id),
            )
            # Restock pantry when checking off (buying), not when un-checking
            # (see #30) - undo doesn't reverse the restock, matching that
            # manual pantry corrections are always available rather than
            # trying to make this perfectly symmetric.
            # Hoisted above the UPDATE (it used to run only when checking off) so
            # both directions can name the item. Reading name/qty before or after
            # the UPDATE is equivalent - the UPDATE touches checked/checked_at/
            # checked_by only.
            cur.execute("SELECT name, qty FROM grocery_list_items WHERE id = %s", (item_id,))
            item = cur.fetchone()
            if checked and item:
                restock_pantry(cur, item[0], item[1], updated_by=active_profile())
        conn.commit()
    finally:
        conn.close()
    label = item[0] if item else "that item"
    if checked:
        # Say the pantry moved too, because it does and it is not visible from the
        # list page - a silent side effect is how the pantry ends up distrusted.
        flash(f"Checked off {label} - pantry restocked if it matched.", "success")
    else:
        flash(f"Put {label} back on the list. The pantry restock was not undone.", "warning")
    return redirect(url_for("grocery_list"))


@app.route("/list/remove", methods=["POST"])
def remove_list_item():
    item_id = request.form["id"]
    removed = None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Name it before deleting: this is one tap on a phone in a store, and
            # "removed" without saying what is no confirmation at all.
            cur.execute("SELECT name FROM grocery_list_items WHERE id = %s", (item_id,))
            row = cur.fetchone()
            removed = row[0] if row else None
            cur.execute("DELETE FROM grocery_list_items WHERE id = %s", (item_id,))
        conn.commit()
    finally:
        conn.close()
    flash(f"Removed {removed} from the list." if removed else "That item was already gone.",
          "success")
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
    cur.execute("ALTER TABLE pantry_items ADD COLUMN IF NOT EXISTS updated_by TEXT;")
    # NULL = feature off for this item (#46) - opt-in per row, never asked
    # at add/restock time, since most pantry rows (leftovers, one-off buys)
    # never want a threshold and guessing one would just create noise.
    cur.execute("ALTER TABLE pantry_items ADD COLUMN IF NOT EXISTS threshold NUMERIC;")


_QTY_LINE = re.compile(r"^\s*([\d.]+)\s*(\S*)\s*$")


def restock_pantry(cur, name, qty_text, updated_by=None):
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
            "UPDATE pantry_items SET amount = %s, updated_at = now(), updated_by = %s WHERE id = %s",
            (float(existing[1] or 0) + amount, updated_by, existing[0]),
        )
    elif not existing:
        cur.execute(
            "INSERT INTO pantry_items (name, amount, unit, updated_by) VALUES (%s, %s, %s, %s)",
            (name, amount, unit, updated_by),
        )
    # else: existing pantry row has a different unit - don't guess how to
    # combine them, leave it for a manual correction.


@app.route("/pantry")
def pantry():
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conn.commit()
            cur.execute("SELECT id, name, amount, unit, updated_by, threshold FROM pantry_items ORDER BY name")
            items = cur.fetchall()
    finally:
        conn.close()
    return render_template("pantry.html", items=items)


@app.route("/pantry/set_threshold", methods=["POST"])
def set_pantry_threshold():
    """Sets (or clears, if left blank) the "running low" point for one
    pantry item (#46) - opt-in per item, doesn't touch amount/unit."""
    item_id = request.form["id"]
    threshold = request.form.get("threshold") or None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE pantry_items SET threshold = %s WHERE id = %s", (threshold, item_id))
        conn.commit()
    finally:
        conn.close()
    # This fires from an inline onchange, so the message has to be short enough
    # not to feel like noise on every edit - but it must exist, because otherwise
    # an auto-submitting field gives no evidence it saved.
    flash(f"Low-stock level set to {threshold}." if threshold
          else "Low-stock alert turned off.", "success")
    return redirect(url_for("pantry"))


def parse_pantry_amount(text):
    """Pantry amount text -> a number, or None when blank. Raises ValueError for
    anything else.

    pantry_items.amount is NUMERIC, and the form used to pass raw text straight
    through, so typing "1/2" returned a 500 (#75, found while adding bulk entry).
    Fractions are converted; anything unreadable is refused rather than guessed,
    because a pantry amount the planner subtracts from the shopping list is
    exactly the kind of number that must not be made up.
    """
    text = (text or "").strip()
    if not text:
        return None
    if "/" in text:
        num, _, den = text.partition("/")
        value = float(num) / float(den)
    else:
        value = float(text)
    if value < 0 or math.isinf(value) or math.isnan(value):
        raise ValueError(text)
    return round(value, 4)


def _set_pantry_item(cur, name, amount, unit):
    """Set (not add to) one pantry item, matched case-insensitively by name.
    Returns True if it created a new row."""
    cur.execute("SELECT id FROM pantry_items WHERE lower(name) = lower(%s)", (name,))
    existing = cur.fetchone()
    if existing:
        cur.execute(
            "UPDATE pantry_items SET amount = %s, unit = %s, "
            "updated_at = now(), updated_by = %s WHERE id = %s",
            (amount, unit, active_profile(), existing[0]),
        )
        return False
    cur.execute(
        "INSERT INTO pantry_items (name, amount, unit, updated_by) VALUES (%s, %s, %s, %s)",
        (name, amount, unit, active_profile()),
    )
    return True


@app.route("/pantry/set", methods=["POST"])
def set_pantry_item():
    name = request.form.get("name", "").strip()
    unit = request.form.get("unit", "").strip() or None
    try:
        amount = parse_pantry_amount(request.form.get("amount"))
    except (ValueError, ZeroDivisionError):
        flash(f"\"{request.form.get('amount')}\" isn't an amount. Use a number like 2, 0.5 or 1/2.", "error")
        return redirect(url_for("pantry"))
    if name:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                _set_pantry_item(cur, name, amount, unit)
            conn.commit()
        finally:
            conn.close()
        # "set", not "added" - this route replaces the amount rather than
        # incrementing it, and saying "added 2 cups" when the pantry now holds
        # exactly 2 cups would be a lie about the semantics.
        qty = " ".join(f"{x:g}" if isinstance(x, float) else str(x) for x in (amount, unit) if x) or "no amount"
        flash(f"Pantry: {name} set to {qty}.", "success")
    else:
        flash("Enter an item name to save it to the pantry.", "error")
    return redirect(url_for("pantry"))


@app.route("/pantry/bulk", methods=["POST"])
def bulk_set_pantry():
    """Many pantry items at once, one per line, in the recipe grammar:
    "2 lb chicken breast", "1/2 cup rice", "salt" (#75).

    Getting a kitchen into the app one form submit at a time was the adoption
    cliff #75 describes: at zero pantry rows the planner's pantry logic never
    runs. Lines that can't be read are listed back, not guessed at, and nothing
    else on the paste is lost because of them.
    """
    created = updated = 0
    problems = []
    rows = []
    for raw in request.form.get("lines", "").splitlines():
        line = raw.strip().lstrip("-*•").strip()
        if not line:
            continue
        amount_text, unit, name = parse_ingredient_line(line)
        try:
            amount = parse_pantry_amount(amount_text)
        except (ValueError, ZeroDivisionError):
            problems.append(line)
            continue
        # A name that still starts like a quantity means the amount wasn't
        # understood ("1-2 cups flour", "2 lb" with no item): refuse it rather
        # than store a junk item under a quantity-shaped name.
        if not name or re.match(r"[\d./½⅓⅔¼¾⅛-]", name):
            problems.append(line)
            continue
        rows.append((name, amount, unit))
    if rows:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                for name, amount, unit in rows:
                    if _set_pantry_item(cur, name, amount, unit):
                        created += 1
                    else:
                        updated += 1
            conn.commit()
        finally:
            conn.close()
    if created or updated:
        flash(f"Pantry: {created} added, {updated} updated.", "success")
    if problems:
        flash("Couldn't read " + ", ".join(f"\"{p}\"" for p in problems[:5])
              + (f" and {len(problems) - 5} more" if len(problems) > 5 else "")
              + " - nothing was saved for those lines.", "warning")
    if not rows and not problems:
        flash("Paste one item per line, like \"2 lb chicken breast\".", "error")
    return redirect(url_for("pantry"))


@app.route("/pantry/remove", methods=["POST"])
def remove_pantry_item():
    item_id = request.form["id"]
    removed = None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Name it before deleting. Pantry amounts are not re-derivable - there
            # is no scrape that puts "2.5 cups of cumin" back - so a mis-tap here
            # costs real data and the confirmation has to say what went.
            cur.execute("SELECT name FROM pantry_items WHERE id = %s", (item_id,))
            row = cur.fetchone()
            removed = row[0] if row else None
            cur.execute("DELETE FROM pantry_items WHERE id = %s", (item_id,))
        conn.commit()
    finally:
        conn.close()
    flash(f"Removed {removed} from the pantry." if removed
          else "That pantry item was already gone.", "success")
    return redirect(url_for("pantry"))


_LIST_QTY_LINE = re.compile(r"^\s*([\d.]+)\s*(\S*)\s*$")
# grocery_prices_latest.unit is always one of these canonical forms
# (units.py, scraper-side) - map common ways a person would actually type a
# unit on the grocery list to the same space, conservative: an unrecognized
# unit just means package-fitting can't be computed for that item, falling
# back to today's per-package-price behavior rather than guessing.
_LIST_UNIT_CANONICAL = {
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "gal": "gal", "gallon": "gal", "gallons": "gal",
    "each": "each", "ea": "each",
    "ft": "ft", "feet": "ft", "foot": "ft",
    "oz": "lb", "ounce": "lb", "ounces": "lb",
    "qt": "gal", "quart": "gal", "quarts": "gal",
    "pt": "gal", "pint": "gal", "pints": "gal",
}
# Exact conversions into the canonical unit, for units that aren't already in
# it. Prices are stored per lb/gal (units.py), so a list saying "8 oz" was
# skipped by package fitting entirely - a real list row, "shredded cheese | 8
# oz", found by #70's tests. Only fixed-ratio units: "fl oz" is two tokens and
# the qty regex takes one, so it stays unfitted rather than being misread as a
# weight.
_LIST_UNIT_FACTOR = {"oz": 1 / 16, "ounce": 1 / 16, "ounces": 1 / 16,
                     "qt": 1 / 4, "quart": 1 / 4, "quarts": 1 / 4,
                     "pt": 1 / 8, "pint": 1 / 8, "pints": 1 / 8}


def _parse_needed_qty(qty_text):
    """Best-effort (amount, canonical_unit) for a grocery-list item's free-
    text qty (#38) - e.g. "2 lb" -> (2.0, "lb"). Returns (None, None) for
    anything not a clean "<number> <unit>" (blank, a merged "X + Y" string
    from the planner merge, an unrecognized unit) - package-fitting is
    skipped rather than guessed at for those, same conservative pattern as
    restock_pantry/apply_pantry elsewhere in this file."""
    if not qty_text:
        return None, None
    match = _LIST_QTY_LINE.match(qty_text)
    if not match or not match.group(1):
        return None, None
    amount_str, unit_str = match.groups()
    unit = _LIST_UNIT_CANONICAL.get(unit_str.lower())
    if not unit:
        return None, None
    try:
        return float(amount_str) * _LIST_UNIT_FACTOR.get(unit_str.lower(), 1.0), unit
    except ValueError:
        return None, None


def _annotate_package_fit(match, needed_qty, needed_unit):
    """Adds packages_needed/total_cost to one price match (#38) - how many
    whole packages of *this* product it takes to cover the needed quantity,
    and the resulting total cost. Falls back to "1 package, this product's
    own price" whenever the fit can't be confidently computed (no needed
    qty, unit mismatch, or a missing/zero unit_price) - that's exactly
    today's existing behavior, so items without a parseable qty aren't
    affected by this at all."""
    match["packages_needed"] = 1
    match["total_cost"] = float(match["price"])
    if not needed_qty or not match["unit_price"] or match["unit"] != needed_unit:
        return
    package_qty = float(match["price"]) / float(match["unit_price"])
    if package_qty <= 0:
        return
    # Rounded before ceil: package_qty is derived as price / unit_price, which
    # lands a hair under the true size - a 14 oz can at $1.03 gives 0.87499999
    # lb, and ceil(0.875 / 0.87499999) = 2 packages at double the cost, for a
    # list asking for exactly one can (review of #70; ~350 of 20k realistic
    # size/price pairs hit this). Six places keeps real overage intact.
    packages = max(1, math.ceil(round(needed_qty / package_qty, 6)))
    match["packages_needed"] = packages
    match["total_cost"] = packages * float(match["price"])


def summarize_where_to_buy(per_item, stores):
    """(split_total, store_totals) for where-to-buy (#31) - pure, so it is
    testable without a database (#70 phase 2).

    split_total: buy every item at its cheapest store. store_totals: one entry
    per store with what the items it carries would cost there and how many it
    covers, stores covering the whole list first, then cheapest. A store missing
    an item is never ranked as if that item were free.
    """
    split_total = sum(p["cheapest"]["total_cost"] for p in per_item) if per_item else None
    store_totals = []
    for store in sorted(stores):
        total = 0.0
        covered = 0
        for p in per_item:
            match = p["by_store"].get(store)
            if match:
                total += match["total_cost"]
                covered += 1
        store_totals.append({
            "store": store, "total": total, "covered": covered,
            "of_total": len(per_item), "covers_all": covered == len(per_item),
        })
    store_totals.sort(key=lambda s: (not s["covers_all"], s["total"]))
    return split_total, store_totals


@app.route("/list/where-to-buy")
def where_to_buy():
    """The payoff feature (#31): for each unchecked list item, the cheapest
    store; plus a "shop one store" vs "split across stores" total
    comparison. v1 - no store-count minimization. Matching is #25's real
    cross-store matching (see matching.py): token-normalized, not a bare
    substring search, and reduced to one best identity match per store
    before any price comparison happens - see the comment in
    matching.best_per_store for why that ordering matters."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conn.commit()
            cur.execute(
                "SELECT id, name, qty, pinned_product, pinned_store "
                "FROM grocery_list_items WHERE checked = FALSE ORDER BY added_at"
            )
            items = cur.fetchall()

            per_item = []
            unmatched = []
            all_stores = set()
            # None means "no price data at all", which the template renders
            # differently from "price data, but old" (#62). Set inside the
            # guard below because there is no catalog to derive it from
            # otherwise.
            freshness = None

            if price_data_available(cur):
                catalog = matching.cached_catalog(cur)
                freshness = price_freshness_from_catalog(catalog)
                for item in items:
                    # One product per store (the best identity match, not
                    # just "cheapest thing that loosely matched") - see
                    # matching.best_per_store. Items the user pinned to a
                    # specific catalogue product resolve to exactly that
                    # product instead (#98). Anything left unmatched here
                    # shows up under "No price match found for" rather than
                    # disappearing (#25's "done" bar).
                    by_store = resolve_item_match(catalog, item)
                    if not by_store:
                        unmatched.append(item)
                        continue

                    needed_qty, needed_unit = _parse_needed_qty(item["qty"])
                    for m in by_store.values():
                        _annotate_package_fit(m, needed_qty, needed_unit)

                    cheapest = min(by_store.values(), key=lambda m: m["total_cost"])
                    per_item.append({"item": item, "cheapest": cheapest, "by_store": by_store})
                    all_stores.update(by_store.keys())
            else:
                unmatched = list(items)
    finally:
        conn.close()

    split_total, store_totals = summarize_where_to_buy(per_item, all_stores)

    return render_template(
        "where_to_buy.html", per_item=per_item, unmatched=unmatched,
        split_total=split_total, store_totals=store_totals, freshness=freshness,
        low_confidence=matching.LOW_CONFIDENCE_SCORE,
    )


RECIPE_UNIT_WORDS = (
    "cups?|tbsp|tablespoons?|tsp|teaspoons?|oz|ounces?|lbs?|pounds?|"
    "g|grams?|kg|ml|l|liters?|cloves?|cans?|pinch|dash|each|ea|slices?|pieces?"
)
_INGREDIENT_LINE = re.compile(
    rf"^\s*([\d./]+)?\s*({RECIPE_UNIT_WORDS})?\b\s*(.*?)\s*$", re.IGNORECASE
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


# Unicode vulgar fractions, as pasted from recipe sites ("½ cup sugar").
_UNICODE_FRACTIONS = {"½": 0.5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": 0.25, "¾": 0.75, "⅛": 0.125}
_MIXED_NUMBER = re.compile(r"^\s*(\d+)?\s*(?:(\d+)\s*/\s*(\d+)|([½⅓⅔¼¾⅛]))(?=\s|[a-z]|$)", re.IGNORECASE)


def _normalize_leading_amount(line):
    """"1 1/2 cups" -> "1.5 cups", "½ cup" -> "0.5 cup", "2½ lb" -> "2.5 lb".

    Without this the line regex stopped at the first space, so "1 1/2 cups flour"
    parsed as amount 1 and the name "1/2 cups flour" - a wrong amount saved
    silently, which the planner then subtracts from a shopping list (review of
    #75). A plain "1/2 cup" is left as written, as it always was. A range
    ("1-2 cups") is deliberately NOT collapsed to either end; the caller sees
    its leftovers and refuses it where an amount matters.
    """
    m = _MIXED_NUMBER.match(line)
    if not m:
        return line
    whole, num, den, uni = m.groups()
    if num and not whole:
        return line
    try:
        frac = int(num) / int(den) if num else _UNICODE_FRACTIONS[uni]
    except ZeroDivisionError:
        return line
    value = (int(whole) if whole else 0) + frac
    return f"{round(value, 4):g}" + line[m.end():]


def parse_ingredient_line(line):
    """Best-effort split of a free-text ingredient line into (amount, unit,
    name). Falls back to putting the whole line in `name` if it doesn't
    look like "<amount> <unit> <name>" - this is deliberately simple
    (recipe-unit conversion is #36's job, not this)."""
    line = _normalize_leading_amount(line)
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


# --- #27: "paste a whole recipe" import. Deliberately paste-only, not
# URL-fetching - scraping arbitrary third-party recipe sites reliably needs
# real browser rendering (this app's own grocery scrapers already show how
# heavy that is just for a handful of known store sites, see #41/#42) and
# raises reliability/scope concerns a copy-paste box doesn't. This is a
# pre-fill convenience, not a silent auto-save: the parsed result always
# lands back in the normal, editable recipe form for review before saving,
# through the exact same /recipes/new path a manually-typed recipe uses.
_PASTE_SECTION_HEADERS = {
    "ingredients": re.compile(r"^\s*ingredients\s*:?\s*$", re.IGNORECASE),
    "instructions": re.compile(r"^\s*(instructions|directions|method|steps|preparation)\s*:?\s*$", re.IGNORECASE),
}
_PASTE_SERVINGS = re.compile(r"(?:serves|servings?|yield)s?\s*:?\s*(\d+)", re.IGNORECASE)
_PASTE_INGREDIENT_LEADIN = re.compile(r"^\s*[\d./]+\s")


def parse_pasted_recipe(text):
    """Best-effort split of a whole pasted recipe blob into the same
    (name, servings, notes, ingredients_text) fields the structured form
    already uses. Looks for explicit "Ingredients"/"Instructions" section
    headers first, since that's how most recipes are actually formatted;
    falls back to "a line starting with a number is an ingredient" when no
    headers are found. Either way this is a heuristic, not a real parser -
    expect it to get unusual formats wrong sometimes, which is exactly why
    the result is only ever a pre-fill, never saved directly."""
    lines = [line.rstrip() for line in text.splitlines()]
    non_blank = [line for line in lines if line.strip()]
    name = non_blank[0].strip() if non_blank else ""

    servings_match = _PASTE_SERVINGS.search(text)
    servings = servings_match.group(1) if servings_match else None

    ingredients_start = None
    instructions_start = None
    for i, line in enumerate(lines):
        if ingredients_start is None and _PASTE_SECTION_HEADERS["ingredients"].match(line):
            ingredients_start = i + 1
        elif ingredients_start is not None and instructions_start is None \
                and _PASTE_SECTION_HEADERS["instructions"].match(line):
            instructions_start = i
            break

    if ingredients_start is not None:
        end = instructions_start if instructions_start is not None else len(lines)
        ingredient_lines = [line for line in lines[ingredients_start:end] if line.strip()]
        notes_lines = lines[instructions_start + 1:] if instructions_start is not None else []
    else:
        ingredient_lines, notes_lines = [], []
        for line in non_blank[1:]:
            (ingredient_lines if _PASTE_INGREDIENT_LEADIN.match(line) else notes_lines).append(line)

    return name, servings, "\n".join(notes_lines).strip(), "\n".join(ingredient_lines)


@app.route("/recipes/parse_paste", methods=["POST"])
def parse_paste_recipe():
    pasted = request.form.get("pasted", "")
    name, servings, notes, ingredients_text = parse_pasted_recipe(pasted)
    # Say how much was found, and that nothing is saved yet. #27 deliberately
    # pre-fills an editable form instead of saving directly, and a silent
    # transition to a filled-in form does not communicate that distinction.
    found = len([line for line in ingredients_text.splitlines() if line.strip()])
    if found:
        where = f' in "{name}"' if name else ""
        flash(f"Found {found} ingredient(s){where}. "
              "Nothing is saved yet - check them over, then save.", "info")
    else:
        flash("Couldn't find any ingredients in that text. Check the formatting, "
              "or fill the form in by hand.", "warning")
    return render_template(
        "recipe_form.html", recipe={"name": name, "servings": servings, "notes": notes},
        ingredients_text=ingredients_text, form_action=url_for("new_recipe"),
        show_paste_import=True, is_edit=False,
    )


@app.route("/recipes")
def recipes():
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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
        return render_template(
            "recipe_form.html", recipe=None, ingredients_text="",
            form_action=url_for("new_recipe"), show_paste_import=True, is_edit=False,
        )

    name = request.form.get("name", "").strip()
    notes = request.form.get("notes", "").strip()
    servings = request.form.get("servings") or None
    ingredients_text = request.form.get("ingredients", "")
    if not name:
        abort(400, "Recipe name is required")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
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


# --- #75: several recipes in one paste --------------------------------------
#
# #27's paste box takes one recipe and pre-fills the form; moving a household's
# existing collection in was one paste, one review, one save per recipe - the
# adoption cliff #75 describes. This splits a paste on lines of "---", runs each
# chunk through the same parse_pasted_recipe(), and shows every result as an
# editable card with an include checkbox. Still never a silent save: nothing is
# written until "Save" on the preview, which is #27's rule applied to a batch.
_RECIPE_SEPARATOR = re.compile(r"^\s*-{3,}\s*$", re.MULTILINE)


def split_recipe_paste(text):
    """Pasted text -> parsed recipes, one per "---"-separated chunk, blank
    chunks dropped."""
    recipes = []
    for chunk in _RECIPE_SEPARATOR.split(text or ""):
        if not chunk.strip():
            continue
        name, servings, notes, ingredients_text = parse_pasted_recipe(chunk)
        recipes.append({
            "name": name, "servings": servings, "notes": notes,
            "ingredients_text": ingredients_text,
            "ingredient_count": len([x for x in ingredients_text.splitlines() if x.strip()]),
        })
    return recipes


def _servings_or_none(text):
    """recipes.servings is INTEGER; "4-6" or "serves 4" must not 500 the save."""
    try:
        value = int(str(text).strip())
    except (TypeError, ValueError):
        return None
    # Capped: "Yield: 99999999999" overflows INTEGER, and one bad row would
    # roll back the whole import batch.
    return value if 0 < value <= 1000 else None


@app.route("/recipes/import", methods=["GET", "POST"])
def import_recipes():
    if request.method == "GET":
        return render_template("recipe_import.html", recipes=None, pasted="")
    pasted = request.form.get("pasted", "")
    recipes = split_recipe_paste(pasted)
    if not recipes:
        flash("Nothing to import - paste one or more recipes, separated by a line of ---.", "error")
    return render_template("recipe_import.html", recipes=recipes, pasted=pasted)


@app.route("/recipes/import/save", methods=["POST"])
def save_imported_recipes():
    names = request.form.getlist("name")
    servings = request.form.getlist("servings")
    notes = request.form.getlist("notes")
    ingredients = request.form.getlist("ingredients")
    include = set(request.form.getlist("include"))
    saved, skipped = [], 0
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for i, name in enumerate(names):
                name = name.strip()
                if str(i) not in include or not name:
                    skipped += 1
                    continue
                cur.execute(
                    "INSERT INTO recipes (name, notes, servings) VALUES (%s, %s, %s) RETURNING id",
                    (name, (notes[i] if i < len(notes) else "").strip() or None,
                     _servings_or_none(servings[i] if i < len(servings) else None)),
                )
                _save_ingredients(cur, cur.fetchone()[0], ingredients[i] if i < len(ingredients) else "")
                saved.append(name)
        conn.commit()
    finally:
        conn.close()
    if saved:
        flash(f"Saved {len(saved)} recipe(s): {', '.join(saved[:5])}"
              + (f" and {len(saved) - 5} more" if len(saved) > 5 else "") + ".", "success")
    if skipped:
        flash(f"{skipped} left out (unticked or without a name).", "info")
    return redirect(url_for("recipes"))


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
            return render_template(
                "recipe_form.html", recipe=recipe, ingredients_text="\n".join(lines),
                form_action=url_for("edit_recipe", recipe_id=recipe_id), show_paste_import=False, is_edit=True,
            )

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
    name = None
    ingredients = 0
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Say what was destroyed, and how much. recipe_ingredients is
            # ON DELETE CASCADE, so deleting a recipe silently takes its
            # ingredients with it - the confirmation is the only place that
            # cascade is ever visible to the person who clicked.
            cur.execute(
                "SELECT r.name, count(i.id) FROM recipes r "
                "LEFT JOIN recipe_ingredients i ON i.recipe_id = r.id "
                "WHERE r.id = %s GROUP BY r.name",
                (recipe_id,),
            )
            row = cur.fetchone()
            if row:
                name, ingredients = row[0], row[1]
            cur.execute("DELETE FROM recipes WHERE id = %s", (recipe_id,))
        conn.commit()
    finally:
        conn.close()
    flash(f"Deleted recipe {name} and its {ingredients} ingredient(s)." if name
          else "That recipe was already gone.", "success")
    return redirect(url_for("recipes"))


DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def ensure_planner_table(cur):
    # meal_plan_slots.recipe_id REFERENCES recipes(id), so `recipes` must exist
    # before this CREATE runs. On a fresh database it did not: /planner called
    # ensure_planner_table() *before* ensure_recipes_tables(), the CREATE failed
    # with UndefinedTable, and the aborted transaction rolled back the `recipes`
    # table created moments later in that same transaction - so nothing persisted
    # and every subsequent request repeated the identical failure. A first-time
    # deploy therefore had a permanently broken /planner, /history and seven
    # other routes (#80). The dev database had been populated for weeks, which is
    # why this was never hit.
    #
    # The dependency is declared here, beside the FK that creates it, so this
    # function is correct on its own whatever order it is called in. Since #66
    # it only runs as part of migration 1 (migrations.py), once, at startup.
    ensure_recipes_tables(cur)
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
    # "Cooked" is a separate, explicit action from being planned (#32) - a
    # planned meal that got skipped/swapped shouldn't silently deplete
    # pantry, so this can't just be inferred from the slot existing.
    cur.execute("ALTER TABLE meal_plan_slots ADD COLUMN IF NOT EXISTS cooked BOOLEAN NOT NULL DEFAULT FALSE;")
    cur.execute("ALTER TABLE meal_plan_slots ADD COLUMN IF NOT EXISTS cooked_at TIMESTAMP;")
    cur.execute("ALTER TABLE meal_plan_slots ADD COLUMN IF NOT EXISTS cooked_by TEXT;")
    # NULL = use the recipe's own `servings` as-is, no scaling (#47) - only
    # set when someone overrides it for this particular night (e.g. company
    # coming), so a recipe used elsewhere unscaled is unaffected.
    cur.execute("ALTER TABLE meal_plan_slots ADD COLUMN IF NOT EXISTS servings INTEGER;")
    # A slot used to hold exactly one recipe (unique per week/day/meal). #44
    # lets a slot hold several recipes (e.g. burgers + buns), so recipe_id
    # has to join the uniqueness instead of being excluded from it - swap
    # the old constraint for the new one, idempotently - it was written when
    # this ran on every request; since #66 it runs once, in migration 1.
    cur.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = 'meal_plan_slots'::regclass
                AND conname = 'meal_plan_slots_week_start_day_of_week_meal_key'
            ) THEN
                ALTER TABLE meal_plan_slots DROP CONSTRAINT meal_plan_slots_week_start_day_of_week_meal_key;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = 'meal_plan_slots'::regclass
                AND conname = 'meal_plan_slots_slot_recipe_key'
            ) THEN
                ALTER TABLE meal_plan_slots ADD CONSTRAINT meal_plan_slots_slot_recipe_key
                    UNIQUE (week_start, day_of_week, meal, recipe_id);
            END IF;
        END $$;
    """)


def ensure_planner_extras_table(cur):
    """Loose, non-recipe ingredients on a meal slot (#50) - e.g. taco night's
    ground-beef recipe plus shredded cheese/lettuce that don't deserve a
    whole recipe of their own. Deliberately a separate table rather than a
    recipe_id-less row on meal_plan_slots - extras don't have a cooked flag
    or a recipe to look up, they're just a line item."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS meal_plan_extras (
            id SERIAL PRIMARY KEY,
            week_start DATE NOT NULL,
            day_of_week INTEGER NOT NULL,
            meal TEXT NOT NULL DEFAULT 'dinner',
            name TEXT NOT NULL,
            amount TEXT,
            unit TEXT
        );
    """)


def ensure_cook_depletions_table(cur):
    """Snapshot of what "Mark cooked" subtracted from pantry for one day's
    meal, per ingredient (#48) - lets "Used tonight" be edited after the
    fact without stacking corrections: each edit recomputes pantry amount
    from `pantry_before`, not from whatever the pantry currently reads."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cook_depletions (
            id SERIAL PRIMARY KEY,
            week_start DATE NOT NULL,
            day_of_week INTEGER NOT NULL,
            meal TEXT NOT NULL,
            ingredient_name TEXT NOT NULL,
            unit TEXT,
            pantry_before NUMERIC NOT NULL,
            used_amount NUMERIC NOT NULL
        );
    """)


def week_start_for(d):
    """Sunday on or before the given date."""
    return d - datetime.timedelta(days=(d.weekday() + 1) % 7)


# Dinner-first (#28), but the slot mechanism from #44 (multiple recipes per
# slot) generalizes cleanly to other meals - a tab per meal (#45) rather
# than widening the day grid, so a family that only plans dinner never sees
# lunch/breakfast at all unless they click over.
MEALS = ("breakfast", "lunch", "dinner")
MEAL_LABELS = {"breakfast": "Breakfast", "lunch": "Lunch", "dinner": "Dinner"}


def _meal_param():
    meal = request.values.get("meal", "dinner")
    return meal if meal in MEALS else "dinner"


@app.route("/planner")
def planner():
    week_param = request.args.get("week")
    # The two branches call different functions with different argument shapes
    # (parse a supplied week vs. derive this week's start), and the if/else
    # reads more clearly than the equivalent three-line ternary. Length isn't
    # the argument - the ternary would be 113 chars at this indent, under the
    # 120 limit - so this is a readability call, suppressed at the one site
    # rather than by ignoring SIM108 repo-wide.
    if week_param:  # noqa: SIM108
        week_start = datetime.date.fromisoformat(week_param)
    else:
        week_start = week_start_for(datetime.date.today())
    meal = _meal_param()

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conn.commit()
            cur.execute("""
                SELECT s.day_of_week, r.id AS recipe_id, r.name AS recipe_name, s.cooked, s.cooked_by,
                       s.servings AS slot_servings, r.servings AS recipe_servings
                FROM meal_plan_slots s
                JOIN recipes r ON r.id = s.recipe_id
                WHERE s.week_start = %s AND s.meal = %s
                ORDER BY s.day_of_week, r.name
            """, (week_start, meal))
            assigned = {}
            for row in cur.fetchall():
                assigned.setdefault(row["day_of_week"], []).append(row)

            cur.execute(
                "SELECT id, day_of_week, name, amount, unit FROM meal_plan_extras "
                "WHERE week_start = %s AND meal = %s ORDER BY name",
                (week_start, meal),
            )
            assigned_extras = {}
            for row in cur.fetchall():
                assigned_extras.setdefault(row["day_of_week"], []).append(row)

            cur.execute(
                "SELECT day_of_week, ingredient_name, unit, used_amount FROM cook_depletions "
                "WHERE week_start = %s AND meal = %s ORDER BY ingredient_name",
                (week_start, meal),
            )
            used_tonight = {}
            for row in cur.fetchall():
                used_tonight.setdefault(row["day_of_week"], []).append(row)

            cur.execute("SELECT id, name FROM recipes ORDER BY name")
            all_recipes = cur.fetchall()
    finally:
        conn.close()

    days = []
    today = datetime.date.today()
    for i in range(7):
        recipes = assigned.get(i, [])
        day_date = week_start + datetime.timedelta(days=i)
        days.append({
            "index": i, "name": DAY_NAMES[i], "date": day_date,
            # Display-only flag for the planner to mark today's row (#101).
            # Computed once outside the loop and derived from the same
            # week_start the rest of the page uses, so a week viewed in the
            # past or future simply has no row marked. It changes no query and
            # no behaviour - it exists because a seven-day grid with no anchor
            # to "now" makes you count columns to find today.
            "is_today": day_date == today,
            "recipes": recipes,
            "extras": assigned_extras.get(i, []),
            # All recipes in a slot get cooked/depleted together as one
            # action (see mark_cooked) - "cooked" for the day is true only
            # once every recipe currently in it is, so adding a new recipe
            # to an already-cooked day correctly shows it as needing
            # cooking again rather than silently inheriting the old state.
            "cooked": bool(recipes) and all(r["cooked"] for r in recipes),
            "cooked_by": recipes[0]["cooked_by"] if recipes and recipes[0]["cooked"] else None,
            "used_tonight": used_tonight.get(i, []),
        })

    return render_template(
        "planner.html", week_start=week_start, days=days, all_recipes=all_recipes,
        meal=meal, meals=MEALS, meal_labels=MEAL_LABELS,
        prev_week=week_start - datetime.timedelta(days=7),
        next_week=week_start + datetime.timedelta(days=7),
    )


@app.route("/planner/set", methods=["POST"])
def set_planner_slot():
    """Adds a recipe to this day's dinner (#44) - a slot can hold several
    recipes now (e.g. a burger patty recipe + a bun recipe), so this is an
    add, not a replace. The picker always resets to its placeholder after
    each pick rather than showing "current" state - the chips do that."""
    week_start = request.form["week_start"]
    day_of_week = int(request.form["day_of_week"])
    recipe_id = request.form.get("recipe_id") or None
    meal = _meal_param()

    if recipe_id:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO meal_plan_slots (week_start, day_of_week, meal, recipe_id)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (week_start, day_of_week, meal, recipe_id) DO NOTHING
                """, (week_start, day_of_week, meal, recipe_id))
            conn.commit()
        finally:
            conn.close()
        flash(f"Planned for {DAY_NAMES[day_of_week]} {meal}.", "success")
    else:
        # The picker's placeholder submits an empty recipe_id. Saying "Planned"
        # there would confirm a save that never happened.
        flash("Pick a recipe to plan it.", "error")
    return redirect(url_for("planner", week=week_start, meal=meal))


@app.route("/planner/remove_recipe", methods=["POST"])
def remove_planner_recipe():
    """Removes one recipe from this day's meal, leaving any other recipes
    already assigned to that slot untouched (#44)."""
    week_start = request.form["week_start"]
    day_of_week = int(request.form["day_of_week"])
    recipe_id = request.form["recipe_id"]
    meal = _meal_param()

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM meal_plan_slots WHERE week_start = %s AND day_of_week = %s "
                "AND meal = %s AND recipe_id = %s",
                (week_start, day_of_week, meal, recipe_id),
            )
        conn.commit()
    finally:
        conn.close()
    flash(f"Removed from {DAY_NAMES[day_of_week]} {meal}.", "success")
    return redirect(url_for("planner", week=week_start, meal=meal))


@app.route("/planner/add_extra", methods=["POST"])
def add_planner_extra():
    """Adds one loose ingredient line to this day's meal (#50) - same
    "<amount> <unit> <name>" free-text grammar as a recipe's ingredient
    box, one line at a time rather than a textarea, since this is meant for
    the handful of extras a meal needs beyond its recipe(s)."""
    week_start = request.form["week_start"]
    day_of_week = int(request.form["day_of_week"])
    meal = _meal_param()
    line = request.form.get("line", "").strip()

    if line:
        amount, unit, name = parse_ingredient_line(line)
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO meal_plan_extras (week_start, day_of_week, meal, name, amount, unit) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (week_start, day_of_week, meal, name, amount, unit),
                )
            conn.commit()
        finally:
            conn.close()
        # Inside the `if`: amount/unit/name only exist when a line was parsed,
        # so a blank submit used to raise UnboundLocalError here and 500.
        added = " ".join(str(x) for x in (amount, unit, name) if x)
        flash(f"Added {added} to {DAY_NAMES[day_of_week]} {meal}.", "success")
    else:
        flash("Enter an ingredient line to add it.", "error")
    return redirect(url_for("planner", week=week_start, meal=meal))


@app.route("/planner/remove_extra", methods=["POST"])
def remove_planner_extra():
    week_start = request.form["week_start"]
    meal = _meal_param()
    extra_id = request.form["extra_id"]

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM meal_plan_extras WHERE id = %s", (extra_id,))
        conn.commit()
    finally:
        conn.close()
    flash("Removed that extra.", "success")
    return redirect(url_for("planner", week=week_start, meal=meal))


@app.route("/planner/set_servings", methods=["POST"])
def set_planner_servings():
    """Overrides how many servings this recipe is being made for on this
    specific night (#47) - e.g. company's coming, double the burger patties
    just for Friday. Blank/0 clears the override and falls back to the
    recipe's own `servings`, which is what every slot does by default -
    scaling is opt-in per night, never required."""
    week_start = request.form["week_start"]
    day_of_week = int(request.form["day_of_week"])
    recipe_id = request.form["recipe_id"]
    meal = _meal_param()
    servings = request.form.get("servings") or None

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE meal_plan_slots SET servings = %s WHERE week_start = %s AND day_of_week = %s "
                "AND meal = %s AND recipe_id = %s",
                (servings, week_start, day_of_week, meal, recipe_id),
            )
        conn.commit()
    finally:
        conn.close()
    flash(f"Servings set to {servings} - ingredients will scale to match.", "success")
    return redirect(url_for("planner", week=week_start, meal=meal))


def _scale_factor(slot_servings, recipe_servings):
    """(scaled_servings / base_servings), or 1.0 (no scaling) whenever
    either side is missing/zero - conservative, never guesses a scale."""
    if not slot_servings or not recipe_servings:
        return 1.0
    try:
        factor = float(slot_servings) / float(recipe_servings)
    except (TypeError, ValueError, ZeroDivisionError):
        return 1.0
    return factor if factor > 0 else 1.0


def _format_amount(value):
    """A quantity for display: 3.0 -> "3", 6.5 -> "6.5".

    Rounded to 4 places first. Without that, float arithmetic leaked straight
    onto the page - 0.1 + 0.2 displayed as "0.30000000000000004 cup" in the
    week's ingredients, the pantry's "need X more" and merged list quantities
    (found writing #70's helper tests). Fixed-point formatting, not :g, so a
    large amount never turns into "1e+03".
    """
    text = f"{round(float(value), 4):.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def deplete_pantry_for_slot(cur, week_start, day_of_week, meal):
    """Subtracts one day's meal (every recipe + loose extra in the slot,
    combined) from pantry (#30/#32/#48). Same conservative matching as
    apply_pantry: only when both sides have a clean numeric amount and the
    exact same unit; clamps at 0 rather than going negative.

    Takes a fresh pantry_before snapshot per ingredient and records it in
    cook_depletions - this is what "Used tonight" (#48) edits against later,
    so correcting a used amount recomputes from that snapshot instead of
    stacking another delta on top of whatever the pantry currently reads.
    Re-running this (e.g. cook -> undo -> cook again) always takes a new
    snapshot from current pantry state, matching "mark cooked" being a
    one-tap action with no confirmation step."""
    cur.execute(
        "DELETE FROM cook_depletions WHERE week_start = %s AND day_of_week = %s AND meal = %s",
        (week_start, day_of_week, meal),
    )
    for ing in slot_ingredient_lines(cur, week_start, day_of_week, meal):
        try:
            used = float(ing["amount"])
        except (TypeError, ValueError):
            continue
        cur.execute("SELECT id, amount, unit FROM pantry_items WHERE lower(name) = lower(%s)", (ing["name"],))
        row = cur.fetchone()
        if not row or row["amount"] is None or row["unit"] != ing["unit"]:
            continue
        pantry_before = float(row["amount"])
        remaining = max(0.0, pantry_before - used)
        cur.execute(
            "UPDATE pantry_items SET amount = %s, updated_at = now(), updated_by = %s WHERE id = %s",
            (remaining, active_profile(), row["id"]),
        )
        cur.execute(
            "INSERT INTO cook_depletions "
            "(week_start, day_of_week, meal, ingredient_name, unit, pantry_before, used_amount) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (week_start, day_of_week, meal, ing["name"], ing["unit"], pantry_before, used),
        )


@app.route("/planner/adjust_used", methods=["POST"])
def adjust_used():
    """Corrects how much of an ingredient actually got used tonight (#48) -
    the "Used tonight" line under a cooked meal. Recomputes pantry from the
    stored pantry_before snapshot rather than the pantry's current amount,
    so editing the same line twice doesn't double-subtract, and this stays
    correct regardless of what else has touched the pantry meanwhile."""
    week_start = request.form["week_start"]
    day_of_week = int(request.form["day_of_week"])
    meal = _meal_param()
    ingredient_name = request.form["ingredient_name"]
    unit = request.form.get("unit") or None
    try:
        new_used = float(request.form["used_amount"])
    except (KeyError, ValueError):
        return redirect(url_for("planner", week=week_start, meal=meal))
    new_used = max(0.0, new_used)

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, pantry_before FROM cook_depletions WHERE week_start = %s AND day_of_week = %s "
                "AND meal = %s AND ingredient_name = %s AND unit IS NOT DISTINCT FROM %s",
                (week_start, day_of_week, meal, ingredient_name, unit),
            )
            depletion = cur.fetchone()
            if depletion:
                new_pantry_amount = max(0.0, float(depletion["pantry_before"]) - new_used)
                cur.execute(
                    "UPDATE cook_depletions SET used_amount = %s WHERE id = %s",
                    (new_used, depletion["id"]),
                )
                cur.execute(
                    "UPDATE pantry_items SET amount = %s, updated_at = now(), updated_by = %s "
                    "WHERE lower(name) = lower(%s) AND unit IS NOT DISTINCT FROM %s",
                    (new_pantry_amount, active_profile(), ingredient_name, unit),
                )
        conn.commit()
    finally:
        conn.close()
    flash(f"Updated {ingredient_name} - the pantry was corrected to match.", "success")
    return redirect(url_for("planner", week=week_start, meal=meal))


@app.route("/planner/cook", methods=["POST"])
def mark_cooked():
    """Marks every recipe assigned to this day's meal cooked together as
    one action (#44) - burger patty + bun are one meal, not two separate
    cook events, so pantry gets depleted for all recipes in the slot."""
    week_start = request.form["week_start"]
    day_of_week = int(request.form["day_of_week"])
    cooked = request.form["cooked"] == "1"
    meal = _meal_param()

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, recipe_id FROM meal_plan_slots WHERE week_start = %s AND day_of_week = %s AND meal = %s",
                (week_start, day_of_week, meal),
            )
            slots = cur.fetchall()
            for slot in slots:
                cur.execute(
                    "UPDATE meal_plan_slots SET cooked = %s, cooked_at = CASE WHEN %s THEN now() ELSE NULL END, "
                    "cooked_by = CASE WHEN %s THEN %s ELSE cooked_by END WHERE id = %s",
                    (cooked, cooked, cooked, active_profile(), slot["id"]),
                )
            # Deplete once for the whole slot (every recipe + extra combined,
            # #48/#50), not once per recipe - two recipes both needing salt
            # should net one depletion, not two independent ones. Deplete on
            # marking cooked, not on un-marking (matches the
            # restock-only-on-check, not-on-uncheck asymmetry in #30 - manual
            # pantry correction, i.e. "Used tonight" (#48), is always
            # available instead of trying to make this perfectly reversible).
            if cooked and slots:
                deplete_pantry_for_slot(cur, week_start, day_of_week, meal)
        conn.commit()
    finally:
        conn.close()
    n = len(slots)
    if cooked:
        flash(f"Marked {n} recipe(s) cooked and depleted the pantry for them." if n else "Marked cooked.", "success")
    else:
        flash("Undid the cooked mark. The pantry was not restocked - correct it by hand if you need to.", "warning")
    return redirect(url_for("planner", week=week_start, meal=meal))


def get_week_ingredients(cur, week_start):
    """Combined (name, unit, amount) list for a planned week, across every
    meal (breakfast/lunch/dinner, #45) - the shopping list should reflect
    everything planned, not just dinner. Sums amounts where numeric and
    sharing a unit, otherwise lists them separately - see #28, exact unit
    math is explicitly OK to be sloppy for v1 (real recipe-unit handling is
    #36's job later). Includes loose ad-hoc extras (#50) and recipe amounts
    scaled per-slot (#47) - both participate exactly like base recipe
    ingredients."""
    cur.execute("""
        SELECT i.name, i.amount, i.unit, s.servings AS slot_servings, r.servings AS recipe_servings
        FROM meal_plan_slots s
        JOIN recipes r ON r.id = s.recipe_id
        JOIN recipe_ingredients i ON i.recipe_id = s.recipe_id
        WHERE s.week_start = %s
    """, (week_start,))
    ingredient_rows = [_scale_ingredient_row(row) for row in cur.fetchall()]

    cur.execute("SELECT name, amount, unit FROM meal_plan_extras WHERE week_start = %s", (week_start,))
    ingredient_rows.extend(cur.fetchall())

    return _combine_ingredient_rows(ingredient_rows)


def _scale_ingredient_row(row):
    """Applies a recipe's per-slot servings scale (#47) to one ingredient
    row; non-numeric amounts ("a pinch") pass through unscaled rather than
    erroring."""
    amount = row["amount"]
    factor = _scale_factor(row["slot_servings"], row["recipe_servings"])
    if amount and factor != 1.0:
        try:
            # Full precision here; rounding happens once, on the combined
            # total (_combine_ingredient_rows), which every display and
            # depletion path goes through. Rounding each part first made three
            # 1/3-cup servings sum to "0.9999 cup" (review of #70).
            amount = repr(float(amount) * factor)
        except (TypeError, ValueError):
            pass
    return {"name": row["name"], "amount": amount, "unit": row["unit"]}


def _combine_ingredient_rows(ingredient_rows):
    """Sums (name, unit) groups where every amount in the group is numeric;
    otherwise joins the raw amount strings rather than guessing."""
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
            display_amount = _format_amount(total)
        elif entry["amounts"]:
            display_amount = " + ".join(entry["amounts"])
        else:
            display_amount = ""
        combined.append({"name": entry["name"], "unit": entry["unit"], "amount": display_amount})
    combined.sort(key=lambda e: e["name"].lower())
    return combined


def slot_ingredient_lines(cur, week_start, day_of_week, meal):
    """Same combined (name, unit, amount) shape as get_week_ingredients, but
    scoped to one day's meal - what "Mark cooked" actually depletes from
    pantry and what "Used tonight" (#48) edits."""
    cur.execute("""
        SELECT i.name, i.amount, i.unit, s.servings AS slot_servings, r.servings AS recipe_servings
        FROM meal_plan_slots s
        JOIN recipes r ON r.id = s.recipe_id
        JOIN recipe_ingredients i ON i.recipe_id = s.recipe_id
        WHERE s.week_start = %s AND s.day_of_week = %s AND s.meal = %s
    """, (week_start, day_of_week, meal))
    ingredient_rows = [_scale_ingredient_row(row) for row in cur.fetchall()]

    cur.execute(
        "SELECT name, amount, unit FROM meal_plan_extras WHERE week_start = %s AND day_of_week = %s AND meal = %s",
        (week_start, day_of_week, meal),
    )
    ingredient_rows.extend(cur.fetchall())

    return _combine_ingredient_rows(ingredient_rows)


def apply_pantry(cur, combined):
    """Annotates each combined ingredient with pantry coverage (#30) - skip
    or reduce items the pantry already has enough of, always showing what
    was skipped/reduced rather than silently dropping it. Only compares
    when both the need and the pantry have a clean numeric amount and the
    exact same unit; anything else is left as a full need - conservative
    on purpose, never guesses its way into subtracting the wrong thing."""
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
        ing["need_amount"] = _format_amount(remaining)
    return combined


# --- #36: cooking-unit -> purchase-unit (lb/gal) conversion via USDA
# FoodData Central. Scoped deliberately narrow for v1: cup/tbsp/tsp go
# through USDA (they're ambiguous - a cup of flour and a cup of oil weigh
# very different amounts, no way to convert without per-ingredient data);
# g/kg/oz/lb/ml/l convert with a fixed factor, no lookup needed. Countable
# units (each/clove/can/slice/piece/pinch/dash) are intentionally not
# attempted - USDA's per-item weights vary too much (see e.g. an egg:
# 38-63g depending on "small" vs "jumbo") to guess at safely.
_USDA_MASS_TO_LB = {"oz": 1 / 16, "lb": 1.0, "g": 0.00220462, "kg": 2.20462}
_USDA_VOLUME_TO_GAL = {"ml": 0.000264172, "l": 0.264172}
# USDA's `modifier` text is inconsistent between foods - sometimes the
# abbreviation ("tsp"), sometimes spelled out ("teaspoon") - match either.
_USDA_MEASURE_WORDS = {"cup": ["cup"], "tbsp": ["tbsp", "tablespoon"], "tsp": ["tsp", "teaspoon"]}
_GRAMS_PER_LB = 453.592

# --- #54: known-ambiguous single-word ingredient names. FDC's own search
# relevance can rank a same-word-different-food result first for these -
# confirmed against the real FDC API while building this fix:
#   "pepper" -> "Pepper, banana, raw" (169394) before the spice
#               "Spices, pepper, black" (170931)
#   "butter" -> "Butter, Clarified butter (ghee)" (171314) before plain
#               "Butter, salted" (173410)
#   "flour"  -> "Arrowroot flour" (170684) before any wheat flour
#   "sugar"  -> "Sugar, turbinado" (170674) before granulated white sugar
# The fix biases the *search query text* toward a more specific phrase
# rather than hand-picking an fdcId directly - it still goes through the
# same whole-word/starts-with filtering below, and stays correct even if
# FDC renumbers or re-ranks its own data (a hardcoded fdcId wouldn't).
# Deliberately small and hand-maintained, same spirit as MANUAL_ALIASES /
# DISQUALIFYING_MODIFIERS in matching.py - grow it from real misses, not
# speculative ones. "onion" was checked too (see test_usda_matching.py)
# but turned out NOT to need an entry here - the whole-word-boundary and
# starts-with fixes below already resolve it correctly on their own once
# plural descriptions ("Onions, raw") are no longer rejected outright.
_USDA_SEARCH_ALIASES = {
    "pepper": "pepper black",
    "black pepper": "pepper black",
    "ground pepper": "pepper black",
    "ground black pepper": "pepper black",
    "cracked pepper": "pepper black",
    "cracked black pepper": "pepper black",
    "butter": "butter salted",
    "flour": "wheat flour all-purpose",
    "all purpose flour": "wheat flour all-purpose",
    "all-purpose flour": "wheat flour all-purpose",
    "sugar": "sugar granulated",
    "white sugar": "sugar granulated",
    "granulated sugar": "sugar granulated",
    # #58: recipe vocabulary that FDC's own descriptions don't use. Verified
    # against the live FDC API (not assumed): a plain "bell pepper" search
    # top-ranks the newer "Peppers, bell, {color}, raw" entries (fdcId
    # 2258588-91) - correct food, but they carry no cup/tbsp portion data at
    # all (only an unmodified ~85g RACC portion), so the ALL-word filter
    # passes them and the portion loop finds nothing -> no estimate.
    #
    # This is NOT a re-ranking problem. The entry that does have usable cup
    # data - "Peppers, sweet, red, raw" (170108) - is absent from a plain
    # "bell pepper" search entirely: the live top-10 is the four
    # "Peppers, bell, *" entries plus six "TACO BELL" branded items. So the
    # alias has to issue a *different* query, not reorder the same one.
    # "peppers sweet red raw" surfaces 170108 and lands on it.
    #
    # This alias fixes the entry (identity). Which cup PORTION of it applies -
    # "cup, sliced" 92g or "cup, chopped" 149g - is #78's choose_portion(): the
    # recipe's form decides, and an unqualified cup has no estimate.
    #
    # Yellow's equivalent (169383) has no cup/tbsp data in real FDC data
    # either - verified: aliasing "yellow bell pepper" to "peppers sweet
    # yellow raw" still returns None - so it is deliberately NOT aliased. A
    # missing estimate beats guessing at one, and an alias that changes
    # nothing would only look like coverage.
    "bell pepper": "peppers sweet red raw",
    "red bell pepper": "peppers sweet red raw",
    "green bell pepper": "peppers sweet green raw",
    # "green onion": FDC's entry is "Onions, spring or scallions (includes
    # tops and bulb), raw" (170005) - it never says "green".
    #
    # Before this alias, "green onion" did NOT fail safe. It resolved to
    # "Onions, young green, tops only" (170006) and returned 71g/cup - the
    # greens-only product, a *silently wrong* estimate rather than a missing
    # one, which is the failure mode this codebase is supposed to avoid. #58
    # was filed on the assumption that this case produced no estimate; it
    # produced a wrong one. The alias lands on 170005 (100g/cup chopped).
    #
    # "raw" is load-bearing - it excludes the canned/frozen/freeze-dried/
    # sauteed entries. "tops" and "bulb" are deliberately NOT included:
    # verified live that "onion spring scallion raw" already yields 170005 as
    # the sole identity-confirmed candidate, so they add no disambiguation,
    # and every extra required word is one more way an FDC rewording silently
    # regresses this to None.
    #
    # "spring onion" is deliberately NOT aliased, despite #58 suggesting it:
    # verified live that it already resolves to 170005 (100g/cup) with no
    # alias at all, because "spring" appears in that description and #54's
    # filter lands it. Adding it would be a no-op that raised the required-
    # word count from 2 to 4 - a speculative entry, which CONTRIBUTING §6
    # forbids in these tables.
    "green onion": "onion spring scallion raw",
    # #123, found in #78's live audit (2026-09-29). Each resolved to a
    # different FOOD before - a wrong estimate, not a missing one:
    #   milk    -> 167686 "Milk dessert, frozen, milk-fat free"  137 g/cup
    #   rice    -> 168951 "Rice and vermicelli mix, rice pilaf"  206 g/cup
    #   spinach -> 170494 "Spinach souffle"                      136 g/cup
    # "1 cup milk" is in the household's own recipes. 2% is the target for
    # plain "milk" because it's the everyday default; whole/2%/skim all weigh
    # ~244-245 g/cup, so the choice doesn't move the estimate. Rice is RAW
    # long-grain white: recipes measure rice uncooked, and that's what gets
    # bought. "tomatoes" is deliberately NOT aliased - canned vs fresh is a
    # real ambiguity, and the current canned answer may be what's meant.
    "milk": "milk reduced fat fluid 2% milkfat",
    "rice": "rice white long grain regular raw",
    "spinach": "spinach raw",
    # Two more misses the same live check turned up: "whole milk" resolved to
    # "Milk, buttermilk, fluid, whole" and "brown rice" to "Rice flour, brown"
    # (158 g/cup - flour, not grain).
    "whole milk": "milk whole 3.25% milkfat",
    "brown rice": "rice brown long grain raw",
    # #126: "milk chocolate" resolved to "Milk dessert, frozen" (137 g/cup) -
    # FDC itself ranks "Candies, milk chocolate" first, but #54's starts-with
    # preference promoted the entry that begins with "milk". Its only cup
    # portion is "cup chips" (168 g), which is how recipes measure it.
    # "chocolate milk" (the drink) hit the same frozen dessert (137 g/cup,
    # confirmed by the independent review with the aliases removed); it gets
    # the fluid commercial chocolate-milk entry (250 g/cup).
    "milk chocolate": "candies milk chocolate",
    "chocolate milk": "milk chocolate fluid commercial reduced fat",
    # #131: "chocolate milk mix" resolved to "Rennin, chocolate, dry mix,
    # prepared with 2% milk" (142 g/cup) - a pudding-type dessert. The drink
    # powder is FDC 173182 "Beverages, rich chocolate, powder".
    "chocolate milk mix": "beverages rich chocolate powder",
}


def ensure_ingredient_conversions_table(cur):
    """Local cache of resolved cooking-unit -> grams-per-unit lookups (#36)
    so a repeat ingredient (flour, sugar, etc. show up in most weeks) never
    re-hits the USDA API. Grows organically as new ingredients are used;
    never pre-seeded."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ingredient_conversions (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            unit TEXT NOT NULL,
            grams_per_unit NUMERIC NOT NULL,
            updated_at TIMESTAMP DEFAULT now(),
            UNIQUE (name, unit)
        );
    """)


# --- Portion choice within one FDC entry (#78) ----------------------------------
#
# An FDC entry often carries several portions for the same measure, differing by
# the food's form: "cup, chopped" vs "cup, sliced", "cup packed" vs "cup
# unpacked", "cup sifted" vs "unsifted". The old loop returned whichever came
# first in FDC's list, which is not a meaning - red bell pepper got 92 g/cup
# (sliced listed first) and green got 149 g/cup (chopped first). Audited live on
# 2026-09-29 across the household's ingredients and common staples:
#   bell pepper    sliced 92 / chopped 149        62% apart
#   brown sugar    packed 220 / unpacked 145      52%
#   onion          chopped 160 / sliced 115       39%
#   pepper (tsp)   ground 2.3 / whole 2.9         26%
#   powdered sugar sifted 100 / unsifted 120      20%
#   carrots        grated 110 / chopped 128 / strips 122   16%
#   broccoli       chopped or diced 88 / chopped 91          3%
# Policy, deliberately and in this order:
#   1. The recipe says the form ("chopped onion", "brown sugar, packed") and
#      exactly one portion carries it -> that portion.
#   2. The recipe names a form no portion carries ("melted butter") -> no
#      estimate: the form may change the weight, so an unqualified portion
#      would be a guess (#54 already decided melted butter must be None).
#   3. Every matching portion agrees within PORTION_AGREEMENT -> their median.
#      The form doesn't matter at that point, so neither does FDC's order.
#   4. Otherwise -> no estimate. #78 is explicit that silently preferring one of
#      two genuinely different forms is worse than None, and so is this
#      codebase ("a missing purchase estimate is fine, a wrong one silently
#      corrupts the shopping list"). Recipes usually name the form, which
#      rule 1 then honours.
# Only words that describe a PREPARATION and are never part of a food's name.
# Deliberately excluded, though FDC uses them as portion modifiers: "ground"
# ("ground beef" is Beef, ground), "whole" ("whole milk"), "crushed" ("crushed
# tomatoes"), "mashed", "shredded", "grated" ("Cheese, parmesan, grated" - a
# grated-parmesan search stripped to "parmesan cheese" lost its 100 g/cup) -
# stripping those changed the food searched for. The trade-off was measured the other way too: searching the full name
# first instead made "chopped onion" resolve to a processed-onion entry at 210
# g/cup rather than "Onions, raw" at 160. Cost of the exclusion: pepper by the
# tsp ("tsp, ground" 2.3 vs "tsp, whole" 2.9) has no estimate.
_FORM_WORDS = (
    "chopped", "sliced", "diced", "minced", "packed", "unpacked",
    "sifted", "unsifted", "melted", "cubed", "halved",
)
_FORM_WORD_RE = re.compile(r"\b(" + "|".join(_FORM_WORDS) + r")\b", re.IGNORECASE)
PORTION_AGREEMENT = 0.15


def _split_form_words(name):
    """"chopped onion" -> ("onion", {"chopped"}); "brown sugar, packed" ->
    ("brown sugar", {"packed"}). The form steers portion choice; searching FDC
    with it would fail #54's all-words identity filter ("Onions, raw" never
    says "chopped")."""
    forms = {m.lower() for m in _FORM_WORD_RE.findall(name or "")}
    base = _FORM_WORD_RE.sub(" ", name or "")
    base = re.sub(r"\s*,\s*$|^\s*,\s*", "", re.sub(r"\s+", " ", base)).strip(" ,")
    return (base or name), forms


def choose_portion(matching, forms):
    """[(modifier, grams_per_unit), ...] for one entry -> grams, or None. See the
    policy above."""
    if not matching:
        return None
    if forms:
        named = [g for m, g in matching
                 if any(re.search(rf"\b{re.escape(f)}\b", m, re.IGNORECASE) for f in forms)]
        if len(named) == 1:
            return named[0]
        if not named:
            # The recipe names a form FDC has no portion for ("melted butter",
            # "diced onion"). The form may change the weight per cup, so an
            # unqualified portion would be a guess - #54 already decided
            # "melted butter" must be None rather than plain butter's 227 g.
            return None
    values = sorted(g for _, g in matching)
    if values[-1] <= values[0] * (1 + PORTION_AGREEMENT):
        mid = len(values) // 2
        return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
    return None


def _usda_grams_per_unit(name, measure_words):
    """Looks up how many grams one of `measure_words` (e.g. ["tsp",
    "teaspoon"]) of `name`
    weighs, via USDA FoodData Central. Returns None on any failure (no API
    key, no network, no match, no matching portion) rather than guessing -
    a missing purchase estimate is fine, a wrong one silently corrupts the
    shopping list."""
    api_key = os.getenv("USDA_API_KEY")
    if not api_key:
        return None
    # #78: form words ("chopped") choose the portion; they are not part of the
    # food's identity, so they are removed before searching. See _FORM_WORDS for
    # why words like "ground" and "whole" are deliberately not treated as forms.
    base_name, forms = _split_form_words(name)
    return _usda_resolve(base_name, measure_words, forms, api_key)


def _usda_resolve(query, measure_words, forms, api_key):
    """One FDC search + portion choice for `query`: grams per unit, or None."""
    try:
        # #54: search the alias's biased phrase for known-ambiguous names
        # (see _USDA_SEARCH_ALIASES above), the raw name otherwise.
        search_name = _USDA_SEARCH_ALIASES.get((query or "").strip().lower(), query)
        search = requests.get(
            "https://api.nal.usda.gov/fdc/v1/foods/search",
            params={"query": search_name, "pageSize": 10, "dataType": "SR Legacy,Foundation", "api_key": api_key},
            timeout=5,
        )
        search.raise_for_status()
        foods = search.json().get("foods", [])
        query_words = [w for w in re.findall(r"[a-z]+", search_name.lower()) if len(w) >= 3]
        if not query_words:
            return None

        # USDA's search is fuzzy relevance, not exact - "salt" can top-match
        # "Butter, salted". Require a whole-word hit for EVERY significant
        # query word (not just any one of them, #54 - a query like "melted
        # butter" used to accept "Butter, Clarified butter (ghee)" on the
        # strength of "butter" alone, since FDC's raw-ingredient entries
        # never actually say "melted"; requiring both words correctly
        # rejects it instead of guessing) in the result's description before
        # trusting its data at all. "s?" tolerates FDC's own plural
        # phrasing ("Onions, raw") that a strict \bonion\b boundary would
        # otherwise reject outright - confirmed against real data that this
        # was silently letting "DENNY'S, onion rings" (the only *singular*
        # "onion" hit) through as the sole candidate for a bare "onion"
        # query. This can still accept a same-word-different-food match -
        # it narrows the risk, doesn't eliminate it - see the starts-with
        # preference below and _USDA_SEARCH_ALIASES for the rest of #54's
        # fix.
        def _word_hits(description):
            desc = description.lower()
            return all(re.search(rf"\b{re.escape(w)}s?\b", desc) for w in query_words)

        foods = [f for f in foods if _word_hits(f.get("description", ""))]
        if not foods:
            return None

        # #54: prefer a description that *starts with* one of the query
        # words over one where the word merely appears somewhere in it.
        # FDC's own naming convention puts the generic/plain food first
        # ("Onions, raw", "Butter, salted", "Flour, wheat, all-purpose...")
        # and pushes a same-word-different-food match to a modifier
        # position instead ("Spices, pepper, black", "Almond butter,
        # creamy") - confirmed against real search results for every
        # ingredient named in the issue. Sort is stable, so FDC's own
        # relevance order (still a meaningful signal) is preserved within
        # each of the two tiers.
        def _starts_with_query_word(description):
            desc = description.lower()
            return any(re.match(rf"{re.escape(w)}s?\b", desc) for w in query_words)

        foods.sort(key=lambda f: not _starts_with_query_word(f.get("description", "")))

        # Try a few identity-confirmed candidates in order, not just the
        # top one - a correct match can still lack portion data for the
        # requested measure (e.g. FDC's newer "Foundation" flour/sugar
        # entries only carry a RACC portion, no "1 cup" - confirmed while
        # building this; an older "SR Legacy" entry for the same food a few
        # slots down does). All candidates here already passed the
        # whole-word identity filter above, so this doesn't reopen the
        # same-word-different-food risk the rest of #54 is fixing.
        for food in foods[:5]:
            # 20 s, not 5 (#123): FDC's Foundation record 746782 for whole milk
            # took 9.5 s, so a 5 s timeout made "whole milk" return None. This
            # runs in #64's background worker, so a longer wait costs no page any
            # time. A failure still ABANDONS the lookup rather than moving to the
            # next candidate: later candidates are often a different form of the
            # food ("Rice noodles, cooked" then "..., dry"), and a success is
            # cached permanently, so skipping would turn a transient network
            # error into a permanent wrong estimate (review of #123). A failure
            # is only remembered in memory for an hour, then retried.
            detail = requests.get(
                f"https://api.nal.usda.gov/fdc/v1/food/{food['fdcId']}",
                params={"api_key": api_key},
                timeout=20,
            )
            detail.raise_for_status()
            matching = []
            for portion in detail.json().get("foodPortions") or []:
                modifier = (portion.get("modifier") or "").lower()
                gram_weight = portion.get("gramWeight")
                amount = portion.get("amount") or 1.0
                if gram_weight and amount and any(w in modifier for w in measure_words):
                    matching.append((modifier, float(gram_weight) / float(amount)))
            if matching:
                # The first candidate WITH this measure is the identity match;
                # an ambiguous portion there is an answer (None), not a reason
                # to fall through to a less relevant food that happens to be
                # unambiguous.
                return choose_portion(matching, forms)
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None
    return None


# --- USDA lookups off the request path (#64) ---------------------------------
#
# /planner/ingredients used to call USDA synchronously for every uncached
# ingredient: up to 6 HTTPS requests x 5s timeout each, inside an open
# transaction that only committed at the end. A new week could take minutes to
# load, and if the request died partway every resolution it had paid for was
# rolled back, so the next attempt started cold again - indefinitely.
#
# Now the request path only reads the ingredient_conversions cache. A miss is
# queued for ONE background thread per worker process, which resolves it on its
# own connection and commits per ingredient, so progress is banked as it
# happens. The page shows "estimating" for queued items and refreshes itself
# until none are left.
#
# Why a thread and not the scheduler (#64's first suggestion): the scraper image
# does not contain the webapp's code, and a thread needs no new service. Why
# one thread per process: USDA rate-limits per key, and serialising keeps a new
# week with 25 misses from firing 150 requests at once.
#
# All of this state is PER GUNICORN WORKER (the entrypoint runs --workers 2),
# not global. Refreshes can land on either worker, so an item can be looked up
# once by each, and a failed lookup keeps showing "estimating" until both have
# recorded it - at worst twice the requests and twice the wait, and it still
# converges. The cache table is shared, so any SUCCESS is seen by both workers
# at once. Accepted rather than solved with shared state: two lookups instead
# of one, for a household, is not worth a coordination mechanism.
#
# Failures are remembered in memory for _USDA_MISS_TTL rather than in the
# table. _usda_grams_per_unit returns None for "no key", "no network" and "FDC
# has no usable portion" alike, and only the last is permanent - persisting it
# would turn a brief outage into a permanently missing estimate. Forgetting it
# after an hour costs at most one background retry per ingredient per hour.
_USDA_MISS_TTL = 3600
_usda_queue = queue.Queue()
_usda_lock = threading.Lock()
_usda_pending = set()      # (lower name, unit) queued or in flight
_usda_misses = {}          # (lower name, unit) -> monotonic time it failed
_usda_worker = None


def _usda_resolve_in_background(name, unit, measure_words):
    """Worker body for one queued lookup. Never raises - an exception here would
    kill the only worker thread and leave every later miss pending forever."""
    key = (name.lower(), unit)
    grams = None
    try:
        grams = _usda_grams_per_unit(name, measure_words)
        if grams is not None:
            # connect_timeout: this is the worker's only thread, so a db host
            # that stops answering would otherwise park it in connect() and
            # leave every later item "estimating".
            conn = get_connection(connect_timeout=5)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO ingredient_conversions (name, unit, grams_per_unit) VALUES (%s, %s, %s) "
                        "ON CONFLICT (name, unit) DO UPDATE SET grams_per_unit = EXCLUDED.grams_per_unit, "
                        "updated_at = now()",
                        (name, unit, grams),
                    )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        app.logger.exception("background USDA lookup failed for %r (%s)", name, unit)
        grams = None
    finally:
        # In `finally` so even a BaseException that escapes the handler above
        # clears the key; a key left pending makes the page refresh forever.
        with _usda_lock:
            _usda_pending.discard(key)
            if grams is None:
                _usda_misses[key] = time.monotonic()


def _usda_worker_loop():
    while True:
        _usda_resolve_in_background(*_usda_queue.get())


def _queue_usda_lookup(name, unit, measure_words):
    """Queue a background lookup unless one is already pending or recently
    failed. Returns "pending" or "miss" for the page to show."""
    global _usda_worker
    key = (name.lower(), unit)
    with _usda_lock:
        if key in _usda_pending:
            return "pending"
        failed_at = _usda_misses.get(key)
        if failed_at is not None and time.monotonic() - failed_at < _USDA_MISS_TTL:
            return "miss"
        # Missing key: nothing to wait for, and queueing would show
        # "estimating" for something that can never resolve.
        if not os.getenv("USDA_API_KEY"):
            return "miss"
        _usda_pending.add(key)
        # Started lazily, in the process that serves requests: gunicorn forks
        # workers after importing the app, and a thread started at import time
        # would exist only in the master, which never serves anything.
        if _usda_worker is None or not _usda_worker.is_alive():
            _usda_worker = threading.Thread(target=_usda_worker_loop, name="usda-lookups", daemon=True)
            _usda_worker.start()
    _usda_queue.put((name, unit, measure_words))
    return "pending"


def resolve_purchase_amount(cur, name, amount_str, unit):
    """Best-effort (amount, unit) in purchase terms (lb or gal) for one
    ingredient line (#36) - e.g. "2 cups flour" -> "~0.55 lb". Returns None
    when it can't resolve confidently, which is the common case for v1's
    intentionally narrow unit coverage, or "pending" when a USDA lookup has been
    queued and the answer isn't known yet (#64). Never calls USDA itself."""
    try:
        amount = float(amount_str)
    except (TypeError, ValueError):
        return None
    if not unit:
        return None
    unit = unit.lower()

    if unit in _USDA_MASS_TO_LB:
        return round(amount * _USDA_MASS_TO_LB[unit], 2), "lb"
    if unit in _USDA_VOLUME_TO_GAL:
        return round(amount * _USDA_VOLUME_TO_GAL[unit], 3), "gal"

    measure_words = _USDA_MEASURE_WORDS.get(unit)
    if not measure_words:
        return None

    cur.execute(
        "SELECT grams_per_unit FROM ingredient_conversions WHERE lower(name) = lower(%s) AND unit = %s",
        (name, unit),
    )
    row = cur.fetchone()
    if not row:
        return "pending" if _queue_usda_lookup(name, unit, measure_words) == "pending" else None
    grams_per_unit = float(row["grams_per_unit"] if isinstance(row, dict) else row[0])
    return round(amount * grams_per_unit / _GRAMS_PER_LB, 2), "lb"


def apply_purchase_estimates(cur, combined):
    """Annotates each combined ingredient with a best-effort purchase-unit
    estimate (#36), for display only - doesn't change what gets added to
    the grocery list (#38's job, package-size fitting, is the next step
    once this exists). Returns how many are still being looked up (#64)."""
    pending = 0
    for ing in combined:
        resolved = resolve_purchase_amount(cur, ing["name"], ing["amount"], ing["unit"])
        ing["purchase_pending"] = resolved == "pending"
        pending += ing["purchase_pending"]
        ing["purchase_amount"], ing["purchase_unit"] = (
            resolved if resolved and resolved != "pending" else (None, None)
        )
    return pending


@app.route("/history")
def history():
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conn.commit()
            cur.execute("""
                SELECT r.id, r.name, count(*) AS times_cooked, max(s.cooked_at) AS last_cooked
                FROM meal_plan_slots s
                JOIN recipes r ON r.id = s.recipe_id
                WHERE s.cooked = TRUE
                GROUP BY r.id, r.name
                ORDER BY last_cooked ASC
            """)
            rows = cur.fetchall()
    finally:
        conn.close()
    return render_template("history.html", rows=rows)


@app.route("/history/readd", methods=["POST"])
def readd_recipe():
    """Re-add a recipe to this week's plan in one click (#32) - fills the
    first open dinner slot; if the week's fully planned already, does
    nothing rather than overwriting something you already chose."""
    recipe_id = request.form["recipe_id"]
    week_start = week_start_for(datetime.date.today())

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT day_of_week FROM meal_plan_slots WHERE week_start = %s AND meal = 'dinner'",
                (week_start,),
            )
            taken = {row[0] for row in cur.fetchall()}
            open_day = next((d for d in range(7) if d not in taken), None)
            if open_day is not None:
                cur.execute(
                    "INSERT INTO meal_plan_slots (week_start, day_of_week, meal, recipe_id) "
                    "VALUES (%s, %s, 'dinner', %s)",
                    (week_start, open_day, recipe_id),
                )
        conn.commit()
    finally:
        conn.close()
    if open_day is not None:
        flash(f"Planned again for {DAY_NAMES[open_day]} (the first free dinner slot).", "success")
    else:
        flash("Every dinner slot that week is taken, so nothing was added. Pick a week with a free day.", "warning")
    return redirect(url_for("planner", week=week_start))


@app.route("/planner/ingredients")
def planner_ingredients():
    week_start = request.args.get("week") or week_start_for(datetime.date.today())
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conn.commit()
            combined = get_week_ingredients(cur, week_start)
            apply_pantry(cur, combined)
            pending = apply_purchase_estimates(cur, combined)
            conn.commit()
    finally:
        conn.close()
    return render_template("planner_ingredients.html", week_start=week_start, combined=combined,
                           pending=pending)


_QTY_LINE = re.compile(r"^\s*([\d.]+)\s*(\S*)\s*$")


def merge_qty(existing_qty, new_amount, new_unit):
    """Combines a grocery-list item's free-text qty with a new amount/unit
    from the planner. Sums when both are numeric and share a unit,
    otherwise concatenates rather than guessing or dropping data."""
    new_qty = " ".join(str(p) for p in (new_amount, new_unit) if p)
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
                return f"{_format_amount(total)} {existing_unit}".strip()
            except ValueError:
                pass
    return f"{existing_qty} + {new_qty}"


@app.route("/planner/add_to_list", methods=["POST"])
def add_week_to_list():
    week_start = request.form["week_start"]
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conn.commit()
            combined = get_week_ingredients(cur, week_start)
            apply_pantry(cur, combined)

            # Counted so the confirmation can report what really happened. Three
            # different things occur in this loop - skipped because the pantry
            # covers it, merged into an existing unchecked item, or added new -
            # and a bare "done" hides all three. The skipped count matters most:
            # it is the only signal that pantry-aware filtering (#30) actually
            # did something, which otherwise looks like the planner lost items.
            added = merged = skipped = 0
            for ing in combined:
                # Fully covered by pantry (#30) - don't add it to the list.
                if ing["pantry_have"] is not None and ing["need_amount"] == "0":
                    skipped += 1
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
                    merged += 1
                else:
                    qty = " ".join(str(p) for p in (amount, unit) if p) or None
                    cur.execute("INSERT INTO grocery_list_items (name, qty) VALUES (%s, %s)", (ing["name"], qty))
                    added += 1
        conn.commit()
    finally:
        conn.close()
    if not combined:
        flash("No dinners are planned that week, so there was nothing to add.", "warning")
    else:
        parts = [f"{added} added"]
        if merged:
            parts.append(f"{merged} merged into items already on the list")
        if skipped:
            parts.append(f"{skipped} skipped because the pantry covers them")
        flash("Week's ingredients: " + ", ".join(parts) + ".", "success")
    return redirect(url_for("grocery_list"))


def ensure_app_schema(cur):
    """Creates every table this app owns, in dependency order, in one call (#82).

    Three routes - GET /recipes/<id>, GET /recipes/<id>/edit and POST
    /recipes/<id>/delete - query `recipes` while calling no ensure_* function at
    all, so on a fresh database they 500 with UndefinedTable. They only ever
    worked because some other route happened to be visited first. That is the
    same root cause as #80: schema creation is scattered through request
    handlers, so "does this table exist yet" is a property of navigation history
    rather than of the deployment.

    Called once at container startup by init_schema.py, before gunicorn serves
    anything - deliberately not from a request hook. With two workers, a
    per-worker first-request hook would race two concurrent CREATE TABLE IF NOT
    EXISTS calls against an empty database, and that is not atomic in Postgres:
    it was observed during #61's concurrency testing as DuplicateTable and
    UniqueViolation on pg_class_relname_nsp_index.

    Since #66 this is migration 1 in migrations.py, not something init_schema.py
    calls directly, and nothing on the request path calls any ensure_* function.

    Order matters: meal_plan_slots.recipe_id REFERENCES recipes(id), so
    ensure_recipes_tables runs before ensure_planner_table. The rest have no
    cross-function dependencies - app.py has exactly two REFERENCES clauses and
    the other one (recipe_ingredients) is satisfied inside
    ensure_recipes_tables itself. The leading ensure_recipes_tables is
    belt-and-braces given #80 made ensure_planner_table call it too; it is
    idempotent, and having the dependency visible here rather than implicit in
    another function's body is worth one redundant call.

    test_app_schema.py asserts the tables this creates cover every table any
    route in this file queries, so the next route added against a table nobody
    creates fails in CI instead of on someone's first deploy.
    """
    ensure_recipes_tables(cur)
    ensure_planner_table(cur)
    ensure_planner_extras_table(cur)
    ensure_cook_depletions_table(cur)
    ensure_profiles_table(cur)
    ensure_staples_table(cur)
    ensure_list_table(cur)
    ensure_pantry_table(cur)
    ensure_ingredient_conversions_table(cur)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
