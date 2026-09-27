FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Explicit chmod rather than relying on the bit surviving git and the build
# context - a non-executable entrypoint fails at container start, which is a
# confusing way to discover it.
RUN chmod 0755 /app/docker-entrypoint.sh

# docker-entrypoint.sh runs init_schema.py once and then `exec gunicorn ...`.
# Both halves matter and the reasoning for each lives in that script, beside the
# flags: schema creation has to happen before any worker exists (two workers
# racing CREATE TABLE IF NOT EXISTS against an empty database is not safe in
# Postgres - #82), and `exec` is what lets Docker's SIGTERM reach gunicorn
# directly for a graceful shutdown instead of being SIGKILLed when a shell's
# grace period expires (#61).
#
# `python app.py` still works for local development without Docker and still uses
# Flask's built-in server; it does not create the schema first, so on an empty
# database the routes that provision nothing will fail until init_schema.py has
# been run once. That path is for iterating on templates, not for running the
# household's instance.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
