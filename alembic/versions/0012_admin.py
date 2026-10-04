"""Админка: флаг админа у пользователя и одноразовые входы через бота.

Revision ID: 0012
Revises: 0011
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Админ — это признак пользователя, а не отдельная сущность со своим
    # паролем. Причина простая: вход в админку идёт через Telegram, а чат
    # привязан к пользователю. Заводить второй вид учётной записи значило бы
    # заводить второй способ её потерять.
    op.execute(
        "ALTER TABLE identity.users "
        "ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT false"
    )

    # Одноразовый вход. Страница админки просит код, показывает ссылку
    # на бота, и ждёт. Кто открыл ссылку — тот и назвался: пароля здесь нет
    # вовсе, личность приходит из Telegram.
    #
    # `user_id` пуст до подтверждения: в момент выдачи кода сервис ещё
    # не знает, кто его откроет, и знать не должен — иначе код, выданный
    # по чужой просьбе, уже был бы наполовину входом.
    op.execute(
        """
        CREATE TABLE identity.admin_logins (
            code         TEXT PRIMARY KEY,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at   TIMESTAMPTZ NOT NULL,
            user_id      UUID REFERENCES identity.users(id) ON DELETE CASCADE,
            chat_id      BIGINT,
            confirmed_at TIMESTAMPTZ,
            used_at      TIMESTAMPTZ
        )
        """
    )
    # Протухшие коды подчищаются по этому индексу, а не перебором таблицы.
    op.execute(
        "CREATE INDEX admin_logins_expires_idx ON identity.admin_logins (expires_at)"
    )


def downgrade() -> None:
    raise NotImplementedError("откат миграций не поддерживается, восстанавливай дамп")
