"""The use cases wired to the in-memory fakes: no database, no network, milliseconds."""

from dataclasses import dataclass, field

import pytest

from app.application.accounts import AccountService
from app.application.products import ProductService
from app.application.purge import WorkspacePurge
from app.application.workspaces import WorkspaceService
from app.domain.product import ProductQuotas
from app.domain.workspace import Workspace
from tests.fakes import (
    FakeEventPublisher,
    FakeIdentityProvider,
    FakeMailer,
    FakeMediaStorage,
    FakeUnitOfWork,
)

WEB = "https://app.example.com"


@dataclass
class World:
    uow: FakeUnitOfWork = field(default_factory=FakeUnitOfWork)
    identity: FakeIdentityProvider = field(default_factory=FakeIdentityProvider)
    media: FakeMediaStorage = field(default_factory=FakeMediaStorage)
    mailer: FakeMailer = field(default_factory=FakeMailer)
    events: FakeEventPublisher = field(default_factory=FakeEventPublisher)
    quotas: ProductQuotas = field(
        default_factory=lambda: ProductQuotas(max_products=3, max_storage_bytes=10_000)
    )

    @property
    def accounts(self) -> AccountService:
        return AccountService(
            self.uow, self.identity, self.media, WorkspacePurge(self.uow, self.media)
        )

    @property
    def workspaces(self) -> WorkspaceService:
        return WorkspaceService(self.uow, self.media, self.mailer, WEB)

    @property
    def products(self) -> ProductService:
        return ProductService(self.uow, self.media, self.events, self.quotas)

    @property
    def purge(self) -> WorkspacePurge:
        return WorkspacePurge(self.uow, self.media)

    async def user(self, email: str, name: str = "Someone") -> str:
        """An account that signed in once and named its profile (`name=""` stops there)."""
        token = self.identity.sign_in(email)
        sub = self.identity.tokens[token][0]
        await self.accounts.ensure_account(sub, token)
        if name:
            await self.accounts.rename(sub, name)
        return sub

    async def workspace(self, owner_id: str, slug: str = "acme") -> Workspace:
        return (await self.workspaces.create(slug.title(), slug, owner_id)).workspace

    async def role_id(self, workspace: Workspace, name: str) -> str:
        role = await self.uow.roles.by_name(workspace.id, name)
        assert role is not None
        return role.id

    async def join(self, workspace: Workspace, email: str, role: str = "member") -> str:
        """A new account that joined `workspace` through an invitation with `role`."""
        user_id = await self.user(email)
        invited = await self.workspaces.invite(
            workspace.id, email, await self.role_id(workspace, role), workspace.owner_id
        )
        token = invited.url.rsplit("token=", 1)[1]
        await self.workspaces.accept_invitation(token, user_id)
        return user_id


@pytest.fixture
def world() -> World:
    return World()
