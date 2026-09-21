"""Учёт сверок и лимитов провайдера.

Обе таблицы из схемы Архитектуры ч.1 §6. Появляются вместе с настоящим
источником: у фейка нет ни лимитов, ни сети, и до шага 4 записывать было нечего.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Журнал сверок. Нужен не для отчётности: без него на вопрос «почему сделки
    # не приехали» нет ответа, кроме логов сервера, которых у трейдера нет.
    op.execute(
        """
        CREATE TABLE source.reconcile_runs (
            id            BIGSERIAL PRIMARY KEY,
            connection_id UUID NOT NULL REFERENCES source.connections(id) ON DELETE CASCADE,
            kind          TEXT NOT NULL,          -- startup | after_reconnect | scheduled | manual
            window_from   TIMESTAMPTZ,
            window_to     TIMESTAMPTZ,
            started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at   TIMESTAMPTZ,
            status        TEXT NOT NULL,          -- ok | error | rate_limited
            trades_seen   INT NOT NULL DEFAULT 0,
            trades_new    INT NOT NULL DEFAULT 0,
            error         TEXT
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_reconcile_runs ON source.reconcile_runs
        (connection_id, started_at DESC)
        """
    )

    # Последнее, что мы прочитали из заголовков x-ratelimit-*. Числа лимитов
    # TMM не документированы, поэтому храним факт, а не предположение (§4.6).
    op.execute(
        """
        CREATE TABLE source.rate_limits (
            connection_id UUID PRIMARY KEY REFERENCES source.connections(id) ON DELETE CASCADE,
            limit_value   INT,
            remaining     INT,
            reset_at      TIMESTAMPTZ,
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError("откат миграций не поддерживается, восстанавливай дамп")
