"""Дневник, комментарии и пост-сессионный разбор.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Запись дневника на любом уровне. Один уровень и одно начало периода —
    # одна запись: вторая запись за тот же день означала бы два разных мнения
    # об одном дне, и было бы непонятно, какое из них считать.
    op.execute(
        """
        CREATE TABLE daybook.entries (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id        UUID NOT NULL,
            level          TEXT NOT NULL,          -- day | week | month
            period_start   DATE NOT NULL,
            period_end     DATE NOT NULL,
            score          SMALLINT,
            status         TEXT,
            body           TEXT,
            -- Срок правки фиксируется при создании и не пересчитывается: смена
            -- таймзоны не должна открывать заново уже закрытую для правок запись.
            editable_until TIMESTAMPTZ NOT NULL,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (user_id, level, period_start),
            CONSTRAINT level_known CHECK (level IN ('day', 'week', 'month')),
            CONSTRAINT score_range CHECK (score IS NULL OR score BETWEEN 1 AND 5)
        )
        """
    )
    op.execute("CREATE INDEX ix_entries_period ON daybook.entries (user_id, level, period_start)")

    # Ментальные теги — свободный текст, а не справочник. Пресеты сервис
    # предлагает, но не ограничивает ими: свой словарь состояний у каждого свой,
    # а таблица-справочник превратила бы «допиши своё» в правку схемы.
    op.execute(
        """
        CREATE TABLE daybook.entry_tags (
            entry_id UUID NOT NULL REFERENCES daybook.entries(id) ON DELETE CASCADE,
            tag      TEXT NOT NULL,
            PRIMARY KEY (entry_id, tag)
        )
        """
    )

    # Комментарии не ограничены по времени и не редактируются: это способ
    # дописать к старой записи, не переписав то, что было сказано тогда (ТЗ 9.2).
    op.execute(
        """
        CREATE TABLE daybook.entry_comments (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            entry_id   UUID NOT NULL REFERENCES daybook.entries(id) ON DELETE CASCADE,
            body       TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_entry_comments ON daybook.entry_comments (entry_id, created_at)")

    # Разбор — один на день, как и чек. Оценка исполнения, не результата.
    op.execute(
        """
        CREATE TABLE daybook.session_reviews (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id         UUID NOT NULL,
            day             DATE NOT NULL,
            plan_followed   TEXT NOT NULL,          -- yes | partial | no
            pull_text       TEXT,
            execution_score SMALLINT,
            takeaway        TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (user_id, day),
            CONSTRAINT plan_followed_known
                CHECK (plan_followed IN ('yes', 'partial', 'no')),
            CONSTRAINT execution_score_range
                CHECK (execution_score IS NULL OR execution_score BETWEEN 1 AND 5)
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError("откат миграций не поддерживается, восстанавливай дамп")
