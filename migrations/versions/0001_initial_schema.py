"""Initial schema.

Revision ID: 0001
Revises:
Create Date: 2026-09-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# One statement per entry: Aurora DSQL runs each DDL in its own transaction. The tables
# are new and empty, so their unique constraints are declared inline; indexes on tables
# that may hold data later must use CREATE INDEX ASYNC.
STATEMENTS: list[str] = [
    """
    CREATE TABLE users (
        id text PRIMARY KEY,
        email text NOT NULL UNIQUE,
        display_name text NOT NULL,
        avatar_url text,
        avatar_key text,
        onboarding_status text NOT NULL
            CHECK (onboarding_status IN ('profile_pending', 'workspace_pending', 'completed')),
        created_at timestamptz NOT NULL
    )
    """,
    """
    CREATE TABLE workspaces (
        id text PRIMARY KEY,
        name text NOT NULL,
        slug text NOT NULL UNIQUE,
        owner_id text NOT NULL,
        logo_url text,
        logo_key text,
        created_at timestamptz NOT NULL,
        deleted_at timestamptz
    )
    """,
    """
    CREATE TABLE roles (
        id text PRIMARY KEY,
        workspace_id text NOT NULL,
        name text NOT NULL,
        is_system boolean NOT NULL,
        is_protected boolean NOT NULL,
        UNIQUE (workspace_id, name)
    )
    """,
    """
    CREATE TABLE role_permissions (
        role_id text NOT NULL,
        permission text NOT NULL,
        PRIMARY KEY (role_id, permission)
    )
    """,
    """
    CREATE TABLE members (
        workspace_id text NOT NULL,
        user_id text NOT NULL,
        role_id text NOT NULL,
        joined_at timestamptz NOT NULL,
        PRIMARY KEY (workspace_id, user_id)
    )
    """,
    """
    CREATE TABLE invitations (
        id text PRIMARY KEY,
        workspace_id text NOT NULL,
        email text NOT NULL,
        role_id text NOT NULL,
        token_hash text NOT NULL UNIQUE,
        status text NOT NULL CHECK (status IN ('pending', 'accepted', 'revoked')),
        invited_by text NOT NULL,
        accepted_by text,
        expires_at timestamptz NOT NULL,
        created_at timestamptz NOT NULL
    )
    """,
    """
    CREATE TABLE products (
        id text PRIMARY KEY,
        workspace_id text NOT NULL,
        name text NOT NULL,
        sku text,
        price numeric(10, 2) NOT NULL CHECK (price >= 0),
        status varchar(16) NOT NULL CHECK (status IN ('active', 'draft', 'archived')),
        description text,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL
    )
    """,
    """
    CREATE TABLE product_images (
        id text PRIMARY KEY,
        product_id text NOT NULL,
        workspace_id text NOT NULL,
        key text NOT NULL,
        thumb_key text,
        upload_key text,
        content_type text NOT NULL,
        size_bytes bigint NOT NULL,
        position integer NOT NULL,
        created_at timestamptz NOT NULL,
        UNIQUE (product_id, upload_key)
    )
    """,
    "CREATE INDEX ASYNC members_user_idx ON members (user_id)",
    "CREATE INDEX ASYNC roles_workspace_idx ON roles (workspace_id)",
    "CREATE INDEX ASYNC invitations_workspace_idx ON invitations (workspace_id, created_at)",
    "CREATE INDEX ASYNC products_workspace_created_idx ON products (workspace_id, created_at, id)",
    "CREATE INDEX ASYNC products_workspace_name_idx ON products (workspace_id, name, id)",
    "CREATE INDEX ASYNC product_images_product_idx ON product_images (product_id, position)",
    "CREATE INDEX ASYNC product_images_workspace_idx ON product_images (workspace_id)",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Roll forward with a new migration")
