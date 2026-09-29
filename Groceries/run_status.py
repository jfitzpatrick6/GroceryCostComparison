"""
Scrape-run bookkeeping for collector.py (#63): the one-line run verdict, and the
"are prices stale enough to scrape now?" decision the scheduler makes at start.

Separate from collector.py for the same reason retention.py is: collector.py
imports pandas and every scraper (playwright included), none of which the
required test tier installs (CONTRIBUTING §7, §11). Both functions here decide
something an operator relies on - whether a run counts as working, and whether
a scrape fires - so they have to be testable there.
"""

from datetime import datetime, timedelta

# Stores that are expected to fail every run and must not turn a working run
# into a PARTIAL one. Walmart is behind a bot-verification wall that is
# deliberately not circumvented (#12); DEPLOYMENT.md already tells the operator
# to expect its failure line. If PARTIAL fired every night it would be noise
# that trains everyone to ignore it, which defeats the point of the verdict.
EXPECTED_FAILURES = {"Walmart"}

# The scheduler scrapes at 03:00 daily. 26h rather than 24h so a run that
# finished at 03:25 yesterday is not "stale" at 03:10 today, before tonight's
# run has had a chance to finish - two hours is the slack for one full scrape.
STALE_AFTER = timedelta(hours=26)

# Outcome marker for a store not attempted this run (collector passes it as the
# error). Distinct from a failure: nothing was tried, so nothing failed (#132).
SKIPPED = "skipped"


def run_summary(outcomes):
    """One log line for a whole run, and its verdict.

    `outcomes` is a list of (store, item_count_or_None, error_or_None) in scrape
    order. Returns (verdict, line) where verdict is "OK", "PARTIAL" or "FAILED".

    Before this, a run's result had to be reconstructed from per-store lines
    scattered through twenty minutes of output (#63). A grep for "Run summary"
    now answers "did last night work?" in one line.
    """
    parts = []
    unexpected_failures = 0
    succeeded = 0
    for store, count, error in outcomes:
        if error is SKIPPED:
            parts.append(f"{store} skipped")
            continue
        # Zero items with no exception is a failure, not success (#132): a real
        # run printed "Walmart ok (0 items)" under an OK verdict after 28
        # minutes of producing nothing, and a Tops or BJs that silently came
        # back empty would have been reported the same way.
        if error is None and not count:
            error = "returned 0 items"
        if error is None:
            succeeded += 1
            parts.append(f"{store} ok ({count} items)")
            continue
        expected = store in EXPECTED_FAILURES
        if not expected:
            unexpected_failures += 1
        # Truncated: an exception message can be a whole HTML error page, and
        # this line is meant to be read at a glance.
        parts.append(f"{store} FAILED{' (expected, #12)' if expected else ''}: {str(error)[:120]}")

    if succeeded == 0:
        verdict = "FAILED"
    elif unexpected_failures:
        verdict = "PARTIAL"
    else:
        verdict = "OK"
    return verdict, f"Run summary: {verdict} - " + "; ".join(parts)


def is_stale(newest, now=None):
    """True when the newest price row is old enough that a scrape should run now.

    `newest` is max(grocery_prices.datetime), or None when the table is empty or
    missing - which is stale by definition: a fresh deploy should not show an
    empty app until 03:00 tomorrow. Timestamps are naive local time, matching
    what collector.py writes (pd.Timestamp.now()).
    """
    if newest is None:
        return True
    now = now or datetime.now()
    return now - newest > STALE_AFTER
