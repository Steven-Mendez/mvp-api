"""Repository contract tests: every test runs against the SQL repositories on a real
Postgres *and* against the in-memory fakes the unit tests use. A behavior the SQL side
has and the fake lacks (or the reverse) fails here, before a unit test can lie about it.
"""

from dataclasses import dataclass

import pytest

from app.application.ports import UnitOfWork
from app.domain.access import SYSTEM_ROLES
from app.domain.user import User
from app.domain.workspace import Member, Role, Workspace
from tests import factories
from tests.fakes import FakeUnitOfWork


@pytest.fixture(params=["sql", "fake"])
def any_uow(request: pytest.FixtureRequest) -> UnitOfWork:
    if request.param == "fake":
        return FakeUnitOfWork()
    uow: UnitOfWork = request.getfixturevalue("uow")
    return uow


@dataclass
class Tenant:
    workspace: Workspace
    roles: dict[str, Role]


@dataclass
class Seed:
    uow: UnitOfWork

    async def user(self, email: str, name: str = "Someone") -> User:
        user = factories.user(user_id=f"sub-{email}", email=email, display_name=name)
        self.uow.users.add(user)
        await self.uow.flush()
        return user

    async def tenant(self, slug: str, owner: User) -> Tenant:
        """A workspace with the system roles and its owner as a member."""
        workspace = factories.workspace(name=slug.title(), slug=slug, owner_id=owner.id)
        self.uow.workspaces.add(workspace)
        roles: dict[str, Role] = {}
        for name, permissions in SYSTEM_ROLES.items():
            roles[name] = Role.system(workspace.id, name)
            await self.uow.roles.add(roles[name], permissions)
        self.uow.members.add(Member.join(workspace.id, owner.id, roles["owner"].id))
        await self.uow.flush()
        return Tenant(workspace, roles)

    async def member(self, tenant: Tenant, user: User, role: str = "member") -> None:
        self.uow.members.add(Member.join(tenant.workspace.id, user.id, tenant.roles[role].id))
        await self.uow.flush()


@pytest.fixture
def seed(any_uow: UnitOfWork) -> Seed:
    return Seed(any_uow)
