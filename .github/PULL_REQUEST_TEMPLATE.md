<!--
Three parts, same shape as a commit body (see CONTRIBUTING.md §3).
Delete the guidance comments before submitting. Keep "Verified" honest -
an unverified claim is worse than an admitted gap.
-->

Fixes #NN

## What changed

<!-- One or two sentences. What does this PR do? -->

## Why

<!-- The root cause or motivation, with the concrete symptom. Name the real
     behavior that was wrong, not just "improves code quality". If you
     deliberately did NOT do something adjacent, say so and why. -->

## How it was verified

<!-- Be specific and honest. Good: "35 mocked tests pass locally; live-FDC tier
     passes against the real API with USDA_API_KEY set"; "drove planner -> list
     -> where-to-buy end-to-end with a real recipe and confirmed X"; "diffed old
     vs new match groups across 19 product names, 0 mismatches".
     Bad: "should work", "tested locally".
     Counts go stale - say what you ran, not a number copied from here.
     If something is NOT verified, say what and why. -->

- [ ] `ruff check .` passes
- [ ] `pytest -k "not Live"` passes (the required, deterministic suite)
- [ ] Live-FDC tier checked if the change touches USDA/ingredient matching (non-blocking in CI by design, so it's on you)
- [ ] New behavior has a test / fixed behavior has a regression test that fails on the old code
- [ ] Independent second-opinion review addressed
- [ ] README updated if user-visible behavior, setup, or config changed

## Checklist for the reviewer

<!-- Anything that needs a careful eye: a schema change, a scrape-behavior
     change, a heuristic threshold, something you're unsure about. Delete if
     nothing. -->
