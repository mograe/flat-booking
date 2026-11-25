"""Add whitelist metadata and extend the standard booking duration."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4c8e2e115f39"
down_revision: str = "ccf47a25f067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("registered_at", sa.DateTime()))
    op.add_column("users", sa.Column("whitelist_added_at", sa.DateTime()))
    op.add_column("users", sa.Column("whitelist_comment", sa.String(200)))
    op.add_column(
        "studio_settings",
        sa.Column("whitelist_initialized", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(
        """
        UPDATE users SET registered_at = created_at
        WHERE first_name != 'Участник ' || telegram_id
           OR display_name != first_name OR last_name IS NOT NULL OR username IS NOT NULL
        """
    )
    op.execute(
        """
        WITH membership AS (
            SELECT id, coalesce(
                (SELECT min(entry.created_at) FROM audit_log AS entry
                 WHERE entry.action = 'member_added' AND entry.entity_type = 'user'
                   AND entry.entity_id = users.id),
                (SELECT min(request.reviewed_at) FROM access_requests AS request
                 WHERE request.status = 'approved' AND request.user_id = users.id)
            ) AS added_at
            FROM users WHERE role IN ('member', 'admin')
        )
        UPDATE users
        SET whitelist_added_at = coalesce(
            (SELECT added_at FROM membership WHERE membership.id = users.id), created_at
        )
        WHERE role = 'member' OR id IN (SELECT id FROM membership WHERE added_at IS NOT NULL)
        """
    )
    op.execute(
        """
        UPDATE studio_settings
        SET options = json_set(options, '$.max_booking_minutes', 1440)
        WHERE json_extract(options, '$.max_booking_minutes') = 240
        """
    )


def downgrade() -> None:
    op.drop_column("studio_settings", "whitelist_initialized")
    op.drop_column("users", "whitelist_comment")
    op.drop_column("users", "whitelist_added_at")
    op.drop_column("users", "registered_at")
