"""Общая база для моделей. Таблицы объявляются в модулях, каждая со своей схемой.

Миграции пишутся руками (alembic/versions), автогенерация не используется:
часть таблиц создаётся SQL-ом с ограничениями, которых в ORM не выразить.
Расхождение ловит тест tests/test_orm_matches_db.py.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
