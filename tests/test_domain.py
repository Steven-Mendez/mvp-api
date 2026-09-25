from datetime import timedelta
from decimal import Decimal

import pytest

from app.domain.access import ONBOARDED, onboarding_needed_for
from app.domain.errors import (
    ConflictError,
    ForbiddenError,
    GoneError,
    InvalidError,
    NotFoundError,
    OnboardingIncompleteError,
    PreconditionFailedError,
)
from app.domain.product import (
    MAX_IMAGES_PER_PRODUCT,
    Product,
    ProductQuotas,
    ProductStatus,
    parse_order,
    readable_size,
)
from app.domain.user import User
from app.domain.workspace import Invitation, Role, Workspace, is_valid_slug, validate_permissions


def _product() -> Product:
    return Product.create(workspace_id="w1", name="Mug", price=Decimal("9.99"))


def _add_image(product: Product, n: int) -> None:
    product.add_image(
        key=f"media/{n}.webp",
        thumb_key=None,
        upload_key=f"u{n}",
        content_type="image/webp",
        size_bytes=1,
    )


class TestProduct:
    def test_update_moves_the_version_only_on_a_real_change(self) -> None:
        product = _product()
        version = product.updated_at
        product.update({"name": "Mug"})
        assert product.updated_at == version
        product.update({"status": ProductStatus.active})
        assert product.updated_at > version

    def test_if_match_protects_against_lost_updates(self) -> None:
        product = _product()
        with pytest.raises(PreconditionFailedError):
            product.ensure_unchanged_since(product.updated_at - timedelta(seconds=1))

    def test_rejects_a_negative_price(self) -> None:
        with pytest.raises(InvalidError):
            Product.create(workspace_id="w1", name="Mug", price=Decimal(-1))

    def test_images_keep_contiguous_positions(self) -> None:
        product = _product()
        for n in range(3):
            _add_image(product, n)
        product.remove_image(product.images[1].id)
        assert [image.position for image in product.images] == [0, 1]
        with pytest.raises(NotFoundError):
            product.remove_image("missing")

    def test_reorder_must_list_every_image(self) -> None:
        product = _product()
        for n in range(2):
            _add_image(product, n)
        first, second = (image.id for image in product.images)
        product.reorder_images([second, first])
        assert product.images[0].id == second
        with pytest.raises(InvalidError):
            product.reorder_images([first])

    def test_image_limit(self) -> None:
        product = _product()
        for n in range(MAX_IMAGES_PER_PRODUCT):
            _add_image(product, n)
        with pytest.raises(ConflictError):
            _add_image(product, 99)

    def test_quotas(self) -> None:
        quotas = ProductQuotas(max_products=1, max_storage_bytes=1024)
        with pytest.raises(ConflictError, match=r"limit of 1 product$"):
            quotas.ensure_room_for_product(1)
        with pytest.raises(ConflictError, match="1 KB"):
            quotas.ensure_room_for_bytes(1000, 25)

    @pytest.mark.parametrize(
        ("size", "expected"),
        [(1, "1 byte"), (512, "512 bytes"), (1536, "1.5 KB"), (10 * 1024**3, "10 GB")],
    )
    def test_readable_size(self, size: int, expected: str) -> None:
        assert readable_size(size) == expected

    def test_order_by(self) -> None:
        assert parse_order(None) == ("created_at", True)
        assert parse_order("name") == ("name", False)
        with pytest.raises(InvalidError):
            parse_order("-sku")


class TestWorkspace:
    def test_owner_may_do_anything_and_others_never_owner_actions(self) -> None:
        workspace = Workspace.create(name="Acme", slug="acme", owner_id="owner")
        assert workspace.grants("owner", "workspace.delete") is True
        assert workspace.grants("member", "workspace.delete") is False
        assert workspace.grants("member", "products.read") is True
        assert workspace.grants("member", "products.create") is None

    def test_soft_delete_frees_the_slug(self) -> None:
        workspace = Workspace.create(name="Acme", slug="acme", owner_id="owner")
        workspace.soft_delete()
        assert workspace.is_deleted
        assert workspace.slug.startswith("deleted-")

    @pytest.mark.parametrize(
        ("slug", "valid"), [("acme", True), ("a", False), ("-acme", False), ("api", False)]
    )
    def test_slugs(self, slug: str, valid: bool) -> None:
        assert is_valid_slug(slug) is valid

    def test_permissions_cannot_include_owner_only_actions(self) -> None:
        with pytest.raises(ForbiddenError):
            validate_permissions(["workspace.delete"])
        with pytest.raises(InvalidError):
            validate_permissions(["nope"])

    def test_only_roles_managers_invite_with_other_roles(self) -> None:
        Role.system("w1", "member").ensure_invitable(may_manage_roles=False)
        with pytest.raises(ForbiddenError):
            Role.system("w1", "admin").ensure_invitable(may_manage_roles=False)
        with pytest.raises(ForbiddenError):
            Role.custom("w1", "owner")


class TestInvitation:
    def test_lifecycle(self) -> None:
        invitation, token = Invitation.issue(
            workspace_id="w1", email="Ana@Example.com", role_id="r1", invited_by="u1"
        )
        assert token not in invitation.token_hash
        with pytest.raises(ForbiddenError):
            invitation.accept("u2", "someone@example.com")
        invitation.accept("u2", "ana@example.com")
        assert invitation.current_status == "accepted"
        with pytest.raises(GoneError):
            invitation.ensure_usable()

    def test_expires(self) -> None:
        invitation, _ = Invitation.issue(
            workspace_id="w1", email="a@b.c", role_id="r", invited_by="u"
        )
        invitation.expires_at -= timedelta(days=8)
        assert invitation.current_status == "expired"


class TestOnboarding:
    def test_steps(self) -> None:
        user = User.new("sub", "Ana@Example.com")
        assert user.email == "ana@example.com"
        assert user.missing_profile_fields == ["display_name"]
        with pytest.raises(OnboardingIncompleteError):
            user.ensure_onboarded(onboarding_needed_for("workspace.read"))
        user.rename("Ana", belongs_to_a_workspace=False)
        assert user.onboarding_status == "workspace_pending"
        user.created_a_workspace()
        user.ensure_onboarded(ONBOARDED)
        user.left_every_workspace()
        assert user.onboarding_status == "workspace_pending"
