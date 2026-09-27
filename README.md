# GroceryCostComparison

Scrapes grocery prices from a handful of stores into Postgres, and a small self-hosted webapp on top: recipes, weekly meal planning (breakfast/lunch/dinner), a pantry that tracks itself, a persistent grocery list, and "where should I buy this" cost comparison. See the [GitHub issues](https://github.com/jfitzpatrick6/GroceryCostComparison/issues) for the full roadmap; issues are the source of truth for what's done/in progress/planned, not this file.

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
USDA_API_KEY=
```

- `TOPS_STORE` / `ALDIS_STORE` can be left blank for now - they're not used yet (see the table above and #43).
- `BJS_STORE` / `WALMARTSTORE` need real store ids for those chains. BJs' id shows up in that chain's own site network requests; Walmart's scraper doesn't currently work regardless of what's set here (see above).
- `USDA_API_KEY` is for the webapp's recipe-ingredient unit conversion ("2 cups flour" -> a purchase-unit estimate) - get a free key at https://fdc.nal.usda.gov/api-key-signup. Optional; that one feature just won't produce estimates without it.

**Do not commit a filled-in `.env`.** Store ids are location-identifying. `.env` is already gitignored - keep it that way.

## Running a scrape

```
cd Groceries
docker compose up --build db grocery_scraper
```

This starts a Postgres container and the scraper container. The scraper runs once and exits; Postgres keeps running. Re-run `docker compose up grocery_scraper` any time you want a manual one-off scrape.

A full run currently takes a while - Tops alone is on the order of 20 minutes (it walks ~170 category pages). Aldi is much faster (a few minutes, smaller catalog). This is expected, not a bug.

To keep prices fresh automatically instead of remembering to run this by hand, start `scraper_scheduler` instead (same image, same `.env`, no separate setup) - it runs the same scrape once a day at 3am:

```
docker compose up -d --build db scraper_scheduler
```

It's a long-running container (`restart: unless-stopped`), unlike `grocery_scraper`'s one-shot behavior - the two don't conflict and can both exist, `scraper_scheduler` is just the hands-off way to get the same result. To change the schedule, edit `Groceries/scraper-cron` and rebuild.

## Running the webapp

```
cd Groceries
docker compose up --build db webapp
```

This is a small Flask app (`Groceries/webapp/`):

| Page | What it does |
|---|---|
| `/prices` | Searchable table of the latest scraped price per product/store |
| `/staples` | Pin products you buy regularly, compare their price across stores at a glance |
| `/recipes` | Save recipes with free-text ingredient lines ("2 cups flour") - paste a whole recipe (title/ingredients/instructions all together) to pre-fill the form instead of typing it in by hand |
| `/planner` | Weekly grid, tabbed by meal (breakfast/lunch/dinner). A day's meal can hold several recipes (e.g. a burger patty recipe + a bun recipe) plus loose ad-hoc ingredients that don't need a whole recipe of their own (e.g. taco night's shredded cheese). Serving counts are adjustable per night without touching the recipe itself. "Ingredients needed this week" merges the whole week into one list, with a best-effort purchase-unit estimate (USDA-backed) alongside each cooking-unit amount |
| `/pantry` | What's on hand - manual add/update, auto-restocks when you check off a grocery-list purchase, auto-depletes when a planned meal is marked cooked (with an editable "Used tonight" correction, since real cooking never matches a recipe exactly). An optional low-stock threshold per item surfaces a "running low" suggestion on the grocery list |
| `/list` | Persistent grocery list - add ad-hoc or merge a planned week's ingredients in one action, check items off as you shop |
| `/list/where-to-buy` | The payoff feature: cheapest store per list item (accounting for needing to buy whole packages, not just the lowest $/unit), plus a one-store-vs-split-across-stores total comparison |
| `/history` | What's actually been cooked, with a one-click "make this again" |
| `/profiles` | Lightweight named household profiles (no password) - attributes who added/checked/cooked what |
| `/healthz` | JSON health probe for Docker's healthcheck - `200 {"status":"ok"}` when the database answers, `503 {"status":"unhealthy"}` when it doesn't. Not a page; nothing links to it |

More pages/features land as the corresponding GitHub issues get done.

It listens on `0.0.0.0:5000` - reachable over your LAN, and over [Tailscale](https://tailscale.com/) too if the host machine itself is already joined to your tailnet (no sidecar container or `TS_AUTHKEY` needed - the host's own tailscale0 interface covers it). Not meant for the public internet; put it behind your own reverse proxy/auth if you need that. An earlier version of this ran Tailscale as a Docker sidecar with its own tailnet identity, but that added a real failure mode (an unauthenticated sidecar loops on auth retries and restarts, dropping the webapp's network namespace each time it shared one) for no benefit on a host that's already on the tailnet - removed in favor of the simpler setup above.

The container serves through **gunicorn** (2 workers x 4 threads), not Flask's development server - see `Groceries/webapp/Dockerfile` for the flags and the reasoning behind each. For local development without Docker, `python app.py` still works and still uses Flask's built-in server; that path is for iterating on templates, not for running the household's instance.

On startup the container runs `init_schema.py` once, before gunicorn serves anything, to create the tables the webapp owns (#82). It retries while Postgres is still coming up, and exits loudly if schema creation fails, so a container that is up is a container whose schema exists. It's idempotent - safe to re-run by hand against an existing database:

```
docker compose exec webapp python init_schema.py
```

The tables the *scraper* owns (`grocery_prices` and its `grocery_prices_latest` view) are created by `collector.py` on the first scrape, not here; pages that need them degrade gracefully until then.

Both `db` and `webapp` have Docker **healthchecks**, and dependent services wait on `db` with `condition: service_healthy` rather than merely for its container to start (#71). `docker compose up` therefore blocks until Postgres is actually accepting connections, and `docker ps` shows a webapp that is hung-but-alive as `unhealthy` instead of looking fine — which `restart: unless-stopped` alone cannot detect, since it only acts when a container exits. The webapp also runs as a **non-root** user (uid 10001); the scraper image still runs as root because its cron job and Playwright install assume it.

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

## Working on this

Everything above is about *using* the app. If you're changing it - human or agent - read **[CONTRIBUTING.md](CONTRIBUTING.md)** first. It's the working agreement: GitHub issues as the source of truth, one branch per issue, PRs with CI green and a second-opinion review, the commit-message convention, the code and testing conventions this repo follows, and the specific lint exemptions (each with its reason). `AGENTS.md` is the short version for AI agents picking the repo up cold.

Lint and tests:

```
.venv/bin/ruff check .
.venv/bin/pytest
```

Both run on every push and PR via [`.github/workflows/ci.yml`](.github/workflows/ci.yml). See CONTRIBUTING.md §11 for setting up `.venv` - the host Python version may not match the containers', which affects one pinned dependency.
