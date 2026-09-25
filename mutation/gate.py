"""The mutation score of the core (app/domain, app/application), as a pass/fail gate.

    uv run python mutation/gate.py                           # every core module
    uv run python mutation/gate.py --changed-since origin/main  # only what a branch touched

Runs mutmut on the chosen modules, then scores them: killed (or timed out) mutants over
all mutants, leaving out the reviewed equivalents in mutation/equivalents.txt. Any
other survivor is listed as unreviewed with the command that shows it. Fails below
`--min` (per cent), and never with no core module to score.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ("app/domain/", "app/application/")
DETECTED = {"killed", "timeout", "caught by type check"}
_RESULT = re.compile(r"^\s*(?P<name>\S+): (?P<status>.+)$")


def _git(*args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def changed_modules(ref: str) -> list[str]:
    """Core modules changed since `ref`, committed or not: `app.domain.product`."""
    base = _git("merge-base", ref, "HEAD").strip()
    names = _git("diff", "--name-only", base, "--", *CORE).split()
    return sorted(
        name.removesuffix(".py").replace("/", ".")
        for name in names
        if name.endswith(".py") and not name.endswith("__init__.py") and (ROOT / name).exists()
    )


def equivalents() -> set[str]:
    lines = (ROOT / "mutation" / "equivalents.txt").read_text().splitlines()
    return {line.split("#")[0].strip() for line in lines if line.split("#")[0].strip()}


def results() -> dict[str, str]:
    output = subprocess.run(
        [sys.executable, "-m", "mutmut", "results", "--all", "true"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {m["name"]: m["status"] for line in output.splitlines() if (m := _RESULT.match(line))}


def main() -> int:
    parser = argparse.ArgumentParser(description="Score the core's mutants.")
    parser.add_argument("--changed-since", metavar="REF", help="only modules changed since REF")
    parser.add_argument("--min", type=float, default=95.0, help="lowest passing score (%%)")
    args = parser.parse_args()

    modules: list[str] = changed_modules(args.changed_since) if args.changed_since else []
    if args.changed_since and not modules:
        sys.stdout.write("No core module changed: nothing to mutate.\n")
        return 0
    globs = [f"{module}.*" for module in modules]
    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "mutmut", "run", *globs],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        check=True,
    )

    reviewed = equivalents()
    scored = {
        name: status
        for name, status in results().items()
        if name not in reviewed
        and (not modules or name.startswith(tuple(f"{m}." for m in modules)))
    }
    if not scored:
        sys.stdout.write("No mutants to score.\n")
        return 0
    detected = sum(status in DETECTED for status in scored.values())
    score = 100 * detected / len(scored)
    unreviewed = sorted(name for name, status in scored.items() if status not in DETECTED)

    scope = ", ".join(modules) if modules else "app/domain and app/application"
    sys.stdout.write(
        f"Mutation score of {scope}: {score:.1f}% ({detected}/{len(scored)} detected, "
        f"{len(reviewed)} reviewed equivalents left out)\n"
    )
    for name in unreviewed:
        sys.stdout.write(f"  {scored[name]:<11} uv run mutmut show '{name}'\n")
    if unreviewed:
        sys.stdout.write(
            "Kill each with a test, or add it to mutation/equivalents.txt with the reason.\n"
        )
    if score < args.min:
        sys.stdout.write(f"Below the {args.min:.0f}% minimum.\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
