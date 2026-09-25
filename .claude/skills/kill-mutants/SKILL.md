---
name: kill-mutants
description: Kill the mutants that survive mutmut in app/domain and app/application by writing unit tests, verified by rerunning mutmut. Use when `make mutate` or `make mutate-diff` reports unreviewed survivors, or when asked to raise the mutation score.
---

# Kill surviving mutants

mutmut is the referee here, not you: a survivor counts as killed only when mutmut says so
after the new test exists. docs/testing.md explains the method.

## Rules

- Write tests only under `tests/unit/`. Never edit `app/`, `migrations/` or an existing
  assert. Weakening a test, or changing the code so the mutant disappears, is cheating,
  even when it turns the gate green.
- Follow the suite's style: a behavior per test, named as a sentence, Arrange/Act/Assert
  split by blank lines, the `world` fixture and `tests/factories.py` for setup, fakes
  and state rather than mocks and call checks. Update the file's docstring list of
  behaviors when a test adds one.
- Kill the behavior, not the mutant: ask what a user would see if this mutant shipped
  and test that. A test that only makes sense next to the mutant is a bad test.

## Loop

1. List the survivors: `make mutate-diff` (this branch) or `make mutate` (whole core).
2. For each one, read it: `uv run mutmut show '<name>'`, and the code around it.
3. Decide:
   - **Real gap**: write the test. Run it on the unmutated code (`uv run pytest <file>`):
     it must pass.
   - **Equivalent** (no caller can observe a difference) or **copy** (only the wording
     of an error message changes): add it to `mutation/equivalents.txt` with a one-line
     reason. Say so in your summary, with the reason, so a person can disagree.
4. Verify every kill mechanically: `uv run mutmut run '<name>'` must end in 🎉. A test
   you believe kills a mutant proves nothing until mutmut agrees.
5. Finish with `make mutate-diff` (or `make mutate`) green, then check the guardrails:

   ```bash
   git diff --name-only -- app migrations   # must print nothing
   git diff -U0 -- tests | grep -E '^-[^-]'  # removed test lines: must print nothing
   ```

6. Report: mutants killed (with the test that kills each), mutants marked equivalent
   (with why), and anything you could not kill and why.
