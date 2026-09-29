# AGENTS.md

Operational brief for AI agents working in this repo. `CONTRIBUTING.md` is the
full standard; this is the short version plus the things that specifically trip
up an agent picking the repo up cold.

## What this is

A self-hosted household grocery app: Playwright/HTTP scrapers write store prices
to Postgres (`Groceries/`), and a Flask webapp (`Groceries/webapp/`) does
recipes, weekly meal planning, a self-tracking pantry, a persistent grocery
list, and a "cheapest store for my list" comparison. **One household uses it.
It is not a product.** Scale, multi-tenancy, and enterprise auth are non-goals;
correctness and not losing the family's recipes are not.

## Non-negotiables

1. **GitHub is the source of truth.** Every unit of work has an issue; no issue,
   no PR. Work lands on `master` only through a PR with CI green.
2. **Never commit to `master` directly.** Branch: `<type>/<issue>-<slug>`.
3. **`ruff check .` and `pytest -k "not Live"` pass before you call anything
   done.** Config is in `pyproject.toml`; CI is `.github/workflows/ci.yml`. The
   live-FDC tier (`-k "Live"`) is **non-blocking in CI by design** — it asserts
   values a third-party API returns today, so a failure there must not redden
   `master` — but run it locally and treat a failure as real if you touched
   USDA/ingredient matching.
4. **Never weaken a test to make it pass.** If the test is wrong, fix it and
   explain why in the commit body.
5. **Never autofix `RUF001/002/003`.** The curly apostrophe in `matching.py`'s
   `r"\bconfectioners['’]?\s+sugar\b"` is load-bearing — real store listings use
   typographic quotes, and ruff's proposed fix swaps in a **grave accent**.
   "Fixing" it silently breaks cross-store matching.
6. **No secrets in source, ever.** Config comes from `.env` (gitignored). Store
   IDs are location-identifying — never paste them into an issue, PR, or log.
7. **Schema changes are numbered migrations** in `Groceries/webapp/migrations.py`,
   applied once at startup (#66). Never call an `ensure_*` function or run DDL
   from a request path, and never edit an `ensure_*` function to change the schema
   - existing databases won't re-run it. Destructive steps only in a migration.
8. **Don't disturb the running containers.** `groceries-webapp-1` and
   `groceries-db-1` may be up on the dev machine. Jake's dev instance is not
   production. Don't restart, rebuild, or `down` them, and don't run destructive
   SQL against `db_data` — it holds the only copy of the household's recipes.

## Design principle to preserve

**Prefer "no answer" over a wrong answer.** This app tells a family where to
spend money, so a confident wrong number is worse than a gap. `_usda_grams_per_unit`
returns `None` rather than guessing; unmatched list items surface under "No price
match found for" instead of vanishing; yellow bell pepper is deliberately left
unaliased because its FDC entry lacks cup data. Follow this in new code.

## Conventions worth matching

- **Comments explain *why* and cite issue numbers.** This repo's comments are its
  best asset — they record reasoning not recoverable from the code, including
  whether something was *verified against live data* or merely assumed. Write
  comments like that; never narrate what the next line does.
- **Commit bodies are three parts:** root cause (with the concrete symptom), the
  change (including what you deliberately didn't do), and a `Verified:` line
  stating how you know it works. AI-assisted commits carry a `Co-Authored-By:`
  trailer. See `git log` for examples — match them.
- **Test fixtures are real captured data**, not invented examples. Invented
  fixtures encode the author's assumptions instead of the world's behavior.
- **Hand-maintained lookup tables** (`MANUAL_ALIASES`,
  `DISQUALIFYING_MODIFIERS`, `_USDA_SEARCH_ALIASES`) grow only from real,
  observed misses. No speculative entries.
- Don't abstract early. Three similar lines beat a premature helper.

## Environment gotchas

- Containers: scraper is **Python 3.10**, webapp is **3.12**. `ruff`'s
  `target-version` is `py310` so lint can't suggest syntax the scraper can't run.
- A host on **Python 3.14 cannot install the pinned `psycopg2-binary==2.9.10`**
  (no wheel; source build needs `pg_config`). For local test runs install
  `psycopg2-binary` unpinned in a `.venv` — the container pin is unaffected.
- Tests need only the **webapp** requirements (flask, psycopg2-binary, requests)
  plus pytest, and never touch a **database**. They *can* touch the network: the
  live-FDC tier in `test_usda_matching.py` runs whenever `USDA_API_KEY` is
  resolvable, and that file loads the repo-root `.env` itself, so a plain
  `pytest` on a machine with `.env` present makes real API calls. Do not install
  the scraper's heavy deps (pandas, playwright) just to lint or test.
- `USDA_API_KEY` lives in `.env`. `test_usda_matching.py` loads it from there
  itself so the live-FDC tier runs locally; in CI it comes from a repo secret and
  skips cleanly when absent.
- E501 exempts **a line that ends with a URL** (provided the URL starts before
  the limit), so `BJs.py`'s **1,129-character request-URL line will never be
  flagged** — at any `line-length`. The exemption is narrower than it sounds:
  append anything after the URL and it *is* flagged. "Lint is green" does not
  mean "no absurd lines." See CONTRIBUTING §10.

## Known state (verify before relying on this)

Rewritten 2026-09-29 (#128) after the "Family ready" milestone work. The tracker
moves; check the issues.

- **Walmart scraping does not work** - bot-verification wall, deliberately not
  circumvented (#12). Tops/Aldi work via Instacart's white-label platform; BJs
  via its search API, with its public key in `.env` as `BJS_CNSTRC_KEY` (#72).
- Tops/Aldi **cannot be pointed at a chosen store** (#43, deliberately out of
  the milestone) - they use whatever the host's IP geolocates to. A wrong
  Instacart `zoneId` returns silently wrong prices, which is why it isn't
  forced.
- **Prices stay fresh by default**: `docker compose up -d` starts the scheduler
  (03:00 UTC nightly, plus a catch-up scrape at start when prices are >26h old);
  every run ends with `Run summary: OK|PARTIAL|FAILED` (#63). The one-shot
  scraper is behind `--profile manual`.
- **Schema**: webapp tables change only through numbered migrations in
  `Groceries/webapp/migrations.py`, applied at startup (#66); `collector.py`
  owns `grocery_prices` and runs DDL only when its schema check says so.
  `test_migrations.py` fails the build on request-path DDL.
- **Performance**: pooled connections (#67); the normalized price catalogue is
  cached per worker until a scrape or prune changes it (#57); USDA lookups run
  in a background thread, never on a request (#64). The catalogue read itself is
  still O(retained rows), paid once per scrape.
- **Webapp safety**: every POST carries a CSRF token (#69); no passwords by
  design (#35) - the port is published on all interfaces on purpose so phones
  on the home Wi-Fi work. `SECRET_KEY` is optional (a random one is generated
  per container start if unset). Error pages and flash messages (#68). Nightly
  `pg_dump` backups to `backups/` (#60).
- **Tests**: pure helpers and the money/pantry routes are covered (#70); USDA
  matching has a live tier that runs when `USDA_API_KEY` is set. Matching still
  ranks by product name only - store departments (#114) are the next step.
