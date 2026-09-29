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
PRICE_HISTORY_RETENTION_DAYS=
```

- `TOPS_STORE` / `ALDIS_STORE` can be left blank for now - they're not used yet (see the table above and #43).
- `BJS_STORE` / `WALMARTSTORE` need real store ids for those chains. BJs' id shows up in that chain's own site network requests; Walmart's scraper doesn't currently work regardless of what's set here (see above).
- `USDA_API_KEY` is for the webapp's recipe-ingredient unit conversion ("2 cups flour" -> a purchase-unit estimate) - get a free key at https://fdc.nal.usda.gov/api-key-signup. Optional; that one feature just won't produce estimates without it.
- `PRICE_HISTORY_RETENTION_DAYS` is optional: how many days of raw price history each scrape keeps, blank for the default of 30. Set it to `0` to keep everything and let the table grow without bound - see [What gets written](#what-gets-written) for what that costs. A value that isn't a whole number of days (`90days`) prunes nothing for that run and says so in the scrape log, rather than falling back to the default and deleting history nobody meant to lose.

**Do not commit a filled-in `.env`.** Store ids are location-identifying. `.env` is already gitignored - keep it that way.

## Running a scrape

```
cd Groceries
docker compose up --build db grocery_scraper
```

This starts a Postgres container and the scraper container. The scraper runs once and exits; Postgres keeps running. Re-run `docker compose up grocery_scraper` any time you want a manual one-off scrape.

A full run currently takes a while - Tops alone is on the order of 20 minutes (it walks ~170 category pages). Aldi is much faster (a few minutes, smaller catalog). This is expected, not a bug.

The scraper logs a count per store as it goes, so a run that quietly fetched less than the store actually has is visible rather than silent (#96). BJs' line reports what it parsed, what it skipped and why, and what the API itself says the catalogue holds:

```
[BJs] parsed 3121 of 3135 products fetched; skipped 14 (ValueError: weighted item missing minpackweight: 2, no_store_price: 12); API declares 3135 in this catalogue
```

`no_store_price` means BJs lists the product online-only, with a ship-to-home price and no club price. Those are skipped on purpose rather than priced from the `online` value: `/list/where-to-buy` compares what it costs to walk into a store, and on real captured products the two differ by as much as 30%. A `WARNING` line naming both numbers is printed if the walk ends before the API's declared total - that means the run is incomplete and its prices will bias the comparison against that store.

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

It listens on `0.0.0.0:5000` inside the container, published to the host on `WEBAPP_PORT` - default 5000, set in `Groceries/.env` (see `Groceries/.env.example`). Note that is a *different* file from the repo-root `.env` and the two are not interchangeable: the root one is passed into containers via `env_file`, while `Groceries/.env` is what compose interpolates `${WEBAPP_PORT}` from. Change the port when something else already holds 5000, which on a shared home server is the normal case rather than an exception - on the deployment host it's the camera NVR. Reachable over your LAN, and over [Tailscale](https://tailscale.com/) too if the host machine itself is already joined to your tailnet (no sidecar container or `TS_AUTHKEY` needed - the host's own tailscale0 interface covers it). Not meant for the public internet; put it behind your own reverse proxy/auth if you need that. An earlier version of this ran Tailscale as a Docker sidecar with its own tailnet identity, but that added a real failure mode (an unauthenticated sidecar loops on auth retries and restarts, dropping the webapp's network namespace each time it shared one) for no benefit on a host that's already on the tailnet - removed in favor of the simpler setup above.

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

Every scrape appends new rows rather than overwriting, so the table holds a rolling window of real history rather than growing forever (#65). At the end of each scrape the collector deletes rows older than `PRICE_HISTORY_RETENTION_DAYS` (default 30), measured back from the newest row in the table - so a pipeline that has been down for a month keeps its last window of history instead of being pruned to nothing, and a run that can't work the cutoff out safely deletes nothing at all. One full snapshot is ~23,000 rows, which is why the window exists: unbounded, the daily schedule reaches ~8M rows / ~1.5 GB in a year. It's 30 days rather than 90 because reading the latest price per product/store means walking every historical row however well it's indexed - measured on synthetic data matched to the real distribution, the full catalog read that `/list` and `/list/where-to-buy` make takes ~1s at 1.4M rows and ~3s at 4.2M rows. Raise the setting if you want a longer history and can spend the latency; rows an earlier run already deleted don't come back.

> **Those row counts used to be quoted at roughly double their real size, and the
> error propagated into this paragraph (#96).** The `grocery_prices` table on the
> dev host held 46,501 rows, which was read as "one snapshot is ~46,500 rows" -
> but it actually held *three* runs from the same afternoon, two of them
> single-store re-runs during development. One real snapshot was 24,260 rows.
> Since `grocery_prices` appends, **a row count over a store, or over a day,
> sums every run in the window**; to size one scrape, count one `datetime`
> value:
>
> ```sql
> SELECT store, datetime, count(*) FROM grocery_prices
> GROUP BY store, datetime ORDER BY datetime DESC;
> ```
>
> Reading it the other way is what made a perfectly complete scrape look like it
> had lost half its catalogue. The latency figures above were measured at the row
> counts stated and are not wrong *at those counts*, but a real deployment now
> reaches about half of them, so treat ~1s / ~3s as an upper bound. They were not
> re-measured as part of #96.

Retention applies to `grocery_prices` and nothing else. Prices are re-scrapable - recipes, what's been cooked (`/history`), the pantry and pinned staples are not, and no cleanup job here touches them.

`grocery_prices_latest` is a view with just the most recent row per product/store - query that instead of the raw table unless you actually want history. The scraper maintains an index on `grocery_prices (product, store, datetime DESC)` for it; without one, every read of the view scans the whole table and spills an external merge sort to disk (it already does at a month of retained history).

## Backups and restore

The `db_backup` service runs `pg_dump` every night at **03:30 UTC** - deliberately after the 03:00 scrape - and writes gzip'd dumps to `backups/` at the repo root. That is a **host bind mount, outside the `db_data` volume**, because a backup stored inside the thing it backs up is not a backup. It survives `docker compose down -v`.

Dumps older than `BACKUP_KEEP_DAYS` (default 14) are pruned automatically. Each attempt logs a timestamped `OK`/`FAILED` line, so check whether backups are actually happening with:

```
docker compose logs --tail 20 db_backup
```

A backup nobody has restored is a hypothesis, not a backup. **To restore over an existing database:**

```
# 1. stop the app so nothing writes mid-restore
docker compose stop webapp scraper_scheduler

# 2. clear the old schema. REQUIRED, not optional: the dump is taken without
#    --clean, so it contains CREATE statements and no DROPs. Restoring over
#    existing tables aborts on the first object ("relation ... already exists"),
#    and -v ON_ERROR_STOP=1 below makes that abort the whole restore.
docker compose exec -T db psql -U user -d grocery_db \
  -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'

# 3. load the dump
gunzip -c backups/grocery_db-YYYYMMDD-HHMMSS.sql.gz \
  | docker compose exec -T db psql -U user -d grocery_db -v ON_ERROR_STOP=1

# 4. bring the app back
docker compose start webapp scraper_scheduler
```

Keep `ON_ERROR_STOP=1`. Without it a failure partway through leaves a *silently* partial restore, which is much worse than a loud one — you'd discover it weeks later as missing recipes rather than now as an error message.

Step 2 is destructive, which is the point of a restore, but check you have the dump you think you have before running it (`gunzip -c <file> | grep -c "^COPY"` should be non-zero).

To restore into a **fresh** volume instead — a new host, or after losing `db_data` — start `db` alone (`docker compose up -d db`), wait for it to report healthy, and run only step 3. There is no schema to drop, and `init_schema.py` will not have run yet, so the dump supplies everything.

**What is and isn't in a dump.** By default `BACKUP_INCLUDE_PRICES=false`, which excludes the *rows* of `grocery_prices` while keeping its schema and the `grocery_prices_latest` view. That keeps dumps in the kilobytes instead of the gigabytes (~17M price rows/year) and is safe because prices are the one thing here the scraper can regenerate. The tradeoff is real, though: **restoring loses price history**, so anything depending on past prices starts again from the next scrape. Recipes, ingredients, meal plans, cook history, pantry, grocery list and profiles are always included - those cannot be regenerated. Set `BACKUP_INCLUDE_PRICES=true` in the compose file if you'd rather have complete dumps; every log line states which mode ran.

Verified end to end, not assumed: seeded a recipe (with an apostrophe in its name), ingredients in order, pantry, list, profile, a cooked meal-plan slot and a `cook_depletions` row, plus 5,000 price rows; dumped; restored into a **fresh** `postgres:16`; and confirmed every household row came back byte-identical, `grocery_prices` came back as an empty table with its view intact, and pruning removed a 30-day-old dump while leaving a 2-day-old one and an unrelated file alone.

## Repo layout

- `Groceries/` - the scrapers, shared unit-conversion helper, and Docker setup; everything above applies to what's in here
- `Groceries/webapp/` - the Flask app, its own (much lighter) Dockerfile
- `Old Report/`, `Testing Attempts/` - earlier experiments, not part of the running pipeline, kept for reference

**Putting this on a real host?** Read **[DEPLOYMENT.md](DEPLOYMENT.md)** - the step-by-step for a first deployment: what `.env` needs (and which variables are actually required), what to look for in the logs, how to check every page against a genuinely fresh database, and how to confirm the backups really restore.

## Working on this

Everything above is about *using* the app. If you're changing it - human or agent - read **[CONTRIBUTING.md](CONTRIBUTING.md)** first. It's the working agreement: GitHub issues as the source of truth, one branch per issue, PRs with CI green and a second-opinion review, the commit-message convention, the code and testing conventions this repo follows, and the specific lint exemptions (each with its reason). `AGENTS.md` is the short version for AI agents picking the repo up cold.

Lint and tests:

```
.venv/bin/ruff check .
.venv/bin/pytest
```

Both run on every push and PR via [`.github/workflows/ci.yml`](.github/workflows/ci.yml). See CONTRIBUTING.md §11 for setting up `.venv` - the host Python version may not match the containers', which affects one pinned dependency.
