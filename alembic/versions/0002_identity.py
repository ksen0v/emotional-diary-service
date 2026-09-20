"""Модуль identity: пользователи, настройки, сессии, флаги модулей.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE identity.users (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email         TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE identity.settings (
            user_id           UUID PRIMARY KEY REFERENCES identity.users(id) ON DELETE CASCADE,
            timezone          TEXT NOT NULL DEFAULT 'Europe/Moscow',
            day_cutoff        TIME NOT NULL DEFAULT '00:00',
            pass_score        SMALLINT NOT NULL DEFAULT 19,
            min_score         SMALLINT NOT NULL DEFAULT 13,
            significance_pct  NUMERIC(5,2) NOT NULL DEFAULT 0.50,
            active_account_id UUID,
            telegram_enabled  BOOLEAN NOT NULL DEFAULT true,
            shadow_mode       BOOLEAN NOT NULL DEFAULT false,
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT pass_above_min CHECK (pass_score > min_score),
            CONSTRAINT scores_in_range CHECK (
                pass_score BETWEEN 1 AND 25 AND min_score BETWEEN 1 AND 25
            ),
            CONSTRAINT significance_in_range CHECK (significance_pct BETWEEN 0 AND 10)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE identity.sessions (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id      UUID NOT NULL REFERENCES identity.users(id) ON DELETE CASCADE,
            token_hash   BYTEA NOT NULL UNIQUE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at   TIMESTAMPTZ NOT NULL,
            user_agent   TEXT,
            ip           INET,
            revoked_at   TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_sessions_live ON identity.sessions (user_id)
        WHERE revoked_at IS NULL
        """
    )

    op.execute(
        """
        CREATE TABLE identity.module_flags (
            user_id UUID NOT NULL REFERENCES identity.users(id) ON DELETE CASCADE,
            module  TEXT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT true,
            reason  TEXT,
            PRIMARY KEY (user_id, module)
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError("откат миграций не поддерживается, восстанавливай дамп")
