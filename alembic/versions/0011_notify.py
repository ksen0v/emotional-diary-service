"""Уведомления: бот, привязка, доверенное лицо, тексты, очередь отправки.

Revision ID: 0011
Revises: 0010
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Токен бота. Одна строка на сервис, а не на пользователя: бот один,
    # и его токен — секрет сервиса, а не данные трейдера. Ограничение
    # `id = 1` держит это в базе, а не в договорённости.
    #
    # Почему токен в базе, а не в переменной окружения: Влад вставляет его
    # в интерфейсе, и по тому же правилу, что ключи источников, он шифруется
    # мастер-ключом и в API не возвращается. Переменная окружения потребовала
    # бы редактировать файл и перезапускать процесс, а `docker inspect`
    # показывает переменные любому, кто дотянулся до хоста.
    op.execute(
        """
        CREATE TABLE notify.bot (
            id              SMALLINT PRIMARY KEY DEFAULT 1,
            token_encrypted BYTEA NOT NULL,
            key_version     SMALLINT NOT NULL DEFAULT 1,
            username        TEXT,
            bot_id          BIGINT,
            installed_by    UUID NOT NULL,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT bot_is_single CHECK (id = 1)
        )
        """
    )

    # Привязка аккаунта трейдера к его чату с ботом. Код одноразовый и живёт
    # недолго: он же уходит в deep link, а ссылка может попасть куда угодно.
    op.execute(
        """
        CREATE TABLE notify.telegram_links (
            user_id         UUID PRIMARY KEY,
            chat_id         BIGINT,
            link_code       TEXT,
            code_expires_at TIMESTAMPTZ,
            state           TEXT NOT NULL,
            linked_at       TIMESTAMPTZ,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT link_state_known CHECK (state IN ('pending', 'linked'))
        )
        """
    )
    # Код ищется по значению, когда бот получает `/start <код>`, поэтому он
    # уникален. Частичный индекс: у привязанного пользователя кода нет.
    op.execute(
        """
        CREATE UNIQUE INDEX one_link_code ON notify.telegram_links (link_code)
            WHERE link_code IS NOT NULL
        """
    )

    # Доверенное лицо. Один на пользователя — как в прототипе и как в ТЗ 6.8,
    # где это «доверенное лицо», а не список. Ограничением СУБД, потому что
    # второй контакт значил бы второй сигнал, а мы обещаем ровно один.
    #
    # `removal_effective_at` — сутки задержки из ТЗ 6.8. Причина прямо
    # в предметной области: в тильте первым желанием будет отключить того,
    # кто может остановить.
    op.execute(
        """
        CREATE TABLE notify.contacts (
            id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id              UUID NOT NULL,
            handle               TEXT NOT NULL,
            display_name         TEXT,
            chat_id              BIGINT,
            status               TEXT NOT NULL,
            invite_code          TEXT,
            invited_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            consent_at           TIMESTAMPTZ,
            removal_effective_at TIMESTAMPTZ,
            template             TEXT,
            CONSTRAINT contact_status_known CHECK (status IN ('pending', 'confirmed'))
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX one_contact_per_user ON notify.contacts (user_id)"
    )
    op.execute(
        """
        CREATE UNIQUE INDEX one_invite_code ON notify.contacts (invite_code)
            WHERE invite_code IS NOT NULL
        """
    )

    # Тексты уведомлений. Хранятся только переопределённые: нет строки —
    # значит действует дефолт из кода (Архитектура ч.1 §6, ч.2 §3.10).
    # Так улучшенный дефолт доедет до всех, кто свой текст не писал.
    op.execute(
        """
        CREATE TABLE notify.templates (
            user_id    UUID NOT NULL,
            key        TEXT NOT NULL,
            body       TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, key)
        )
        """
    )

    # Очередь отправки. Текст рендерится при постановке, а не при отправке:
    # уведомление должно нести то, что было правдой в момент события, даже
    # если трейдер успел переписать шаблон, пока сообщение лежало в очереди.
    #
    # `dedup_key` — то, что не даст отправить другу пять одинаковых сигналов,
    # если процесс перезапустился посреди обработки.
    op.execute(
        """
        CREATE TABLE notify.outbound (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id         UUID NOT NULL,
            channel         TEXT NOT NULL,
            template        TEXT NOT NULL,
            payload         JSONB NOT NULL,
            body            TEXT NOT NULL,
            keyboard        JSONB,
            chat_id         BIGINT,
            dedup_key       TEXT UNIQUE,
            state           TEXT NOT NULL DEFAULT 'queued',
            attempts        SMALLINT NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            sent_at         TIMESTAMPTZ,
            error           TEXT,
            CONSTRAINT outbound_channel_known
                CHECK (channel IN ('telegram_self', 'telegram_buddy')),
            CONSTRAINT outbound_state_known
                CHECK (state IN ('queued', 'sent', 'failed', 'skipped'))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_outbound_due ON notify.outbound (next_attempt_at)
            WHERE state = 'queued'
        """
    )
    op.execute(
        "CREATE INDEX ix_outbound_user ON notify.outbound (user_id, created_at DESC)"
    )

    # Подтверждение снятия доверенным лицом. Таблица из Архитектуры ч.1 §6,
    # которой не было в миграции 0009: пустая таблица без кода, который в неё
    # пишет, выглядела бы как готовая функция. Теперь код есть.
    op.execute(
        """
        CREATE TABLE incidents.lock_confirmations (
            lock_id      UUID PRIMARY KEY REFERENCES incidents.locks(id),
            contact_id   UUID NOT NULL,
            chat_id      BIGINT,
            confirmed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # Когда у друга попросили подтверждение. Нужно для cooldown из ч.2 §3.7:
    # друга нельзя завалить просьбами в тильте. Поле состояния, как `lifted_at`,
    # поэтому триггер неизменяемости его не запрещает — он сторожит то, что
    # описывает само событие: день, время срабатывания, окно и текст правила.
    op.execute("ALTER TABLE incidents.locks ADD COLUMN buddy_requested_at TIMESTAMPTZ")

    # Разовое выравнивание: сигнал доверенному лицу у SR-2 и SR-3 включён
    # по ТЗ 6.5, но до этого шага код принудительно держал его выключенным —
    # контакта с двойным согласием не существовало. Значение в базе поставил
    # сервис, а не трейдер, поэтому здесь оно возвращается к умолчанию ТЗ.
    # Дальше это уже выбор трейдера, и трогать его нельзя.
    op.execute(
        """
        UPDATE rules.rules
           SET actions = jsonb_set(actions, '{buddy}', 'true'::jsonb)
         WHERE kind = 'system' AND system_code IN ('SR-2', 'SR-3')
        """
    )

    # Гранты по тому же правилу, что и у остальных схем: модуль пишет только
    # в свою (Архитектура ч.1 §6). Роли на модуль в первой версии нет, но
    # владение схемой должно быть объявлено сразу.
    op.execute("COMMENT ON SCHEMA notify IS 'модуль notifications'")


def downgrade() -> None:
    raise NotImplementedError("откат миграций не поддерживается, восстанавливай дамп")
