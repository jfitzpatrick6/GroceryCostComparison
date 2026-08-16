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

COPY . .

# Run the script
CMD ["python", "collector.py"]
