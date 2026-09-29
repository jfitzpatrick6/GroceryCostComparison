#!/bin/sh
# Container entrypoint: create the app's schema once, then hand off to gunicorn.
#
# Two steps, and the order matters. init_schema.py must complete before any
# worker exists, because two workers racing CREATE TABLE IF NOT EXISTS against an
# empty database is not safe in Postgres (see #82 and the comment in
# init_schema.py). `set -e` means a failed schema init aborts startup rather than
# letting gunicorn come up and serve 500s on every page.
#
# `exec` replaces this shell with gunicorn so it becomes the process Docker
# signals directly. Without it, SIGTERM on `docker compose stop`/`restart` goes
# to the shell, which does not forward it, and gunicorn is SIGKILLed after the
# grace period instead of shutting its workers down gracefully.
set -e

python init_schema.py

# SECRET_KEY signs sessions and CSRF tokens (#69). When .env doesn't set one,
# generate a random key HERE, before gunicorn forks, so both workers share it -
# a per-worker key would reject every other request's token. It changes on
# every container start, which resets sessions; set SECRET_KEY in .env to keep
# them. Never a constant: a token signed with a published key is not a token.
if [ -z "${SECRET_KEY:-}" ]; then
    SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
    export SECRET_KEY
    echo "SECRET_KEY not set - using a random key for this container run (sessions reset on restart; set SECRET_KEY in .env to keep them)"
fi

# Gunicorn rather than `python app.py` (#61), which ran Werkzeug's development
# server - its own banner said "Do not use it in a production deployment".
#
#   --workers 2 --threads 4   8 concurrent requests. Far more than a household
#                             needs; the point is that one blocked request no
#                             longer blocks everyone else.
#   --timeout 120             Guards the *worker's main loop*. With the gthread
#                             worker this does NOT kill an individual request
#                             thread hung on a slow upstream - the main loop
#                             keeps heartbeating while threads block. Bounding
#                             that is #64's job (moving the synchronous USDA
#                             lookups off the request path), not a timeout's.
#   --max-requests (+jitter)  Recycles workers so a slow leak can't accumulate
#                             forever. Jitter stops both workers recycling at
#                             the same instant.
#   --*-logfile -             stdout/stderr, so `docker logs` keeps working.
exec gunicorn \
    --bind 0.0.0.0:5000 \
    --workers 2 \
    --threads 4 \
    --timeout 120 \
    --max-requests 500 \
    --max-requests-jitter 50 \
    --access-logfile - \
    --error-logfile - \
    app:app
