# Testing

Three tiers of tests decide *what kind of test* each layer gets. Mutation testing decides
*whether those tests would catch a bug*. It runs in two forms: mutmut on the core, and
semantic mutants for the bugs mutmut cannot write.

```sh
make test              # unit: ~1 s, no Docker. Run it all the time.
make test-integration  # repositories, migrations, locks on a throwaway Postgres
make test-e2e          # the HTTP API end to end on that Postgres
make test-all          # all three with branch coverage: what CI runs
make mutate-diff       # mutation score of the core modules your branch changed
make mutate            # mutation score of the whole core (minimum 95%)
make mutate-semantic   # every realistic bug in mutation/semantic/ must be caught
```

## The triad

| Tier | Tests | Runs against | Share | Time |
|---|---|---|---|---|
| `tests/unit/` | entities, use cases, pure edges (JWT, cursors, config) | in-memory fakes | ~73% | ~1 s |
| `tests/integration/` | SQL repositories, migrations, unit of work, row locks | Postgres 16 in Docker | ~23% | ~5 s |
| `tests/e2e/` | the critical journeys over HTTP | the real app on that Postgres | ~3% | ~4 s |

Clean Architecture puts each tier on its own layer:

```
presentation (routers, security)    ← e2e, plus unit tests of its pure parts
infrastructure (SQL, AWS adapters)  ← integration, on a real Postgres
application + domain                ← unit, with fakes of the ports
```

- **Unit** (`tests/unit/`). `tests/unit/` mirrors `app/`: the tests of
  `app/application/products.py` live in `tests/unit/application/test_products.py`. The
  use cases run on `tests/fakes.py`, one in-memory implementation per port. The
  `world` fixture wires them (`tests/unit/conftest.py`).
- **Integration** (`tests/integration/`). Postgres is never mocked. testcontainers
  starts `postgres:16-alpine` once per session (`tests/database.py`), and the real
  Alembic migrations build the schema, so the migrations are tested too. Each test runs
  in a transaction that is rolled back afterwards. The code under test commits to a
  SAVEPOINT. Concurrency tests need real commits across connections, so they use the
  `committed` fixture, which truncates afterwards.
- **E2E** (`tests/e2e/`). The real app is driven through `httpx.ASGITransport` in the
  test's event loop, so it shares the test's session and rollback. Only what lives
  outside the process is replaced: the AWS adapters by the fakes, and Cognito's
  signature check by the fake pool's tokens (`tests/unit/presentation/test_security.py`
  covers the real check, with keys generated in the test). Keep this tier small:
  journeys and cross-cutting rules (tenant isolation, route permissions), not every
  error.

### Keeping the fakes honest

A fake that behaves differently from the real adapter makes the unit tests lie. The
repository tests in `tests/integration/repositories/` are contract tests: each one runs
against the SQL repositories **and** the fakes (`any_uow` is parametrized over both).
This already caught one difference. `revoke_pending` was a Core bulk `UPDATE`, which left
invitation objects already loaded in the session stale, while the fake updated them in
place. The repositories' bulk statements now name the entity and synchronize the
session (`synchronize_session="fetch"`). The contract tests pin that, and the
`stale-bulk-*` semantic mutants check it stays that way.

`FakeUnitOfWork` imitates the parts of the real one that use cases depend on:
- Transactions roll back and keep the objects callers hold, as the ORM's identity map
  does.
- Unique constraints raise `DuplicateError`.
- `uow.conflicts = n` replays a lost Aurora DSQL race: the work reruns from the top.
- `uow.fail_next_commit = exc` fails the next commit.

The other fakes expose state for tests to assert on: `media.objects` (what is stored),
`uow.locks`, `events.published`, `mailer.sent`. Tests check that state, not which calls
were made.

## Writing a test

1. **List the behaviors first.** Every test file opens with a docstring listing what
   the unit must do. Each line becomes a test. For each behavior, ask three questions:
   what is the happy path, where are the limits (zero, exact, one more, empty, None),
   and what can go wrong?
2. **Name it as a sentence.** When it fails in CI, the name alone should say what broke:
   `test_a_stale_version_changes_nothing`, not `test_update_2`.
3. **One behavior per test, and Arrange / Act / Assert split by blank lines.** Two Acts
   means two tests.
4. **Set up with factories.** `tests/factories.py` builds entities with sensible
   defaults, and you override only what the test is about:
   `factories.product(price=Decimal(0))`. In the use-case tests, `world.user(...)`,
   `world.workspace(...)` and `world.join(...)` build the scene.
5. **Turn near-identical tests into a table** with `pytest.mark.parametrize`.
6. **Use fakes, not mocks, and assert on state, not calls.** Fix what you do not
   control (tokens, the clock, other requests) through the fakes' hooks.
7. **If a test is hard to write, suspect the design first.** Logic that needs ten mocks
   usually belongs in a use case.
8. **Finish with mutation testing.** Run `make mutate-diff` on your branch. It lists
   the cases your tests let through.

## Mutation testing without AI: mutmut

Coverage says which lines ran. Mutation testing says whether a test would fail if the
code were wrong. mutmut makes small changes (`>=` becomes `>`, `True` becomes `False`,
an argument becomes `None`) and runs the unit tests against each one. A mutant that no
test notices *survives*. It means there is a bug the suite cannot see.

- **Scope**: `app/domain` and `app/application`, against `tests/unit` only
  (`[tool.mutmut]` in pyproject.toml). Each mutant costs milliseconds. The adapters and
  routes are left to the semantic mutants below.
- **Noise left out**: the wording of error messages and logging. Tests assert the kind
  of error, which is its HTTP status, not the copy.
- **Reviewed equivalents**: some survivors change nothing a caller can see.
  `mutation/equivalents.txt` lists each one with its reason. `mutation/gate.py` leaves
  them out of the score and reports any other survivor as unreviewed.
- **When it runs**: on every pull request that touches the core, for just the changed
  modules (`make mutate-diff`, the `mutation` job in `ci.yml`). The whole core runs
  weekly (`.github/workflows/mutation.yml`). The gate fails below 95%.

To look at a survivor: `uv run mutmut show '<name>'`, or `uv run mutmut browse` for the
interactive view. For each one, either write the test that kills it or add it to
`equivalents.txt` with the reason. Do not chase 100% by testing wording.

## Mutation testing with AI

With AI writing tests, coverage stops meaning much. A model can execute every line and
still check nothing. The mechanical score is the referee that keeps AI-written tests
honest. AI helps in two places.

### 1. Killing mutmut's survivors (`/kill-mutants`)

The `kill-mutants` skill (`.claude/skills/kill-mutants/`) gives an agent each survivor,
the code and the existing tests. It writes a unit test in the suite's style, or marks
the mutant equivalent with a reason for a person to review. Then it **reruns mutmut on
that mutant**. A kill counts only when mutmut says so.

### 2. Semantic mutants (`/semantic-mutants`)

The bugs that cost the most in this stack are not a flipped operator:
- a query that forgets `workspace_id` (one tenant sees another's data)
- a lost `FOR UPDATE` (quotas overshoot, `If-Match` stops protecting)
- a route that asks for the read permission instead of the write one
- a file or email handled inside a transaction that may roll back

mutmut never writes these. An LLM can, when asked for "a realistic bug of category X in
layer Y".

Each such bug lives in `mutation/semantic/` as a patch with a header:

```
# category: tenant-isolation
# tests: tests/integration tests/e2e
# why: A product id from another workspace loads, so anyone can read or edit any catalog.
--- a/app/infrastructure/db/repositories.py
+++ b/app/infrastructure/db/repositories.py
...
```

`mutation/semantic.py` checks each patch in a copy of the repository:
1. It applies the patch and checks that the result still compiles.
2. It runs the tiers named in the header and reports the patch as **KILLED**,
   **SURVIVED**, **STALE** (the code moved) or **INVALID**.
3. It first checks that the unmutated suite passes, because against a red baseline
   every mutant would look killed.

Candidate patches run the same way: `uv run python mutation/semantic.py path/to/x.patch`.

The `semantic-mutants` skill runs the loop:
1. Pick a category and read its layer.
2. Write 3 to 5 plausible bugs as patches.
3. Run them.
4. For each survivor, write the test that kills it.
5. Keep only KILLED patches.

The LLM explores, which is not deterministic. The catalog it leaves behind is, and CI
reruns it weekly as a regression suite.

The first run of the catalog had 23 mutants, and 4 survived. Every one was a real hole:
- `authz-update-route-reads`, `authz-delete-route-reads`: a viewer could edit or delete
  products, and no test would notice.
- `tenant-member-list`: the member list could show every workspace's people. The test
  only had one workspace.
- `side-effect-avatar-in-transaction`: deleting the old avatar inside the transaction
  would leave the profile pointing at a deleted file if the commit failed.

Each one now has a test that kills it.

The suite also surfaced three behaviors, which are now fixed. Each fix has tests and a
semantic mutant that restores the old behavior, so a regression gets caught:
- `onboarding-traps-leavers`: deleting the account needed finished onboarding, so
  someone removed from their only workspace could not leave.
- `registration-name-ignored`: a name given at registration still left the profile step
  pending.
- `stale-bulk-revoke`, `stale-bulk-member-removal`: bulk changes left the session's
  objects stale.

### Guardrails for agents

- Agents write tests only. `app/` and `migrations/` stay untouched, and no existing
  assert is weakened or deleted. Both skills end by checking:

  ```bash
  git diff --name-only -- app migrations   # must print nothing
  git diff -U0 -- tests | grep -E '^-[^-]'  # removed test lines: must print nothing
  ```

- Check everything by running it. A new test must pass on the original code and fail on
  the mutant. mutmut and `semantic.py` decide that, not the model.
- The same model does not write the code, write the tests and grade both. The mutation
  score, computed mechanically, is the external referee.

## Where the suite stands

| | Before | After |
|---|---|---|
| Tests | 42 (unit only) | 467: 341 unit, 108 integration, 18 e2e |
| mutmut score, domain | 52% (220/423) | 97.8% (353/361); the other 8 are reviewed equivalents |
| mutmut score, use cases | 0% (0/1211) | 98.3% (1143/1163); the other 20 are reviewed equivalents |
| Semantic mutants | none | 27 of 27 killed |
| Line + branch coverage | not measured | 88% |

The "before" scores still counted mutations of error-message wording, which are now left
out. Those mutants are why the before and after totals differ.

Known gaps:
- **The S3 and Pillow adapter** (`app/infrastructure/aws/storage.py`) and **Cognito
  account creation** (`cognito.py`) have no tests. moto, which `make up` already runs,
  could back an integration test of both.
- **Aurora DSQL itself.** Postgres 16 stands in for it. DSQL's optimistic concurrency
  (SQLSTATE 40001 at commit) is exercised by simulating the error, not by producing a
  real one.
