---
name: semantic-mutants
description: Hunt for holes in the test suite by writing realistic, stack-specific bugs (tenant leaks, missing row locks, wrong permission on a route, side effects inside a transaction) as patches in mutation/semantic/, checking them with mutation/semantic.py, and writing the tests that kill the survivors. Use when asked for semantic or LLM mutation testing, to audit a category of bugs, or after adding a repository, route or use case.
---

# Semantic mutants

mutmut changes operators; it never forgets a `WHERE workspace_id`, drops a `FOR UPDATE`
or wires a route to the read permission. You write those bugs, one patch each, and
`mutation/semantic.py` decides whether the suite catches them. You are exploring; the
script is the referee. docs/testing.md explains the method.

## Categories (and where they live)

| category | layer | typical bug |
|---|---|---|
| tenant-isolation | `app/infrastructure/db/repositories.py` | a query loses its workspace filter |
| concurrency | repositories, `unit_of_work.py` | a lock or a retry rule goes wrong |
| authorization | `app/application/`, `app/presentation/routers/` | a check is skipped, or a route asks for the wrong permission |
| authentication | `app/presentation/security.py`, `middleware.py` | a token or origin check is loosened |
| side-effects | `app/application/` | a file, mail or event happens inside the transaction, or before validation |
| data-loss | repositories, `app/application/purge.py` | a delete or purge reaches too far |
| data-integrity | repositories, `migrations/` | a constraint, escape or boundary is lost |

## Loop

1. Pick a category and read the code of its layer. Look at what already exists in
   `mutation/semantic/`; do not repeat a mutant.
2. Write 3 to 5 candidate bugs a competent developer could plausibly ship: small,
   compiling, changing behavior, each one a single idea. Save each as
   `mutation/semantic/<category-prefix>-<what>.patch`: a `git diff` of the change
   (make it on the working tree, `git diff > file`, then `git checkout -- <file>`),
   preceded by the header

   ```
   # category: tenant-isolation
   # tests: tests/integration tests/e2e
   # why: <what breaks in production, and for whom>
   ```

   `tests` names the tiers that should catch it: `tests/unit`, `tests/integration`
   (repositories, SQL, locks) or `tests/e2e` (routes).
3. Leave the working tree clean: `git status --short -- app migrations` prints nothing.
4. Run them: `uv run python mutation/semantic.py '<prefix>-*'`.
   - **KILLED**: keep the patch; the suite already guards this.
   - **SURVIVED**: a real hole. Write the test that kills it in the tier the header
     names, following the suite's style (behavior per test, sentence names, AAA, fakes
     and state over mocks). It must pass on the unmutated code (`uv run pytest <file>`),
     then the script must show the mutant KILLED.
   - **INVALID** or **STALE**: fix or regenerate the patch; never keep one.
   - A mutant you find no way to kill is probably equivalent: delete it and say why.
5. Finish with `make mutate-semantic` all KILLED, and the guardrails:

   ```bash
   git diff --name-only -- app migrations   # must print nothing
   git diff -U0 -- tests | grep -E '^-[^-]'  # removed test lines: must print nothing
   ```

6. Report each mutant: what it breaks, its verdict, and for the survivors the test that
   now kills it.

## Rules

- Never change `app/` or `migrations/` for real, and never weaken a test to kill a
  mutant.
- A patch stays in the catalog only once it is KILLED: the catalog is the regression
  suite the weekly job reruns (`.github/workflows/mutation.yml`).
- Your mutants are not deterministic; the catalog is. Anything worth keeping becomes a
  patch file, so the next run does not depend on you.
