"""
Retention policy for `grocery_prices` (#65).

`grocery_prices` only ever grew: collector.py appends a full catalog snapshot
per store on every scrape (46,501 rows in the run measured in #65: Tops 38,279,
BJs 6,204, Aldis 2,018) and nothing pruned, so the daily 03:00 schedule reaches
~17M rows / ~3 GB in a year. This module is the bound on that growth, and the
window is a deliberate choice rather than an accident:

  Keep the last `PRICE_HISTORY_RETENTION_DAYS` days of raw snapshots
  (default 30), measured back from the newest row in the table, and delete
  the rest. Nothing else is touched.

Why 30 days and not 90 - measured, not guessed. On a throwaway postgres:16
carrying the same stock settings as the dev db (work_mem 4MB, shared_buffers
128MB, random_page_cost 4; both 16.15) loaded with synthetic rows matched to
#65's distribution (46,501 rows/day over ~23k distinct product/store pairs),
`matching.load_catalog()`'s full-view read:

    window    rows         no index   indexed   table+index
    1 day     46,501       182 ms     24 ms     7 MB   <- one scrape, i.e. today
    30 days   1,395,030    2,459 ms   976 ms    270 MB
    90 days   4,185,090    7,166 ms   3,129 ms  810 MB

Each cell is one EXPLAIN ANALYZE straight after ANALYZE, so before and after
are on the same basis; warm repeats agreed (19 ms at one day, 928-934 ms at
thirty). The unindexed column is the view's Sort spilling to disk - 4,200 kB of
temp file at one day, 126 MB per parallel worker at ninety - and the same query
on the dev db's real 46,501 rows before this change was 99 ms with the same
external merge sort.

That read is O(every historical row) however good the index is: DISTINCT ON
must visit all of them to pick the newest per (product, store). So the window
*is* the latency budget for /list and /list/where-to-buy, the app's payoff
pages, and 30 days is where a month of daily snapshots - enough to answer "is
this more expensive than last month?" - costs about a second instead of three.
A longer window buys insight nothing consumes yet: no page reads
`grocery_prices` history today, every one reads `grocery_prices_latest`. Raising
the setting starts keeping more from that point on; it cannot bring back rows an
earlier run already deleted.

Rejected alternatives, both suggested in #65: aggregating old rows into a
summary table, and keeping only the latest row per product/store. Each needs a
new table plus a migration, and each would be schema for the imagined feature
above. A plain window needs no new table, keeps the raw rows that do exist
usable, and is one env var to change. If long-horizon trends ever get a real
consumer, the aggregate table is the right next step and this setting can go
back up when it exists.

Scope guard worth stating out loud, since this is the only code in the repo that
deletes anything: it deletes from `grocery_prices` and nowhere else. Prices are
re-scrapable. Recipes, `meal_plan_slots` (which is what `/history` reads),
pantry and staples are not, and no future cleanup job should conclude from this
one that they are disposable.

This lives in its own module rather than in collector.py because collector.py
imports pandas at module load and pulls playwright in through aldis/tops ->
instacart_storefront - none of which the required test tier installs
(CONTRIBUTING §7, §11). The piece of this change whose failure mode is
*deleting history the operator asked to keep* is stdlib-only here, so it can
actually be regression-tested.
"""

import os

# Documented in README's `.env` block, read the same way collector.py reads its
# other config (os.getenv at call time, so a test can monkeypatch the env).
RETENTION_DAYS_ENV = "PRICE_HISTORY_RETENTION_DAYS"

# See the measurement table above for what this costs and why it isn't larger.
DEFAULT_RETENTION_DAYS = 30


def retention_days(raw):
    """Interpret a configured retention window.

    Returns the number of days to keep, a non-positive number to mean "keep
    everything", or None when the value can't be interpreted at all.

    None is the load-bearing case. An unparseable setting - a typo like
    `90days`, a stray quote from a hand-edited .env - must NOT fall back to
    DEFAULT_RETENTION_DAYS, because the default deletes. Guessing there would
    trade a config typo for silent, irreversible loss of price history, which
    is the same "no answer beats a wrong answer" rule the rest of the repo
    follows (matching.py's `_usda_grams_per_unit`, collector.py's unmatched
    list items). Callers treat None as "issue no DELETE at all".
    """
    if raw is None or not raw.strip():
        return DEFAULT_RETENTION_DAYS
    try:
        return int(raw.strip())
    except ValueError:
        return None


def prune(cur):
    """Delete `grocery_prices` rows older than the retention window.

    Returns the number of rows deleted (0 when retention is off, when the
    window is unparseable, or when nothing was old enough yet). Runs on the
    caller's cursor inside the caller's transaction - collector.py gives it a
    transaction of its own after the scrape's has committed, so a prune that
    fails can't cost the run its prices (reasoning at that call site).

    Two properties this has to keep:

    * The cutoff comes from `max(datetime)` in the table, not from a clock.
      collector.py writes `datetime` as `pd.Timestamp.now()` - a naive local
      time in the *scraper* container - while `now()::timestamp` is rendered in
      the *Postgres* container's TimeZone. Those two need not agree (different
      TZ, host clock drift), and a disagreement would silently shave hours off
      or add hours to every run. Anchoring to the newest row we actually have
      makes the window a statement about the data rather than about two clocks,
      and it also means a pipeline that has been down for a month keeps its
      last window of history instead of being pruned down to nothing.
    * It never deletes when the cutoff can't be determined. `max(datetime)`
      over an empty table (or one where every `datetime` is NULL) is NULL, the
      arithmetic stays NULL, and `datetime < NULL` is never true - so the
      statement is a no-op instead of an unbounded DELETE. An unparseable
      window doesn't reach SQL at all.

    Deliberately not covered here: an index on `datetime` to speed this DELETE
    up. Measured on 4.19M rows, a steady-state day's prune (~46.5k rows, since
    the window slides forward one day per scrape) costs 351 ms and the one-time
    catch-up that deletes 2.79M rows costs 2.1 s - two seq scans, one for
    max(datetime) and one for the filter - once a day at the end of a scrape
    that already takes 20+ minutes. A second index would tax every one of those
    ~46k daily inserts to save a fraction of a second a day.

    What the first run looks like on a table that has never been pruned
    (measured): the catch-up that deleted 2.79M of 4.19M rows took 2.1 s, and
    the heap was back to its live-row size once autovacuum ran behind it
    (174 MB for 1.35M rows). The index file does not shrink - btree vacuum
    marks those pages reusable rather than truncating them, because the oldest
    rows are scattered through an index ordered by product/store - so it keeps
    its high-water mark (288 MB measured) and later inserts reuse the space
    instead of growing it. Not a leak, and not worth a REINDEX here.
    """
    raw = os.getenv(RETENTION_DAYS_ENV)
    days = retention_days(raw)
    if days is None:
        print(f"[retention] {RETENTION_DAYS_ENV}={raw!r} is not a whole number of days - deleting nothing. "
              "An unparseable window must not fall back to the default, because the default deletes.")
        return 0
    if days <= 0:
        print(f"[retention] disabled ({RETENTION_DAYS_ENV}={days}) - keeping grocery_prices history unbounded.")
        return 0

    cur.execute(
        """
        DELETE FROM grocery_prices
        WHERE datetime < (
            SELECT max(datetime) - make_interval(days => %s)
            FROM grocery_prices
        )
        """,
        (days,),
    )
    deleted = cur.rowcount
    print(f"[retention] deleted {deleted} grocery_prices row(s) older than {days} days before the newest row.")
    return deleted
