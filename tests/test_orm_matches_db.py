"""ORM-модели против настоящей схемы в базе.

Миграции пишутся руками, поэтому расхождение возможно: колонку добавили в модель
и забыли в миграцию. Такая ошибка проявляется только в рантайме и в самый неудобный
момент, поэтому её ловит тест.
"""

import pytest
from sqlalchemy import text

from eds.modules.identity import models as identity_models  # noqa: F401  (регистрирует таблицы)
from eds.platform.orm import Base


@pytest.mark.usefixtures("test_db_url")
async def test_every_orm_column_exists_in_db(factory) -> None:
    async with factory() as s:
        rows = await s.execute(
            text(
                """
                SELECT table_schema, table_name, column_name
                FROM information_schema.columns
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                """
            )
        )
        actual = {(r[0], r[1], r[2]) for r in rows}

    missing: list[str] = []
    for table in Base.metadata.sorted_tables:
        schema = table.schema or "public"
        for column in table.columns:
            if (schema, table.name, column.name) not in actual:
                missing.append(f"{schema}.{table.name}.{column.name}")

    assert not missing, "в базе нет колонок, объявленных в моделях: " + ", ".join(missing)
