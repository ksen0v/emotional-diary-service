"""Источник Binance: исполнения, начисления, снимки баланса, позиции.

Revision ID: 0010
Revises: 0009
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Исполнения — истина этого источника. Сделок биржа не отдаёт, их собирает
    # агрегатор, и он обязан уметь пересобрать их заново в любой момент
    # (Архитектура ч.1 §5.5). Поэтому филлы хранятся всегда и целиком: без них
    # первая же ошибка в агрегаторе осталась бы в данных навсегда.
    op.execute(
        """
        CREATE TABLE source.fills (
            id               BIGSERIAL PRIMARY KEY,
            connection_id    UUID NOT NULL REFERENCES source.connections(id),
            user_id          UUID NOT NULL,
            external_id      BIGINT NOT NULL,
            order_id         BIGINT NOT NULL,
            symbol           TEXT NOT NULL,
            position_side    TEXT NOT NULL,
            side             TEXT NOT NULL,
            price            NUMERIC(24,10) NOT NULL,
            qty              NUMERIC(24,10) NOT NULL,
            realized_pnl     NUMERIC(24,10) NOT NULL,
            commission       NUMERIC(24,10) NOT NULL,
            commission_asset TEXT NOT NULL,
            trade_time       TIMESTAMPTZ NOT NULL,
            raw              JSONB NOT NULL
        )
        """
    )
    # Тот же филл приходит и потоком, и сверкой — дубль это норма приёма,
    # а не сбой, и отсекается ключом, а не проверкой в коде.
    op.execute(
        "CREATE UNIQUE INDEX fills_unique ON source.fills (connection_id, external_id)"
    )
    op.execute(
        "CREATE INDEX ON source.fills (connection_id, symbol, position_side, external_id)"
    )
    op.execute("CREATE INDEX ON source.fills (connection_id, trade_time)")

    # Начисления. Нужны дважды: фандинг попадает в результат сделки, а сам
    # список символов, по которым была активность, — единственный способ
    # узнать, у каких символов вообще спрашивать исполнения (§5.4).
    op.execute(
        """
        CREATE TABLE source.income (
            id            BIGSERIAL PRIMARY KEY,
            connection_id UUID NOT NULL REFERENCES source.connections(id),
            external_id   BIGINT,
            symbol        TEXT,
            income_type   TEXT NOT NULL,
            income        NUMERIC(24,10) NOT NULL,
            asset         TEXT NOT NULL,
            happened_at   TIMESTAMPTZ NOT NULL
        )
        """
    )
    # `tranId` у части начислений пустой, поэтому ключ составной и включает
    # время: без него повторная сверка дописывала бы фандинг второй раз,
    # и результат сделки менялся бы на каждом проходе.
    op.execute(
        """
        CREATE UNIQUE INDEX income_unique ON source.income
            (connection_id, income_type, happened_at, COALESCE(symbol, ''),
             COALESCE(external_id, 0))
        """
    )
    op.execute("CREATE INDEX ON source.income (connection_id, symbol, happened_at)")

    # Снимки баланса. У TMM проценты от депозита восстанавливались из
    # `profit_deposit`, где каждый процент посчитан от своей базы; здесь база
    # настоящая, и это главное, что Binance делает точнее.
    op.execute(
        """
        CREATE TABLE source.balance_snapshots (
            connection_id UUID NOT NULL REFERENCES source.connections(id),
            taken_at      TIMESTAMPTZ NOT NULL,
            wallet_usdt   NUMERIC(24,10) NOT NULL,
            equity_usdt   NUMERIC(24,10) NOT NULL,
            PRIMARY KEY (connection_id, taken_at)
        )
        """
    )

    # Докуда агрегатор досчитал. `last_fill_id` — последний филл последней
    # ЗАКРЫТОЙ сделки: всё, что после него, пересобирается на каждом проходе,
    # поэтому пропущенный и позже доехавший филл встаёт на место сам.
    op.execute(
        """
        CREATE TABLE source.aggregate_state (
            connection_id UUID NOT NULL REFERENCES source.connections(id),
            symbol        TEXT NOT NULL,
            position_side TEXT NOT NULL,
            last_fill_id  BIGINT NOT NULL,
            open_position JSONB,
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (connection_id, symbol, position_side)
        )
        """
    )

    # Открытые позиции. Отсюда берётся нереализованный убыток — тот самый
    # момент тильта, который при источнике без позиций был слепой зоной
    # (ТЗ 4.2, Архитектура ч.1 §5.7).
    op.execute(
        """
        CREATE TABLE source.positions (
            connection_id  UUID NOT NULL REFERENCES source.connections(id),
            symbol         TEXT NOT NULL,
            position_side  TEXT NOT NULL,
            qty            NUMERIC(24,10) NOT NULL,
            entry_price    NUMERIC(24,10) NOT NULL,
            mark_price     NUMERIC(24,10),
            unrealized_usd NUMERIC(24,10) NOT NULL,
            unrealized_pct NUMERIC(10,6) NOT NULL,
            liquidation    NUMERIC(24,10),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (connection_id, symbol, position_side)
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError("откат миграций не поддерживается, восстанавливай дамп")
