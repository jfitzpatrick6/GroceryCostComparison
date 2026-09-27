# Contributing

How work gets done in this repo. Written for the two people who actually work
here: Jake, and the AI agents he directs. If you are an agent picking this up
cold, read this file and `AGENTS.md` before touching anything.

This is a **household application**, not a product with customers or an SLO.
That context decides most of the tradeoffs below: correctness and
maintainability matter a great deal; horizontal scale, multi-tenancy, and
enterprise auth do not. Don't add machinery this app will never need.

---

## 1. GitHub is the single source of truth

- **Every unit of work has an issue.** No issue, no PR. If you find something
  worth fixing mid-task, file an issue for it rather than smuggling the fix into
  an unrelated branch — unless it is a one-line typo blocking your actual work.
- The **issue**, not the README, states what is done / in progress / planned.
  README describes how to *use* the thing; issues describe what's *left*.
- Close issues from the commit or PR that resolves them (`Fixes #NN`), so the
  history and the tracker can't drift apart.
- Existing issues are numbered into the 50s and are referenced throughout the
  code comments. **Read the referenced issue before changing code that cites
  it** — those citations mark decisions that were already reasoned through,
  often against a specific bug.

### Labels

| Label | Meaning |
|---|---|
| `bug` | Something behaves wrongly |
| `enhancement` | New capability |
| `documentation` | Docs only |
| `area:webapp` / `area:scraper` / `area:infra` | Which part of the system |
| `p1-blocker` | Breaks or endangers daily household use. Do these first. |
| `p2-should-fix` | Real problem, tolerable for now |
| `p3-hygiene` | Cleanup, no user-visible effect |

---

## 2. Branching

`master` is always deployable and always green. Nothing is committed straight to
it — work lands through a PR, including Jake's own.

```
master ────────────────────────────────●────●───
                 \                    ↑    ↑
  fix/58-usda-…   ●───●───●───────────┘    │
  chore/repo-…        ●───●────────────────┘
```

- Branch from `master`, one issue per branch.
- Name it `<type>/<issue>-<short-slug>`:
  `fix/58-usda-qualified-ingredient-names`, `feat/62-price-freshness-banner`,
  `chore/77-repo-standards-and-ci`.
  `type` is `fix` | `feat` | `chore` | `docs` | `refactor`.
  The issue number is part of the name, not decoration — it's what lets anyone
  map a branch back to the tracker months later. (The PR that introduced this
  rule had to file its own issue retroactively during review, because its branch
  was named `chore/repo-standards-and-ci` with no number. Renaming a branch
  after its PR is open is more disruptive than filing the issue, so the branch
  kept its name — recorded here rather than left as a silent counterexample.)
- Keep branches short-lived. A branch that lives for weeks will conflict with
  everything; if the work is that big, the issue was too big — split it.
- Rebase onto `master` rather than merging `master` in, so history stays linear.
- Delete the branch after merge.

---

## 3. Commits

This repo has a strong existing commit style. Match it — don't invent a new one.

**Subject:** imperative mood, specific, ≤ ~72 chars where practical, with the
issue number in parentheses when there is one.

```
Fix USDA ingredient matching for ambiguous single-word names (#54)
Drop the Tailscale Docker sidecar; publish webapp directly for LAN access
```

**Body:** three parts, in this order. This is the convention that makes this
repo's history genuinely useful — a `git log` read tells you *why*, not just
*what*.

1. **Root cause / motivation.** What was actually wrong, and how it was
   discovered. Name the concrete symptom ("a 4.5–6.5 lb chicken breast pack
   priced '$2.19-2.69' is obviously $/lb, not a total package price").
2. **The change.** What you did and, where it matters, what you deliberately
   did *not* do and why.
3. **`Verified:`** — how you know it works. Be honest. "41 tests pass locally;
   live-FDC suite passes against the real API" is useful. "Should work" is not.
   If you could not verify something, say so explicitly.

**Trailer:** AI-assisted commits carry `Co-Authored-By: <Tool> <noreply@…>` so
the history records what was human-directed and what was agent-written.

Never weaken a test to make it pass. If a test is wrong, fix the test *and
explain in the commit body why the test was wrong* — that explanation is the
only thing standing between the next reader and re-breaking it.

---

## 4. Pull requests

Open a PR as soon as the branch has a coherent first commit; don't wait until
it's finished.

A PR must have:

- A title matching the commit-subject convention, and `Fixes #NN`.
- A description covering **what changed, why, and how it was verified** — the
  same three parts as a commit body. Use the PR template.
- **CI green.** Both jobs. No exceptions, including for "obvious" changes.
- **A second-opinion review** before merge. For agent-driven work this means an
  independent reviewer (a subagent or another CLI) that did not write the code.
  Real findings get fixed and regression-tested, not argued away.
- No unrelated changes. If you notice something else, file an issue.

Merge with **squash** so one issue = one commit on `master`.

---

## 5. Definition of Done

Work is done when *all* of these hold:

- [ ] `ruff check .` passes
- [ ] `pytest -k "not Live"` passes — the required, deterministic suite
- [ ] The live-FDC tier ran if the change touches USDA/ingredient matching. CI
      runs it **non-blocking by design** (see §7), so gating on it is on you.
- [ ] CI is green on the PR
- [ ] New behavior has a test. Fixed behavior has a *regression* test that
      fails on the old code — otherwise nothing stops it regressing.
- [ ] An independent review pass has been addressed
- [ ] README updated if user-visible behavior, setup, or config changed
- [ ] The issue is closed by the merge

**Verify, don't assume.** This repo's history is full of bugs found by actually
running the flow end-to-end rather than by reading code — the literal `"None"`
in grocery-list quantities (#e750a5e) was found by walking
planner → list → where-to-buy with a real recipe *after* the test suite passed.
If a change touches a user-visible flow, drive that flow.

---

## 6. Code conventions

These are observed patterns, not aspirations. Follow them.

**Comment the *why*, and cite the issue.** This repo's comments are its best
feature. They record the reasoning that isn't recoverable from the code:

```python
# By-weight items (produce, fresh meat/poultry) don't carry a `prices` field
# at all - BJs only exposes a min/max *per-pound rate* over the pack's min/max
# weight (see #16, #51 - verified against live data: a "$2.19-2.69" range on a
# 4.5-6.5 lb chicken breast pack is obviously $/lb, not a total package price).
```

Note what that comment carries: the mechanism, the issue references, *and* that
it was verified against live data rather than assumed. Write comments like that.

Do **not** write comments that narrate what the next line does.

**Prefer "no answer" over a wrong answer.** This is the single most consistent
design principle in the codebase, and it's the right one for an app that tells a
family where to spend money. `_usda_grams_per_unit` returns `None` on any doubt
rather than guessing, because *"a missing purchase estimate is fine, a wrong one
silently corrupts the shopping list."* Unmatched list items surface under "No
price match found for" rather than disappearing. Keep it that way.

**Hand-maintained lookup tables grow from real misses.** `MANUAL_ALIASES`,
`DISQUALIFYING_MODIFIERS`, `_USDA_SEARCH_ALIASES` are deliberately small and
each entry exists because a specific real case failed. Don't add speculative
entries, and don't replace them with cleverer general logic without evidence
the general logic is actually better.

**Don't abstract early.** Three similar lines beat a premature helper. No new
utility module for a one-time operation.

---

## 7. Testing

- Tests live next to the code: `Groceries/webapp/test_*.py`. Run with `pytest`
  from the repo root (config in `pyproject.toml`).
- `unittest.TestCase` style, matching the existing files.
- **Fixtures are real captured data, not invented examples.** `SAMPLE_CATALOG`
  in `test_matching.py` is real scraped rows; the USDA fixtures are trimmed real
  FDC responses captured against the live API. Invented fixtures encode the
  author's assumptions instead of the world's behavior. Keep this habit.
- **Two tiers for anything hitting an external API:** a mocked suite that always
  runs, and a live suite behind `@unittest.skipUnless(os.getenv("API_KEY"), …)`.
  Mocks are the regression net; the live suite is the truth. If the live tier is
  skipping in your environment, that's a coverage gap, not a convenience.
- **Only the mocked tier may gate CI.** The live tier asserts exact values a
  third party returns today — `1 tsp black pepper = 2.3g`, `1 cup butter = 227g`
  — so an FDC re-rank, a data revision, or a 429 would redden `master` with no
  code change and nothing anyone could do about it. That's the kind of flake
  that trains people to ignore CI. So `.github/workflows/ci.yml` splits them:
  `pytest -k "not Live"` is required, `pytest -k "Live"` runs with
  `continue-on-error: true` and reports. Run the live tier locally before
  merging anything that touches matching, and treat a failure there as real
  even though CI won't.
- The required tier must not need a database or network to pass. If a test needs
  one, it belongs in the live tier or the logic should be extracted into a pure
  function.
- **`app.py` is the coverage gap.** `matching.py` and the USDA logic are well
  tested; the 1,800 lines of routes, planner math, pantry depletion, and
  package-fit costing are not. New work in `app.py` should add tests — the pure
  helper functions (`merge_qty`, `_parse_needed_qty`, `_annotate_package_fit`,
  `parse_ingredient_line`, `parse_pasted_recipe`) are testable without a
  database and are the right place to start.

---

## 8. Database and schema

**The current state is known-bad and is being fixed — do not make it worse.**

The webapp's own tables are now created **once at container startup** by
`init_schema.py`, which calls `ensure_app_schema()` before gunicorn serves
anything (#82). That is the correct place, and it fixed a real first-deploy
failure: schema creation used to be scattered through request handlers, so
whether a table existed depended on which URL someone opened first — three
recipe routes 500'd on an empty database because they provisioned nothing.

What has *not* been removed yet is the legacy behaviour underneath: ad-hoc
`CREATE TABLE IF NOT EXISTS` and `ALTER TABLE` calls still sit in nine
`ensure_*_table()` functions at roughly 32 call sites inside route handlers (47
occurrences of `ensure_` in total, including the nine definitions — grep to
recheck rather than trusting a number in a document), and they still execute on
nearly every request. They are now semantic no-ops, but **not free**: measured on
postgres:16 by holding a transaction open and reading `pg_locks` from another
session, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` takes an
**`AccessExclusiveLock`** on the relation *even when the column already exists
and nothing changes*. `CREATE TABLE IF NOT EXISTS` against an existing table
takes **no lock at all** (Postgres notices and skips it), so the cost is the
`ALTER`s, not the `CREATE`s — app.py has about ten of them. One call site
(`inject_profile_switcher`) is a Flask `@app.context_processor`, so *every page
render* runs DDL and a `COMMIT`, and one path drops and re-adds a constraint on
`meal_plan_slots` implicitly. Removing those call sites is #66.

Rules while that stands:

- **Never add a new `ensure_*_table()` call site in a request handler.** If you
  need a schema change, say so in the PR and coordinate it with the migrations
  issue (#66). (`ensure_app_schema()` calling the nine helpers is the sanctioned
  exception — that's the one ordered place schema is declared, not a request
  handler. The rule below is what that means in practice.)
- **A new table goes in `ensure_app_schema()`**, not in a route. That function is
  the single ordered place the webapp's schema is declared, and
  `test_app_schema.py` asserts every table any route queries is either created
  there or explicitly listed as owned by something else — so a route added
  against a table nobody creates fails in CI instead of 500ing on the next fresh
  deploy. Add the table to that test's `EXPECTED_TABLES` too.
- **Never write a destructive migration** (`DROP COLUMN`, `DROP CONSTRAINT`,
  `ALTER TYPE`) as an implicit side effect of a request handler.
- Schema for scraped prices is owned by `collector.py`; app tables are owned by
  the webapp. `price_data_available()` exists because the webapp must tolerate a
  database the scraper hasn't populated yet — preserve that graceful degradation
  in any new page that joins against `grocery_prices_latest`.
- **All SQL is parameterized** (`%s` placeholders, values passed separately).
  There is no string interpolation of user input into SQL anywhere, and there
  must not be. The one f-string-built `ORDER BY` in `prices()` is safe only
  because it selects from the `SORT_COLUMNS` whitelist — keep it that way.
- `grocery_prices` **appends**; it never overwrites. Query
  `grocery_prices_latest` unless you genuinely want history.

---

## 9. Secrets and configuration

- All config comes from `.env` at the repo root, loaded by docker-compose via
  `env_file`. `.env` is gitignored and **must stay that way**.
- Store IDs are location-identifying. Never commit them, never paste them into
  an issue, PR, or log.
- **No hardcoded credentials in source.** This includes API keys that "are
  public anyway" — a client-side key committed to a repo can't be rotated
  without a code change, and its presence trains the next contributor to think
  committing keys is fine. (There is currently one such key in `BJs.py`; it has
  an issue and should not be copied.)
- Add new config as an `os.getenv("NAME")` read plus a documented line in the
  README's `.env` block. If a value is required, fail loudly at startup with a
  clear message rather than degrading mysteriously later — see
  `REQUIRED_STORE_ENV` in `collector.py`.

---

## 10. Lint configuration and its exemptions

Config is in `pyproject.toml`; **every** ignored rule has a written reason next
to it. Read those reasons before adding to the list. The short version:

- `line-length = 120` because that's what the code already writes to.
- `target-version = "py310"` — the *scraper* container is 3.10, the webapp is
  3.12. Targeting the older one stops lint from suggesting syntax the scraper
  image can't run.
- `BLE001` (blind `except Exception`) is not enabled, because the broad catches
  here are deliberate fault tolerance: one store's scraper throwing must not
  discard the others' results (#24), and one malformed product must not abort a
  20-minute scrape.
- `RUF001/002/003` (ambiguous unicode) are ignored because this app's *domain*
  is messy human-written product text. The load-bearing case is
  `matching.py`'s `r"\bconfectioners['’]?\s+sugar\b"` — real store listings use
  the curly apostrophe, so "fixing" it would silently break matching.
  **Never autofix these.**
- `SIM105` and `SIM117` are ignored repo-wide because both name *recurring*
  patterns (documented at each entry in `pyproject.toml`). `SIM108` is **not**
  ignored — it fires at exactly one site, which carries a targeted
  `# noqa: SIM108` with its reason beside it. That's the rule: blanket ignore
  for a pattern, targeted noqa for a single line.
- `ruff format` is intentionally not enforced (see the CI workflow).

**One gap to know about.** E501 has three documented exemptions (`ruff rule
E501`), and one of them bites here: **a line that *ends with* a URL is exempt,
as long as the URL starts before the line-length threshold.** `BJs.py` has a
**1,129-character line** that is one long request URL, and E501 will not flag
it — at any `line-length`.

The exemption is narrower than it sounds, which is why it's worth stating
precisely: the same line *is* flagged if anything follows the URL (appending
`  # note` pushes it over), and a long non-URL string assignment is flagged
normally. So the rule isn't "long URLs are allowed", it's "a line whose tail is
a URL gets a pass." Don't assume "lint is green" means "no absurd lines."

---

## 11. Local development

The app runs in Docker; you don't need Python installed to run a scrape or the
webapp. See README for that. For *development* (running tests and lint on the
host):

```bash
python3 -m venv .venv
.venv/bin/pip install -r Groceries/webapp/requirements.txt
.venv/bin/pip install pytest ruff
.venv/bin/ruff check .
.venv/bin/pytest
```

`.venv/` is gitignored.

Two gotchas:

- **Host Python may not match container Python.** The containers are 3.10
  (scraper) and 3.12 (webapp). A host on 3.14 cannot install the pinned
  `psycopg2-binary==2.9.10` — no wheel exists and building from source needs
  `pg_config`. Install `psycopg2-binary` unpinned for local test runs; the
  container pin is unaffected.
- **The scraper's dependencies (pandas, playwright) are heavy.** The test suite
  doesn't need them — it only covers `Groceries/webapp/`. Don't install the
  scraper requirements just to run lint or tests.

---

## 12. Docker and services

- `db` — Postgres 16, data in the `db_data` volume. Not published to the host.
- `grocery_scraper` — one-shot; runs `collector.py` and exits.
- `scraper_scheduler` — same image, runs cron in the foreground (daily 03:00).
  Long-running. **Not started by a bare `docker compose up`** — start it
  explicitly or prices silently go stale.
- `webapp` — Flask on `0.0.0.0:5000`, published to the host.

The webapp is reachable from the LAN and, via the host's own `tailscale0`
interface, from the tailnet. It has **no authentication** — that is a deliberate
documented tradeoff (issue #35), not an oversight. Never publish this port to
the public internet. If that ever becomes necessary, put a real reverse proxy
with auth in front of it; don't bolt auth onto Flask.

**There is no backup mechanism.** Recipes and meal history exist only in the
`db_data` volume and are not re-derivable. Treat any change touching that volume
or running a destructive migration as needing extra care, and get the backup
issue done before trusting this with real household data.
