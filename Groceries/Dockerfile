# Use official Python image. Pinned to a Debian 12 (bookworm) base -
# Playwright 1.62 (see requirements.txt) doesn't support Debian 11, which
# python:3.10.7 was built on.
FROM python:3.10-bookworm

# Set working directory
WORKDIR /app

RUN pip install --upgrade pip
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Tops/Aldi scraping (instacart_storefront.py) needs a real headless browser -
# see #41/#42. This adds real build time and image size; only Tops/Aldi need it.
RUN playwright install --with-deps chromium

# For the scraper_scheduler service (#33) - not used by the default
# one-shot `grocery_scraper` service, same image either way.
RUN apt-get update && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN chmod 0644 /app/scraper-cron && cp /app/scraper-cron /etc/cron.d/scraper-cron \
    && chmod 0755 /app/scheduler_entrypoint.sh

# Runs as ROOT, deliberately - unlike the webapp image, which is non-root (#71).
# Two things here assume it, both verified rather than assumed:
#   - `playwright install` above put Chromium under /root/.cache/ms-playwright,
#     and /root is drwx------ so a non-root user cannot read the browser it needs.
#   - the scheduler's cron job installs into /etc/cron.d, and scraper-cron uses
#     the `root` user field.
# Making this image non-root is therefore not a two-line change: it needs the
# Playwright cache relocated (PLAYWRIGHT_BROWSERS_PATH to a world-readable path)
# and cron replaced with something unprivileged - the honest shape is dropping
# cron for a sleep loop in the entrypoint, which would also remove the
# `.env.runtime` quoting fragility that scheduler_entrypoint.sh's own comment
# flags. Left as a separate change with its own verification.

# Unbuffered, so scrape progress and the final "Run summary" line reach
# `docker logs` as they happen - buffered output arrives in delayed chunks and
# is lost outright if a stop has to SIGKILL the process (#63).
ENV PYTHONUNBUFFERED=1

# Run the script
CMD ["python", "collector.py"]
