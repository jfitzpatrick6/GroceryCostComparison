FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Gunicorn rather than `python app.py` (#61). The old CMD ran Werkzeug's
# development server - its own startup warning said "Do not use it in a
# production deployment" - which is a single process with no worker
# supervision, no recycling and no graceful restart, so one slow request could
# stall the whole household.
#
# Exec form (JSON array), not a shell string: gunicorn must receive SIGTERM
# directly from Docker to shut workers down gracefully on `compose restart`
# instead of being SIGKILLed when the shell's grace period expires.
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
#   --max-requests (+jitter)  Recycles workers periodically so any slow leak
#                             can't accumulate forever. Jitter stops both
#                             workers recycling at the same instant.
#   --*-logfile -             stdout/stderr, so `docker logs` keeps working.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "2", \
     "--threads", "4", \
     "--timeout", "120", \
     "--max-requests", "500", \
     "--max-requests-jitter", "50", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
