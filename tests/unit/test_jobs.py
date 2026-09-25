from typing import Any

import pytest

from app import jobs


def test_an_unknown_job_fails_the_invocation() -> None:
    with pytest.raises(ValueError, match="Unknown job"):
        jobs.handler({"job": "nope"}, None)
    with pytest.raises(ValueError, match="Unknown job"):
        jobs.handler({}, None)


def test_a_known_job_runs_and_returns_its_result(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake() -> dict[str, Any]:
        return {"purged_workspaces": 0}

    monkeypatch.setitem(jobs.JOBS, "purge-deleted-workspaces", fake)
    assert jobs.handler({"job": "purge-deleted-workspaces"}, None) == {"purged_workspaces": 0}


def test_a_failing_job_fails_the_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken() -> dict[str, Any]:
        raise RuntimeError("database unreachable")

    monkeypatch.setitem(jobs.JOBS, "purge-deleted-workspaces", broken)
    with pytest.raises(RuntimeError, match="database unreachable"):
        jobs.handler({"job": "purge-deleted-workspaces"}, None)
