#!/bin/sh
# Cron jobs run with a minimal environment and don't inherit the container's
# runtime env vars (DB_HOST, BJS_STORE, etc, set via docker-compose env_file/
# environment) - dump them to a file the cron job sources before running
# collector.py (see scraper-cron). Good enough for this app's simple
# alphanumeric-ish values; not bulletproof against values containing quotes.
printenv | while IFS='=' read -r key value; do
    echo "export $key=\"$value\""
done > /app/.env.runtime

cron -f
