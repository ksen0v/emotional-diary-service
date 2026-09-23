"""Стрики: зачёт дня и состояние серии.

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # По дню: зачтён или нет и почему. Причина хранится рядом с расчётом,
    # а не собирается заново при показе — иначе однажды на экране окажется
    # одна причина, а в расчёте другая, и объяснить это будет нечем.
    op.execute(
        """
        CREATE TABLE streaks.day_marks (
            user_id UUID NOT NULL,
            day     DATE NOT NULL,
            counted BOOLEAN NOT NULL,
            reason  TEXT NOT NULL,
            PRIMARY KEY (user_id, day),
            CONSTRAINT reason_known CHECK (reason IN (
                'ok', 'violation', 'breach', 'no_review', 'no_check',
                'no_entry', 'frozen'
            ))
        )
        """
    )

    # Состояние серии. Считается из day_marks и хранится отдельно, потому что
    # на него смотрит каждый экран: пересчитывать всю историю на каждый запрос
    # главной страницы — плохая идея с первого дня.
    op.execute(
        """
        CREATE TABLE streaks.state (
            user_id       UUID PRIMARY KEY,
            current       SMALLINT NOT NULL DEFAULT 0,
            best          SMALLINT NOT NULL DEFAULT 0,
            last_day      DATE,
            freezes_month DATE,
            freezes_used  SMALLINT NOT NULL DEFAULT 0,
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError("откат миграций не поддерживается, восстанавливай дамп")
