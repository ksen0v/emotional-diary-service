"""Схемы модулей и событийная шина на outbox.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMAS = (
    "identity",
    "source",
    "trades",
    "daybook",
    "rules",
    "incidents",
    "streaks",
    "notify",
    "events",
)


def upgrade() -> None:
    for name in SCHEMAS:
        op.execute(f'CREATE SCHEMA IF NOT EXISTS "{name}"')

    # gen_random_uuid() понадобится почти во всех модулях
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        """
        CREATE TABLE events.outbox (
            id          BIGSERIAL PRIMARY KEY,
            event_type  TEXT NOT NULL,
            payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
            dedup_key   TEXT UNIQUE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_outbox_type ON events.outbox (event_type, id)")

    op.execute(
        """
        CREATE TABLE events.cursors (
            consumer   TEXT PRIMARY KEY,
            last_id    BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE events.dead_letters (
            id         BIGSERIAL PRIMARY KEY,
            consumer   TEXT NOT NULL,
            event_id   BIGINT NOT NULL,
            error      TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    # Откат не поддерживаем осознанно (Архитектура ч.2 §5.9):
    # возврат назад делается восстановлением дампа.
    raise NotImplementedError("откат миграций не поддерживается, восстанавливай дамп")
