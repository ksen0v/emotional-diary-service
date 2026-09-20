"""Источники, аккаунты, словарь тегов и сделки.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- source ---
    op.execute(
        """
        CREATE TABLE source.connections (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id          UUID NOT NULL,
            provider         TEXT NOT NULL,            -- fake | tmm | binance
            market           TEXT,
            key_encrypted    BYTEA,
            secret_encrypted BYTEA,
            key_version      SMALLINT NOT NULL DEFAULT 1,
            key_masked       TEXT,
            auth_kind        TEXT NOT NULL DEFAULT 'api_key',
            is_active        BOOLEAN NOT NULL DEFAULT false,
            activated_at     TIMESTAMPTZ,
            ingest_from      TIMESTAMPTZ NOT NULL,
            state            TEXT NOT NULL DEFAULT 'connected',
            base_url         TEXT,
            capabilities     JSONB NOT NULL DEFAULT '{}'::jsonb,
            permissions      JSONB,
            last_error       TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (user_id, provider, market)
        )
        """
    )
    # Ровно один активный источник на пользователя — ограничением СУБД,
    # а не проверкой в коде: смешанные метрики из двух источников не означают ничего.
    op.execute(
        """
        CREATE UNIQUE INDEX one_active_source ON source.connections (user_id)
        WHERE is_active
        """
    )

    op.execute(
        """
        CREATE TABLE source.accounts (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id       UUID NOT NULL,
            connection_id UUID NOT NULL REFERENCES source.connections(id) ON DELETE CASCADE,
            external_id   TEXT NOT NULL,
            name          TEXT NOT NULL,
            exchange      TEXT,
            market        TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (connection_id, external_id)
        )
        """
    )

    # Словарь тегов трейдера. Теги узнаём по мере поступления сделок,
    # is_violation по умолчанию false — решает трейдер на экране разметки (шаг 3).
    op.execute(
        """
        CREATE TABLE source.tags (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id       UUID NOT NULL,
            connection_id UUID NOT NULL REFERENCES source.connections(id) ON DELETE CASCADE,
            external_id   TEXT NOT NULL,
            column_key    TEXT NOT NULL DEFAULT 'entry_reason',
            name          TEXT NOT NULL,
            is_violation  BOOLEAN NOT NULL DEFAULT false,
            seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (connection_id, external_id)
        )
        """
    )

    # Лента фейкового источника: то, что он «отдаёт» при следующем запросе.
    # Отдельная таблица, а не список в памяти: фейк должен вести себя как
    # настоящий источник, в том числе переживать перезапуск процесса.
    op.execute(
        """
        CREATE TABLE source.fake_feed (
            id            BIGSERIAL PRIMARY KEY,
            connection_id UUID NOT NULL REFERENCES source.connections(id) ON DELETE CASCADE,
            payload       JSONB NOT NULL,
            close_time    TIMESTAMPTZ NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_fake_feed ON source.fake_feed (connection_id, close_time)")

    # --- trades ---
    op.execute(
        """
        CREATE TABLE trades.trades (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id            UUID NOT NULL,
            account_id         UUID NOT NULL,
            source             TEXT NOT NULL,
            external_id        TEXT NOT NULL,
            symbol             TEXT NOT NULL,
            side               TEXT NOT NULL,
            profit_usd         NUMERIC(18,8) NOT NULL,
            percent            NUMERIC(10,4),
            size_usd           NUMERIC(18,8),
            leverage           NUMERIC(10,4),
            account_return_pct NUMERIC(10,6) NOT NULL,
            duration_sec       INT,
            open_time          TIMESTAMPTZ NOT NULL,
            close_time         TIMESTAMPTZ,
            is_open            BOOLEAN NOT NULL DEFAULT false,
            trading_day        DATE NOT NULL,
            is_significant     BOOLEAN NOT NULL,
            marking            TEXT NOT NULL,
            marked_by          TEXT NOT NULL DEFAULT 'source_tag',
            tags_hash          TEXT NOT NULL,
            raw                JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (user_id, source, external_id),
            CONSTRAINT marking_known CHECK (marking IN ('clean', 'violation', 'unreviewed')),
            CONSTRAINT side_known CHECK (side IN ('long', 'short'))
        )
        """
    )
    op.execute("CREATE INDEX ix_trades_day ON trades.trades (user_id, trading_day)")
    op.execute("CREATE INDEX ix_trades_close ON trades.trades (user_id, close_time DESC)")
    op.execute(
        """
        CREATE INDEX ix_trades_unreviewed ON trades.trades (user_id, marking)
        WHERE marking = 'unreviewed'
        """
    )

    op.execute(
        """
        CREATE TABLE trades.trade_tags (
            trade_id    UUID NOT NULL REFERENCES trades.trades(id) ON DELETE CASCADE,
            external_id TEXT NOT NULL,
            column_key  TEXT NOT NULL,
            name        TEXT NOT NULL,
            PRIMARY KEY (trade_id, external_id)
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError("откат миграций не поддерживается, восстанавливай дамп")
