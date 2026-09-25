"""The critical journeys, as a web or mobile client makes them.

- a new person registers, names the profile, creates a workspace and runs a catalog
- a concurrent edit with a stale `If-Match` is a 412
- people only ever see their own workspaces' data, even with the ids of someone else's
- an invitation brings a teammate in with the role it names, and that role is enforced
- invalid input is a 422, an unknown token a 401, and readiness checks the database
"""

from typing import ClassVar

import pytest

from tests.e2e.conftest import Api


async def test_from_sign_up_to_a_catalog(api: Api) -> None:
    token = await api.sign_up("ana@example.com", name="")

    profile = (await api.request("GET", "/me", token)).json()
    assert profile["onboarding_status"] == "profile_pending"
    blocked = await api.request("GET", "/api/workspaces", token)
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "ONBOARDING_INCOMPLETE"

    named = await api.request("PATCH", "/me", token, json={"display_name": "Ana"})
    assert named.json()["onboarding_status"] == "workspace_pending"
    created = await api.request(
        "POST", "/api/workspaces", token, json={"name": "Acme", "slug": "acme"}
    )
    assert created.status_code == 201

    product = await api.request(
        "POST", "/api/workspaces/acme/products", token, json={"name": "Mug", "price": "9.99"}
    )
    assert product.status_code == 201
    assert product.json()["status"] == "draft"
    listed = (await api.request("GET", "/api/workspaces/acme/products", token)).json()
    assert (listed["total"], [p["name"] for p in listed["items"]]) == (1, ["Mug"])
    assert api.outside.events.actions() == ["created"]


async def test_a_name_given_at_registration_skips_the_profile_step(api: Api) -> None:
    token = await api.sign_up("ana@example.com", name="Ana")

    profile = (await api.request("GET", "/me", token)).json()
    created = await api.request(
        "POST", "/api/workspaces", token, json={"name": "Acme", "slug": "acme"}
    )

    assert (profile["onboarding_status"], profile["missing_profile_fields"]) == (
        "workspace_pending",
        [],
    )
    assert created.status_code == 201


async def test_someone_removed_from_their_only_workspace_can_delete_their_account(
    api: Api,
) -> None:
    owner = await api.onboarded_with_workspace("owner@example.com", "acme")
    roles = (await api.request("GET", "/api/workspaces/acme/roles", owner)).json()
    member_role = next(r["id"] for r in roles if r["name"] == "member")
    await api.request(
        "POST",
        "/api/workspaces/acme/invitations",
        owner,
        json={"email": "leo@example.com", "role_id": member_role},
    )
    leo = await api.sign_up("leo@example.com")
    token = api.outside.mailer.token_sent_to("leo@example.com")
    await api.request("POST", "/api/invitations/accept", leo, json={"token": token})
    leo_id = (await api.request("GET", "/me", leo)).json()["id"]
    removed = await api.request("DELETE", f"/api/workspaces/acme/members/{leo_id}", owner)
    assert removed.status_code == 204
    assert (await api.request("GET", "/me", leo)).json()["onboarding_status"] == (
        "workspace_pending"
    )

    deleted = await api.request("DELETE", "/me", leo)

    assert deleted.status_code == 204
    assert api.outside.identity.deleted == [leo_id]


async def test_a_stale_if_match_is_a_412(api: Api) -> None:
    token = await api.onboarded_with_workspace("ana@example.com", "acme")
    created = (
        await api.request("POST", "/api/workspaces/acme/products", token, json={"name": "Mug"})
    ).json()
    url = f"/api/workspaces/acme/products/{created['id']}"
    version = created["updated_at"]
    await api.request("PATCH", url, token, json={"name": "Cup"}, headers={"If-Match": version})

    stale = await api.request(
        "PATCH", url, token, json={"name": "Bowl"}, headers={"If-Match": version}
    )

    assert stale.status_code == 412
    assert (await api.request("GET", url, token)).json()["name"] == "Cup"


async def test_cursor_pages_walk_the_whole_catalog(api: Api) -> None:
    token = await api.onboarded_with_workspace("ana@example.com", "acme")
    for name in ("A", "B", "C", "D", "E"):
        await api.request("POST", "/api/workspaces/acme/products", token, json={"name": name})

    names: list[str] = []
    cursor = ""
    while cursor is not None:
        page = (
            await api.request(
                "GET",
                "/api/workspaces/acme/products",
                token,
                params={"cursor": cursor, "size": 2, "order_by": "name"},
            )
        ).json()
        names += [p["name"] for p in page["items"]]
        cursor = page["next_cursor"]

    assert names == ["A", "B", "C", "D", "E"]


class TestTenantIsolation:
    @pytest.fixture
    async def tenants(self, api: Api) -> tuple[str, str, str]:
        """Ana's product in `acme`; Bea, who owns `bravo`; the product's id."""
        ana = await api.onboarded_with_workspace("ana@example.com", "acme")
        bea = await api.onboarded_with_workspace("bea@example.com", "bravo")
        product = await api.request(
            "POST", "/api/workspaces/acme/products", ana, json={"name": "Secret"}
        )
        return ana, bea, product.json()["id"]

    async def test_another_workspace_does_not_exist_for_outsiders(
        self, api: Api, tenants: tuple[str, str, str]
    ) -> None:
        _, bea, product_id = tenants

        for url in ("/api/workspaces/acme", f"/api/workspaces/acme/products/{product_id}"):
            assert (await api.request("GET", url, bea)).status_code == 404

    async def test_a_product_id_does_not_travel_between_workspaces(
        self, api: Api, tenants: tuple[str, str, str]
    ) -> None:
        _, bea, product_id = tenants
        url = f"/api/workspaces/bravo/products/{product_id}"

        responses = [
            await api.request("GET", url, bea),
            await api.request("PATCH", url, bea, json={"name": "Mine now"}),
            await api.request("DELETE", url, bea),
        ]

        assert [r.status_code for r in responses] == [404, 404, 404]

    async def test_lists_only_hold_the_own_workspace(
        self, api: Api, tenants: tuple[str, str, str]
    ) -> None:
        ana, bea, _ = tenants

        theirs = (await api.request("GET", "/api/workspaces/bravo/products", bea)).json()
        workspaces = (await api.request("GET", "/api/workspaces", bea)).json()

        assert theirs["total"] == 0
        assert [w["slug"] for w in workspaces] == ["bravo"]
        still = (await api.request("GET", "/api/workspaces/acme/products", ana)).json()
        assert [p["name"] for p in still["items"]] == ["Secret"]


async def test_an_invitation_brings_a_teammate_in_with_its_role(api: Api) -> None:
    owner = await api.onboarded_with_workspace("owner@example.com", "acme")
    roles = (await api.request("GET", "/api/workspaces/acme/roles", owner)).json()
    viewer_role = next(r["id"] for r in roles if r["name"] == "viewer")
    invited = await api.request(
        "POST",
        "/api/workspaces/acme/invitations",
        owner,
        json={"email": "vic@example.com", "role_id": viewer_role},
    )
    assert invited.status_code == 201
    token = api.outside.mailer.token_sent_to("vic@example.com")

    preview = (await api.request("GET", f"/api/invitations/{token}")).json()
    assert (preview["workspace_slug"], preview["role_name"]) == ("acme", "viewer")
    vic = await api.sign_up("vic@example.com", name="Vic")
    accepted = await api.request("POST", "/api/invitations/accept", vic, json={"token": token})
    assert accepted.status_code == 200

    reads = await api.request("GET", "/api/workspaces/acme/products", vic)
    writes = await api.request("POST", "/api/workspaces/acme/products", vic, json={"name": "Nope"})
    deletes = await api.request("DELETE", "/api/workspaces/acme", vic)
    assert (reads.status_code, writes.status_code, deletes.status_code) == (200, 403, 403)


class TestProductPermissions:
    """Each product route asks for its own permission: a viewer reads, a member writes."""

    ROUTES: ClassVar = [
        ("POST", "", "products.create"),
        ("PATCH", "/{id}", "products.update"),
        ("DELETE", "/{id}", "products.delete"),
        ("POST", "/{id}/images/uploads", "products.update"),
        ("POST", "/{id}/images/confirm", "products.update"),
        ("PUT", "/{id}/images/order", "products.update"),
        ("DELETE", "/{id}/images/some-image", "products.update"),
    ]
    BODIES: ClassVar = {
        "POST": {"name": "Mug", "content_type": "image/png", "key": "abc"},
        "PATCH": {"name": "Cup"},
        "PUT": {"image_ids": ["x"]},
    }

    @pytest.fixture
    async def setup(self, api: Api) -> tuple[str, str, str]:
        """The owner's token, a viewer's token and a product id."""
        owner = await api.onboarded_with_workspace("owner@example.com", "acme")
        roles = (await api.request("GET", "/api/workspaces/acme/roles", owner)).json()
        viewer_role = next(r["id"] for r in roles if r["name"] == "viewer")
        await api.request(
            "POST",
            "/api/workspaces/acme/invitations",
            owner,
            json={"email": "vic@example.com", "role_id": viewer_role},
        )
        viewer = await api.sign_up("vic@example.com")
        token = api.outside.mailer.token_sent_to("vic@example.com")
        await api.request("POST", "/api/invitations/accept", viewer, json={"token": token})
        product = await api.request(
            "POST", "/api/workspaces/acme/products", owner, json={"name": "Mug"}
        )
        return owner, viewer, product.json()["id"]

    @pytest.mark.parametrize(("method", "path", "permission"), ROUTES)
    async def test_a_viewer_is_refused(
        self, api: Api, setup: tuple[str, str, str], method: str, path: str, permission: str
    ) -> None:
        _, viewer, product_id = setup
        url = "/api/workspaces/acme/products" + path.format(id=product_id)
        body = self.BODIES.get(method)

        response = await api.request(method, url, viewer, json=body)

        assert response.status_code == 403, f"{method} {path} must need {permission}"

    async def test_a_viewer_reads(self, api: Api, setup: tuple[str, str, str]) -> None:
        _, viewer, product_id = setup

        listed = await api.request("GET", "/api/workspaces/acme/products", viewer)
        one = await api.request("GET", f"/api/workspaces/acme/products/{product_id}", viewer)

        assert (listed.status_code, one.status_code) == (200, 200)


async def test_edge_answers(api: Api) -> None:
    token = await api.onboarded_with_workspace("ana@example.com", "acme")

    invalid = await api.request(
        "POST", "/api/workspaces/acme/products", token, json={"name": "", "price": "-1"}
    )
    unknown = await api.request("GET", "/me", "made-up-token")
    ready = await api.request("GET", "/health/ready")

    assert invalid.status_code == 422
    assert unknown.status_code == 401
    assert ready.json() == {"status": "ready"}
