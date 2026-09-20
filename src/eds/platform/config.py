"""Настройки процесса. Читаются из окружения, значения по умолчанию — для локального запуска."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    database_url: str = Field(
        default="postgresql+asyncpg://eds:eds_local_password@localhost:5432/eds",
        alias="DATABASE_URL",
    )
    env: str = Field(default="local", alias="EDS_ENV")
    log_level: str = Field(default="INFO", alias="EDS_LOG_LEVEL")

    # Мастер-ключ шифрования ключей источников. На шаге 0 ещё не используется,
    # но читается сразу, чтобы отсутствие было видно на странице состояния.
    secret_key: str = Field(default="", alias="EDS_SECRET_KEY")

    # Как часто консьюмеры опрашивают outbox, секунды.
    bus_poll_interval: float = Field(default=0.5, alias="EDS_BUS_POLL_INTERVAL")

    @property
    def secret_key_set(self) -> bool:
        return bool(self.secret_key.strip())


@lru_cache
def settings() -> Settings:
    return Settings()
