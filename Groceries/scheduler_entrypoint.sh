#!/bin/sh
# Cron jobs run with a minimal environment and don't inherit the container's
# runtime env vars (DB_HOST, BJS_STORE, etc, set via docker-compose env_file/
# environment) - dump them to a file the cron job sources before running
# collector.py (see scraper-cron).
#
# `export -p`, not a printenv loop (#63). The loop wrapped each value in double
# quotes, so any value containing a quote, `$`, backtick or backslash produced
# a file that fails to source - verified in this image: the old loop on
# NASTY='a'"'"'b"c $HOME `id` \' made `.` abort with "export: /root: bad
# variable name", i.e. every scheduled scrape would have died before starting.
# dash's `export -p` emits shell-quoted assignments that round-trip that same
# value exactly. Owner-only because it holds USDA_API_KEY and the store ids.
export -p > /app/.env.runtime
chmod 600 /app/.env.runtime

# Catch-up scrape (#63): cron never re-runs a missed 03:00, so a host that was
# off or rebooting then skipped a day, and a fresh deploy had no prices until
# the next night. collector.py --if-stale scrapes only when the newest prices
# are over 26h old. Same lock as the cron job, so this and a 03:00 run can
# never scrape concurrently. Backgrounded so cron starts on time regardless.
(cd /app && flock -n /tmp/scrape.lock python collector.py --if-stale) &

exec cron -f
