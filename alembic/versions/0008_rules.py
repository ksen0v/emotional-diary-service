"""Правила: конструктор «если — то».

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Условия, действия и условия снятия лежат в JSONB одним куском, а не
    # разложены по колонкам. Причина не в лени: правило — это дерево, которое
    # будет расти (в бэклоге словаря ТЗ 6.3 лежат поведенческие предикаты
    # и вложенные группы), и каждый новый вид условия иначе означал бы миграцию.
    #
    # deleted_at — потому что удаление правила по контракту (Архитектура ч.2 §3.6)
    # не стирает строку: на правило будут ссылаться инциденты, и «почему меня
    # заблокировало» должно отвечаться и после того, как правило убрали.
    op.execute(
        """
        CREATE TABLE rules.rules (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id     UUID NOT NULL,
            name        TEXT NOT NULL,
            kind        TEXT NOT NULL,
            system_code TEXT,
            enabled     BOOLEAN NOT NULL DEFAULT true,
            conditions  JSONB NOT NULL,
            actions     JSONB NOT NULL,
            unlock      JSONB NOT NULL,
            version     INT NOT NULL DEFAULT 1,
            deleted_at  TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT kind_known CHECK (kind IN ('system', 'user')),
            CONSTRAINT system_has_code CHECK (
                (kind = 'system') = (system_code IS NOT NULL)
            ),
            CONSTRAINT name_not_empty CHECK (length(btrim(name)) > 0)
        )
        """
    )

    # Один SR-код на пользователя. Системные правила создаются по требованию,
    # а не при регистрации, поэтому идемпотентность нужна ограничением базы:
    # два одновременных запроса не должны создать SR-1 дважды.
    op.execute(
        """
        CREATE UNIQUE INDEX rules_one_system_code
            ON rules.rules (user_id, system_code)
            WHERE system_code IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX rules_user_live
            ON rules.rules (user_id)
            WHERE deleted_at IS NULL
        """
    )

    # ТЗ 6.5: системные триггеры отключить нельзя. Проверка стоит в базе,
    # а не только в API, по той же причине, по которой в базе стоит запрет
    # перепройти чек: код обходится новым эндпоинтом, ограничение — нет.
    op.execute(
        """
        CREATE FUNCTION rules.forbid_disabling_system() RETURNS trigger AS $$
        BEGIN
            IF OLD.kind = 'system' AND NEW.enabled = false THEN
                RAISE EXCEPTION 'системное правило % нельзя выключить', OLD.system_code
                    USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.kind = 'system' AND NEW.deleted_at IS NOT NULL THEN
                RAISE EXCEPTION 'системное правило % нельзя удалить', OLD.system_code
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER system_rules_always_on
            BEFORE UPDATE ON rules.rules
            FOR EACH ROW EXECUTE FUNCTION rules.forbid_disabling_system()
        """
    )


def downgrade() -> None:
    raise NotImplementedError("откат миграций не поддерживается, восстанавливай дамп")
