# GroceryCostComparison

Scrapes grocery prices from a handful of stores into Postgres so they can be compared. Currently just the data layer - the eventual goal is a small self-hosted webapp (recipes, weekly meal planning, "where should I buy this") built on top of this data. See the [GitHub issues](https://github.com/jfitzpatrick6/GroceryCostComparison/issues) for the full roadmap; issues are the source of truth for what's done/in progress/planned, not this file.

## Current status per store

| Store | Status |
|---|---|
| Tops | Working. Scrapes via a real headless browser against Instacart's white-label platform (Tops migrated off its old standalone API - see #41). |
| Aldi | Working, same mechanism as Tops (also migrated to Instacart's platform). |
| BJs | Working, direct API call, no browser needed. |
| Walmart | **Not working.** Walmart actively challenges scraping attempts with a bot-verification wall, even from a real browser session. Not being circumvented - see #12. |

Only Tops and Aldi target a *specific* store right now via whatever this machine's network location IP-geolocates to (accurate for a home server on a residential connection; not configurable to an arbitrary store yet - see #43). BJs and Walmart take a `store` id directly.

## Prerequisites

- Docker and Docker Compose

Everything else (Python, headless Chromium, all dependencies) is installed inside the container - you don't need Python locally to run a scrape.

## Setup

Create a `.env` file at the repo root (same level as this file, one directory above `Groceries/`):

```
TOPS_STORE=
ALDIS_STORE=
BJS_STORE=1234
WALMARTSTORE=1234
TS_AUTHKEY=
```

- `TOPS_STORE` / `ALDIS_STORE` can be left blank for now - they're not used yet (see the table above and #43).
- `BJS_STORE` / `WALMARTSTORE` need real store ids for those chains. BJs' id shows up in that chain's own site network requests; Walmart's scraper doesn't currently work regardless of what's set here (see above).
- `TS_AUTHKEY` is for the webapp's Tailscale sidecar (see below) - generate one at https://login.tailscale.com/admin/settings/keys. Can be left blank if you're not running the webapp yet.

**Do not commit a filled-in `.env`.** Store ids are location-identifying, and an exposed `TS_AUTHKEY` could let someone else join your tailnet. `.env` is already gitignored - keep it that way.

## Running a scrape

```
cd Groceries
docker compose up --build db grocery_scraper
```

This starts a Postgres container and the scraper container. The scraper runs once and exits; Postgres keeps running. There's no scheduled/recurring run yet (see #33) - re-run `docker compose up grocery_scraper` manually for now.

A full run currently takes a while - Tops alone is on the order of 20 minutes (it walks ~170 category pages). Aldi is much faster (a few minutes, smaller catalog). This is expected, not a bug.

## Running the webapp

```
cd Groceries
docker compose up --build db tailscale webapp
```

This is a small Flask app (`Groceries/webapp/`) with one real page so far: `/prices`, a searchable table of the latest scraped price per product/store. More pages land as the corresponding GitHub issues get done.

It's meant to be reachable only over [Tailscale](https://tailscale.com/), not the public internet or LAN - the `tailscale` service is a sidecar container the webapp shares its network namespace with, so it shows up on your tailnet once `TS_AUTHKEY` is set and it authenticates. **This part is configured but not verified end-to-end** - it was built and tested without a real Tailscale account available, so confirm the container actually appears in your tailnet (`https://login.tailscale.com/admin/machines`) rather than assuming it works. Without a valid `TS_AUTHKEY`, the sidecar will sit in an auth-retry loop and periodically restart, which breaks the webapp's networking each time (`docker compose restart webapp` recovers it, but a real auth key is the actual fix).

## What gets written

Everything lands in a single `grocery_prices` table in the `grocery_db` Postgres database (default credentials are in `Groceries/docker-compose.yml` - fine for a local/home-server Postgres instance not exposed elsewhere, change them if that's not your situation):

| Column | Meaning |
|---|---|
| `product` | Product name as listed by the store |
| `price` | Price at time of scrape (numeric) |
| `rate` | Display-string unit price (e.g. `$3.99 per lb`) where a size could be parsed |
| `size` | Raw size/quantity string from the store |
| `store` | Which chain (`Tops`, `Aldis`, `BJs`, `Walmart`) |
| `store_id` | The store id used for that scrape, where applicable |
| `datetime` | When the row was scraped |
| `unit_price` | Same as `rate` but numeric, for comparing/sorting |
| `unit` | Unit `unit_price` is in: `lb`, `gal`, `each`, or `ft` |

Every scrape appends new rows rather than overwriting, so there's a real history in the table. `grocery_prices_latest` is a view with just the most recent row per product/store - query that instead of the raw table unless you actually want history.

## Repo layout

- `Groceries/` - the scrapers, shared unit-conversion helper, and Docker setup; everything above applies to what's in here
- `Groceries/webapp/` - the Flask app, its own (much lighter) Dockerfile
- `Old Report/`, `Testing Attempts/` - earlier experiments, not part of the running pipeline, kept for reference
