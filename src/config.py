"""Application configuration (TZ 9: secrets come from the environment only)."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

AppEnv = Literal["dev", "test", "prod"]


class MissingSettingError(RuntimeError):
    """Raised when a required environment variable is empty at runtime."""

    def __init__(self, name: str) -> None:
        super().__init__(f"{name} is not set: copy .env.example to .env and fill it in")
        self.name = name


class Settings(BaseSettings):
    """Environment-driven settings. Names match .env.example one to one.

    Values are intentionally optional at import time so that tooling and unit tests can
    import the package without a populated .env; entry points call `require()` on startup.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: AppEnv = "dev"
    log_level: str = "INFO"

    bot_token: str = ""
    # One-off trigger for the "create venue" wizard (decision A3), never a source of rights.
    owner_telegram_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    database_url: str = ""
    redis_url: str = ""

    @field_validator("owner_telegram_ids", mode="before")
    @classmethod
    def _parse_owner_ids(cls, value: Any) -> Any:
        """Accept both a JSON array and a plain comma-separated list."""
        if value is None or isinstance(value, list | tuple | set):
            return value
        if isinstance(value, int):
            return [value]
        raw = str(value).strip()
        if not raw:
            return []
        if raw.startswith("["):
            parsed: Any = json.loads(raw)
            return parsed
        return [chunk.strip() for chunk in raw.split(",") if chunk.strip()]

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        return value.strip().upper() or "INFO"

    @property
    def is_production(self) -> bool:
        return self.app_env == "prod"

    def require(self, *names: str) -> None:
        """Fail fast on startup if a required variable is empty."""
        for name in names:
            if not getattr(self, name):
                raise MissingSettingError(name.upper())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor; use it instead of reading os.environ."""
    return Settings()


settings: Settings = get_settings()
