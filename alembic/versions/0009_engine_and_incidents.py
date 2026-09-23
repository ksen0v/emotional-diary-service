"""Движок правил: счётчики дня, журнал проверок, инциденты и блокировки.

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- rules: состояние дня и журнал проверок ---

    # Счётчики дня — кеш, а не истина. Истина лежит в сделках, и recompute()
    # пересобирает эту строку проходом по ним в любой момент. Без кеша каждая
    # проверка правила означала бы полный проход по дню; без пересбора первая
    # же ошибка в apply() осталась бы в данных навсегда.
    op.execute(
        """
        CREATE TABLE rules.day_counters (
            user_id            UUID NOT NULL,
            day                DATE NOT NULL,
            loss_streak        SMALLINT NOT NULL DEFAULT 0,
            equity_pct         NUMERIC(10,6) NOT NULL DEFAULT 0,
            peak_pct           NUMERIC(10,6) NOT NULL DEFAULT 0,
            drawdown_pct       NUMERIC(10,6) NOT NULL DEFAULT 0,
            loss_sum_pct       NUMERIC(10,6) NOT NULL DEFAULT 0,
            unrealized_pct     NUMERIC(10,6),
            drawdown_full_pct  NUMERIC(10,6),
            significant_trades SMALLINT NOT NULL DEFAULT 0,
            all_trades         SMALLINT NOT NULL DEFAULT 0,
            last_trade_id      UUID,
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, day)
        )
        """
    )

    # unrealized_pct и drawdown_full_pct допускают NULL, а не ноль по умолчанию:
    # у источника без открытых позиций это «неизвестно», и ноль читался бы как
    # «открытых позиций нет» — это разные вещи (Архитектура ч.2 §3.5).

    # Журнал проверок. snapshot — то, без чего невозможно разобраться в жалобе
    # «правило сработало непонятно почему»: в нём лежат все числа, по которым
    # принято решение. Уникальность по (rule_id, trigger_ref) делает обработку
    # идемпотентной: одна и та же сделка приходит и из потока, и из сверки.
    op.execute(
        """
        CREATE TABLE rules.evaluations (
            id          BIGSERIAL PRIMARY KEY,
            rule_id     UUID NOT NULL,
            user_id     UUID NOT NULL,
            day         DATE NOT NULL,
            trigger_ref TEXT NOT NULL,
            fired       BOOLEAN NOT NULL,
            snapshot    JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (rule_id, trigger_ref)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_evaluations_day ON rules.evaluations (user_id, day, id)"
    )

    # --- incidents: инциденты и блокировки ---

    # shadow — режим наблюдения (Архитектура ч.2 §5.10). Колонкой, а не полем
    # в details: по нему фильтруются метрики, а фильтр по JSONB здесь был бы
    # платой ни за что. Инцидент в режиме наблюдения пишется полностью и
    # остаётся в истории как факт наблюдения.
    op.execute(
        """
        CREATE TABLE incidents.incidents (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id    UUID NOT NULL,
            day        DATE NOT NULL,
            rule_id    UUID,
            code       TEXT NOT NULL,
            outcome    TEXT NOT NULL DEFAULT 'open',
            shadow     BOOLEAN NOT NULL DEFAULT false,
            opened_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            closed_at  TIMESTAMPTZ,
            details    JSONB NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT outcome_known CHECK (outcome IN ('open', 'kept', 'breached'))
        )
        """
    )
    # Ключ повтора — сделка, на которой правило сработало. Она же приходит
    # дважды (поток и сверка), поэтому уникальность обязана стоять в базе,
    # а не в проверке «а не создавали ли мы уже такой».
    op.execute(
        """
        CREATE UNIQUE INDEX uq_incident_trigger ON incidents.incidents
            (user_id, code, day, rule_id, (details->>'trade_id'))
        """
    )
    op.execute("CREATE INDEX ix_incidents_day ON incidents.incidents (user_id, day DESC)")

    op.execute(
        """
        CREATE TABLE incidents.locks (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id      UUID NOT NULL,
            incident_id  UUID NOT NULL REFERENCES incidents.incidents(id),
            day          DATE NOT NULL,
            rule_id      UUID,
            rule_name    TEXT NOT NULL,
            rule_text    TEXT NOT NULL,
            rule_version INT NOT NULL DEFAULT 1,
            started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            timer_until  TIMESTAMPTZ,
            window_until TIMESTAMPTZ NOT NULL,
            requires     JSONB NOT NULL,
            state        TEXT NOT NULL DEFAULT 'active',
            lifted_at    TIMESTAMPTZ,
            lift_reason  TEXT,
            CONSTRAINT lock_state_known
                CHECK (state IN ('active', 'lifted', 'expired', 'breached'))
        )
        """
    )
    # rule_name и rule_text — копия на момент срабатывания, а не ссылка.
    # Правку правила инцидент не должен переписывать задним числом, иначе
    # история «почему меня заблокировало» станет нечитаемой после первой же
    # правки (Архитектура ч.2 §3.6).
    op.execute(
        """
        CREATE UNIQUE INDEX one_active_lock ON incidents.locks (user_id)
            WHERE state = 'active'
        """
    )
    # Ровно одна активная блокировка на пользователя — ограничением СУБД,
    # а не проверкой в коде. Контракт GET /locks/active отдаёт одну блокировку
    # (Архитектура ч.2 §3.7), и второй активной в нём просто негде показаться.
    op.execute("CREATE INDEX ix_locks_day ON incidents.locks (user_id, day DESC)")

    op.execute(
        """
        CREATE TABLE incidents.lock_reviews (
            lock_id   UUID PRIMARY KEY REFERENCES incidents.locks(id),
            q1        TEXT NOT NULL,
            q2        TEXT NOT NULL,
            q3        TEXT NOT NULL,
            filled_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # incidents.lock_confirmations здесь нет: подтверждение доверенного лица
    # требует контакта с двойным согласием, а контакты — шаг 13. Пустая
    # таблица без кода, который в неё пишет, выглядела бы как готовая функция.

    # История неизменяема (ТЗ 9.2). Правило уровня репозитория продублировано
    # триггером: меняться могут только поля состояния, всё остальное — нет.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION incidents.freeze_incident() RETURNS trigger AS $$
        BEGIN
            IF NEW.user_id IS DISTINCT FROM OLD.user_id
               OR NEW.day IS DISTINCT FROM OLD.day
               OR NEW.rule_id IS DISTINCT FROM OLD.rule_id
               OR NEW.code IS DISTINCT FROM OLD.code
               OR NEW.opened_at IS DISTINCT FROM OLD.opened_at THEN
                RAISE EXCEPTION 'инцидент % неизменяем: меняется только исход', OLD.id
                    USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.outcome <> 'open' AND NEW.outcome <> OLD.outcome THEN
                RAISE EXCEPTION 'исход инцидента % уже записан', OLD.id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER incidents_are_immutable
            BEFORE UPDATE ON incidents.incidents
            FOR EACH ROW EXECUTE FUNCTION incidents.freeze_incident()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION incidents.freeze_lock() RETURNS trigger AS $$
        BEGIN
            IF NEW.user_id IS DISTINCT FROM OLD.user_id
               OR NEW.incident_id IS DISTINCT FROM OLD.incident_id
               OR NEW.day IS DISTINCT FROM OLD.day
               OR NEW.started_at IS DISTINCT FROM OLD.started_at
               OR NEW.window_until IS DISTINCT FROM OLD.window_until
               OR NEW.timer_until IS DISTINCT FROM OLD.timer_until
               OR NEW.requires IS DISTINCT FROM OLD.requires
               OR NEW.rule_text IS DISTINCT FROM OLD.rule_text THEN
                RAISE EXCEPTION 'блокировка % неизменяема: меняется только состояние', OLD.id
                    USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.state <> 'active' AND NEW.state <> OLD.state THEN
                RAISE EXCEPTION 'блокировка % уже закрыта', OLD.id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER locks_are_immutable
            BEFORE UPDATE ON incidents.locks
            FOR EACH ROW EXECUTE FUNCTION incidents.freeze_lock()
        """
    )


def downgrade() -> None:
    raise NotImplementedError("откат миграций не поддерживается, восстанавливай дамп")
