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

# Run the script
CMD ["python", "collector.py"]
