"""Semantic mutants: realistic bugs of this stack, kept as patches the suite must catch.

mutmut mutates operators (`>=` to `>`); it never forgets a tenant filter, drops a row
lock or wires a route to the wrong permission, which are the bugs that cost the most
here. Each file in mutation/semantic/ is one such bug as a unified diff, with a header:

    # category: tenant-isolation
    # tests: tests/integration tests/e2e
    # why: what breaks in production, and for whom

Most were written by an LLM asked for a bug of one category (docs/testing.md); none is
kept until this script shows the suite catches it. For each patch it copies the
repository aside, applies the patch, checks it still compiles and runs the tests the
header names:

    KILLED    a test failed: the suite catches this bug
    SURVIVED  every test passed: a hole in the suite (write the test that kills it)
    STALE     the patch no longer applies: the code moved, regenerate the mutant
    INVALID   the mutant does not compile or the tests could not run: fix the patch

Usage:
    uv run python mutation/semantic.py                  # the whole catalog
    uv run python mutation/semantic.py 'tenant-*'       # patches matching a glob
    uv run python mutation/semantic.py path/to/new.patch  # check a candidate

Exits non-zero unless every mutant is KILLED, and before any mutant if the unmutated
suite fails (a red baseline would make every mutant look killed).
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "mutation" / "semantic"
COPIED = ["app", "tests", "migrations", "pyproject.toml", "alembic.ini"]


@dataclass(frozen=True)
class Mutant:
    path: Path
    category: str
    tests: list[str]
    why: str

    @property
    def name(self) -> str:
        return self.path.stem

    @classmethod
    def load(cls, path: Path) -> Mutant:
        header: dict[str, str] = {}
        for line in path.read_text().splitlines():
            if not line.startswith("# "):
                break
            key, _, value = line[2:].partition(":")
            header[key.strip()] = f"{header.get(key.strip(), '')} {value.strip()}".strip()
        missing = {"category", "tests", "why"} - header.keys()
        if missing:
            raise SystemExit(f"{path}: header lacks {', '.join(sorted(missing))}")
        return cls(path, header["category"], header["tests"].split(), header["why"])


@dataclass(frozen=True)
class Outcome:
    verdict: str
    seconds: float
    detail: str = ""


def _copy_of_repo(into: Path) -> Path:
    target = into / "repo"
    target.mkdir()
    for name in COPIED:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, target / name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, target / name)
    return target


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)  # noqa: S603


def _pytest(repo: Path, tests: list[str]) -> subprocess.CompletedProcess[str]:
    return _run(
        [sys.executable, "-m", "pytest", *tests, "-x", "-q", "-p", "no:cacheprovider"], repo
    )


def _tail(result: subprocess.CompletedProcess[str], lines: int = 15) -> str:
    return "\n".join((result.stdout + result.stderr).strip().splitlines()[-lines:])


def check(mutant: Mutant) -> Outcome:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="semantic-mutant-") as tmp:
        repo = _copy_of_repo(Path(tmp))
        applied = _run(["git", "apply", "--unidiff-zero", str(mutant.path)], repo)
        if applied.returncode != 0:
            return Outcome("STALE", time.monotonic() - started, _tail(applied))
        compiled = _run([sys.executable, "-m", "compileall", "-q", "app", "migrations"], repo)
        if compiled.returncode != 0:
            return Outcome("INVALID", time.monotonic() - started, _tail(compiled))
        result = _pytest(repo, mutant.tests)
    seconds = time.monotonic() - started
    # pytest: 0 all passed, 1 some test failed; anything else means the run itself broke.
    if result.returncode == 0:
        return Outcome("SURVIVED", seconds)
    if result.returncode == 1:
        failed = [line for line in result.stdout.splitlines() if line.startswith("FAILED")]
        return Outcome("KILLED", seconds, failed[0] if failed else "")
    return Outcome("INVALID", seconds, _tail(result))


def baseline(tests: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="semantic-baseline-") as tmp:
        result = _pytest(_copy_of_repo(Path(tmp)), tests)
    if result.returncode != 0:
        sys.stderr.write(f"The unmutated suite fails; fix it first.\n{_tail(result)}\n")
        raise SystemExit(2)


def select(patterns: list[str]) -> list[Mutant]:
    if not patterns:
        return [Mutant.load(path) for path in sorted(CATALOG.glob("*.patch"))]
    chosen: list[Path] = []
    for pattern in patterns:
        path = Path(pattern)
        if path.suffix == ".patch" and path.exists():
            chosen.append(path.resolve())
        else:
            chosen += sorted(CATALOG.glob(f"{pattern.removesuffix('.patch')}.patch"))
    if not chosen:
        raise SystemExit(f"No mutant matches {' '.join(patterns)}")
    return [Mutant.load(path) for path in chosen]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the semantic mutants.")
    parser.add_argument("patterns", nargs="*", help="catalog globs or .patch paths")
    mutants = select(parser.parse_args().patterns)

    baseline(sorted({test for mutant in mutants for test in mutant.tests}))
    width = max(len(mutant.name) for mutant in mutants)
    outcomes: dict[str, int] = {}
    for mutant in mutants:
        outcome = check(mutant)
        outcomes[outcome.verdict] = outcomes.get(outcome.verdict, 0) + 1
        sys.stdout.write(
            f"{outcome.verdict:<8}  {mutant.name:<{width}}  {mutant.category:<18}"
            f"  {outcome.seconds:5.1f}s\n"
        )
        if outcome.verdict != "KILLED":
            sys.stdout.write(f"          why: {mutant.why}\n")
        if outcome.detail and outcome.verdict != "KILLED":
            sys.stdout.write(
                "".join(f"          | {line}\n" for line in outcome.detail.splitlines())
            )

    killed = outcomes.get("KILLED", 0)
    summary = ", ".join(f"{count} {verdict.lower()}" for verdict, count in sorted(outcomes.items()))
    sys.stdout.write(f"\n{killed}/{len(mutants)} semantic mutants killed ({summary})\n")
    return 0 if killed == len(mutants) else 1


if __name__ == "__main__":
    sys.exit(main())
