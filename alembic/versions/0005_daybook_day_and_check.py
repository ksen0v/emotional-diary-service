"""Торговый день и пре-маркет чек.

Дневник, комментарии и пост-сессионный разбор появятся отдельной миграцией
на шаге 6: их таблицы из схемы Архитектуры ч.1 §6 здесь сознательно не создаются,
чтобы миграции шли по шагам и в базе не лежало пустых таблиц без кода.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Одна строка на торговый день трейдера. Ключ составной, без своего id:
    # день пользователя существует ровно один, и суррогатный ключ позволил бы
    # завести второй такой же.
    op.execute(
        """
        CREATE TABLE daybook.trading_days (
            user_id           UUID NOT NULL,
            day               DATE NOT NULL,
            admission         TEXT,            -- green | red | denied | NULL (чека не было)
            check_score       SMALLINT,
            session_opened_at TIMESTAMPTZ,
            session_closed_at TIMESTAMPTZ,
            review_state      TEXT NOT NULL DEFAULT 'none',  -- none | pending | done
            PRIMARY KEY (user_id, day),
            CONSTRAINT admission_known
                CHECK (admission IS NULL OR admission IN ('green', 'red', 'denied')),
            CONSTRAINT review_state_known
                CHECK (review_state IN ('none', 'pending', 'done'))
        )
        """
    )

    # Ответы чека храним целиком: балл — производная, и через месяц вопрос
    # «почему тогда вышло 14» должен иметь ответ. Уникальность по дню —
    # это запрет перепройти чек (ТЗ 5.2), и он стоит ограничением СУБД,
    # а не проверкой в коде: два одновременных запроса обошли бы проверку.
    op.execute(
        """
        CREATE TABLE daybook.premarket_checks (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id    UUID NOT NULL,
            day        DATE NOT NULL,
            answers    JSONB NOT NULL,
            score      SMALLINT NOT NULL,
            verdict    TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (user_id, day),
            CONSTRAINT verdict_known CHECK (verdict IN ('green', 'red', 'denied'))
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError("откат миграций не поддерживается, восстанавливай дамп")
