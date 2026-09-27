# Deploying to gigabyte

Written 2026-09-27 for a first deployment of this stack to `gigabyte`
(`100.99.60.84` / `gigabyte.taild1e879.ts.net`), a Linux host on the
`taild1e879` tailnet. Every command below was either run during this project's
verification or is standard git/docker usage; where a step has a non-obvious
failure mode it says so.

The household reaches this app over **Tailscale** — the family's phones
(`iphone-15-pro-max`, `jacobs-s21-ultra`) are tailnet nodes. That is the access
path this deployment assumes.

---

## 0. What you need on the host

- Docker Engine with the Compose v2 plugin (`docker compose`, not
  `docker-compose`). Check: `docker compose version`.
- Git.
- A `.env` file at the **repo root** — not in `Groceries/`. `docker-compose.yml`
  refers to it as `../.env`.
- Roughly 2 GB free for images (the scraper image carries headless Chromium and
  pandas), plus room for the database and `backups/`.

```
docker compose version && git --version && df -h .
```

## 1. Get the code and write `.env`

```
git clone git@github.com:jfitzpatrick6/GroceryCostComparison.git
cd GroceryCostComparison
```

Create `.env` at the repo root. Required contents (names verified against
`collector.py`, which is what reads them):

| Variable | Required? | Notes |
|---|---|---|
| `BJS_STORE` | **yes** | In `REQUIRED_STORE_ENV`; `collector.py` fails loudly at startup if missing. Location-identifying. |
| `WALMARTSTORE` | **yes** (listed) | Also in `REQUIRED_STORE_ENV`, so it is checked even though Walmart scraping does not work (#12). Set it or the run aborts before scraping anything. |
| `TOPS_STORE`, `ALDIS_STORE` | no | Read and passed to the scrapers, but **deliberately not required** — `tops.py`/`aldis.py` don't use them yet, because Instacart's white-label platform no longer supports safe store targeting via a simple store id (#43). Setting them changes nothing today. |
| `USDA_API_KEY` | recommended | Free key from <https://fdc.nal.usda.gov/api-key-signup>. Without it the planner's purchase-unit estimates silently return nothing rather than erroring. |
| `SECRET_KEY` | recommended | Defaults to a hardcoded dev constant, which is safe for *correctness* across gunicorn workers but not for security — session cookies and (once #69 lands) CSRF tokens derive from it. Generate with `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`. |

**Never commit `.env`, and never paste a store id into an issue, PR or log** —
they identify which store you shop at.

```
chmod 600 .env
```

`git check-ignore .env` should print `.env` — confirm it is ignored before
anything else, because a committed `.env` puts store IDs in the repo history
permanently.

## 2. First start

```
cd Groceries
docker compose up -d --build
```

**Know what this starts before you run it.** There is no `profiles:` key in
`docker-compose.yml`, so a bare `up -d` starts **every** service — including
`grocery_scraper`, the *one-shot* scraper, which will immediately begin a full
scrape (Tops alone is on the order of 20 minutes, walking ~170 category pages
through a headless browser). That is fine on a first deploy, where you want price
data, but it is not what you want on every subsequent `up`. Issue #63 tracks
profiling the one-shot service so the default set is `db`, `webapp`,
`scraper_scheduler` and `db_backup`.

To start without the one-shot scrape:

```
docker compose up -d --build db webapp scraper_scheduler db_backup
```

Startup is ordered, not parallel: `db` has a `pg_isready` healthcheck and the
other services wait on `condition: service_healthy`, so nothing connects before
Postgres is actually accepting connections.

> **`pg_isready -h 127.0.0.1` matters.** Without `-h`, the probe uses the unix
> socket, which the postgres image's *temporary* initdb server also listens on.
> Measured: the socket reports ready ~2.2s before TCP does, and clients
> connecting in that window get `FATAL: the database system is shutting down`.
> The webapp survives it (its schema init retries); the one-shot scraper does
> not, because it connects once with no retry and no restart policy.

## 3. Confirm it came up correctly

```
docker compose ps
```

Expected: `db` **healthy**, `webapp` **healthy** (allow up to ~90s — its
`start_period`), `db_backup` running, `scraper_scheduler` running.

```
docker compose logs --tail 20 webapp
```

You want to see, in order:

1. `init_schema: app schema ready (attempt 1)` — the webapp created its own ten
   tables before serving anything.
2. `Starting gunicorn 23.0.0`
3. `Using worker: gthread`
4. Two `Booting worker with pid:` lines.

You do **not** want to see `WARNING: This is a development server`. If you do,
the image is stale — rebuild.

Then check the database really is empty-but-correct, and that the app answers:

```
docker compose exec db psql -U user -d grocery_db -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"   # -> 10
curl -s http://127.0.0.1:5000/healthz                                            # -> {"database":"reachable","status":"ok"}
```

`/healthz` returning `503 {"status":"unhealthy"}` means the app is up and the
database is not reachable — check `docker compose logs db`.

### Every page, on a fresh database

This was verified during development and is worth repeating on the real host,
because a first deploy is exactly when schema-ordering bugs show up (#80, #82):

```
for p in / /healthz /prices /staples /list /pantry /recipes /recipes/new \
         /planner /planner/ingredients /history /profiles /list/where-to-buy; do
  printf '%s  %s\n' "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5000$p)" "$p"
done
```

All should be `200`. `/recipes/<id>` returns `404` for an id that doesn't exist,
which is correct — a `500` there means the schema wasn't created.

> **Test each page against a fresh database, not in sequence after browsing.**
> Sweeping these in order hides order-dependent bugs, because earlier pages
> create tables later ones need. That is precisely how #80 and #82 stayed hidden
> for weeks on a dev database that had been populated for a while.

## 4. Reach it from the family's devices

The container publishes `5000:5000` on **all** of gigabyte's interfaces. From a
tailnet device:

```
http://gigabyte.taild1e879.ts.net:5000
```

There is **no authentication** — that is a deliberate, documented tradeoff
(issue #35: the tailnet is the access control). Two consequences to be aware of:

- Anyone on gigabyte's **LAN** can also reach it, not just the tailnet. #69
  tracks deciding whether that's acceptable or whether to bind to the Tailscale
  interface only.
- Never publish this port to the public internet. If that ever becomes necessary,
  put a real reverse proxy with auth in front of it.

## 5. Verify backups are real

`db_backup` takes a dump on startup, then nightly at 03:30 UTC (after the 03:00
scrape). Dumps go to `backups/` at the repo root — a host bind mount, outside the
`db_data` volume, so they survive `docker compose down -v`.

```
docker compose logs --tail 5 db_backup     # want: "OK /backups/grocery_db-...sql.gz <size> prices_data=false"
ls -la ../backups/
```

Dumps should be owned by your host user (the service runs as `1000:1000`), not
root — otherwise you can't delete them without sudo.

**Then actually restore one.** A backup nobody has restored is a hypothesis. The
full procedure is in README's "Backups and restore"; the part people get wrong is
that restoring over an *existing* database needs a schema drop first, because the
dump is taken without `--clean` and contains no `DROP` statements. Skipping it
fails with `relation "..." already exists`.

Note the default excludes `grocery_prices` **rows** (schema and view are kept).
Restoring therefore loses price history; recipes, meal plans, cook history,
pantry, list and profiles all come back. Set `BACKUP_INCLUDE_PRICES=true` in
`docker-compose.yml` if you want complete dumps.

**Backups live on the same host as the database.** That protects against
`down -v`, a botched migration and container loss — not against gigabyte's disk
dying. If the recipes matter that much, copy `backups/` off-host; see #60's
out-of-scope note.

## 6. Prices

`scraper_scheduler` runs cron in the foreground and scrapes daily at 03:00.
Walmart does not work (#12 — bot-verification wall, deliberately not
circumvented), so expect three of four stores and a failure line for Walmart in
every run. That is not a broken deployment.

```
docker compose logs --tail 40 scraper_scheduler
```

Tops and Aldi go through Instacart's white-label storefront and **cannot be
pointed at a chosen store yet** (#43) — they use whatever the host's network
location IP-geolocates to. If gigabyte is not physically near the stores you
shop at, the scraped prices will be for the wrong locations. Check this before
trusting the where-to-buy page.

> **Price staleness is currently invisible in the UI.** `/list/where-to-buy`
> shows no price date, so a six-week-old dataset presents with the same
> confidence as this morning's. #62 tracks fixing it. Until then, check
> freshness by hand:
> ```
> docker compose exec db psql -U user -d grocery_db -c \
>   "SELECT store, max(datetime)::date AS last_scrape, count(*) FROM grocery_prices GROUP BY store ORDER BY store"
> ```

## 7. Routine operations

```
docker compose logs -f webapp              # follow the app
docker compose restart webapp              # schema init re-runs; safe
docker compose pull && docker compose up -d --build   # after a git pull
docker compose exec webapp python init_schema.py      # re-run schema init by hand (idempotent)
docker compose --profile manual run --rm grocery_scraper   # one-off scrape, once #63 lands
```

Applying a change that alters container configuration **recreates** the affected
containers. For `db` that means the household database restarts and replays WAL —
expected and safe, but do it deliberately rather than discovering it at 6pm.

## 8. Known gaps as of this writing

Verify against the tracker rather than trusting this list; it goes stale.

| Issue | Effect on a deployed household |
|---|---|
| #62 | No price-age indicator on where-to-buy. Check freshness by hand (§6). |
| #63 | A bare `up -d` also starts the one-shot scraper; README's commands name services explicitly. |
| #64 | `/planner/ingredients` can block for minutes on a cold cache (synchronous USDA lookups). Avoid hitting it repeatedly for a new week. |
| #65 | `grocery_prices` has no index or retention; the latest-view query full-sorts the table. Fine at first, degrades over months. |
| #66 | Schema is still created ad-hoc per request (~32 call sites), taking `ACCESS EXCLUSIVE` locks on `ALTER TABLE … ADD COLUMN IF NOT EXISTS`. Startup creation (#82) made it correct, not cheap. |
| #67 | No connection pooling — a fresh Postgres connection per route, and the template context processor opens another per page render. |
| #68 | No error handlers and no flash messages: failures render as bare 500s, and successful actions give no confirmation. |
| #69 | No CSRF tokens; port published on all interfaces. |
| #75 | Pantry and staples start empty, so the auto-restock / auto-deplete / low-stock features do nothing until seeded. |
