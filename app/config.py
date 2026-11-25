from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

MAX_BOOKING_MINUTES = 1440


class StudioOptions(BaseModel):
    studio_name: str = Field(default="Флэт", min_length=1, max_length=80)
    timezone: str = "Asia/Yekaterinburg"
    max_booking_days: int = Field(default=14, ge=0, le=365)
    min_booking_minutes: int = Field(default=60, ge=1, le=1440)
    max_booking_minutes: int = Field(default=MAX_BOOKING_MINUTES, ge=1, le=MAX_BOOKING_MINUTES)
    booking_step_minutes: int = Field(default=30, ge=1, le=180)
    open_minute: int = Field(default=0, ge=0, lt=1440)
    close_minute: int = Field(default=1440, ge=0, le=1440)
    reminder_24h_enabled: bool = True
    reminder_1h_enabled: bool = True
    group_notifications_enabled: bool = False
    studio_chat_id: int | None = None
    admin_contact: str = Field(default="", max_length=200)
    rules: str = Field(
        default="Подготовка оборудования и уборка входят в забронированное время. "
        "Опоздание не продлевает посещение. Продление возможно при наличии свободного "
        "времени по согласованию с администратором.",
        min_length=1,
        max_length=3000,
    )

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Неизвестный часовой пояс") from None
        return value

    @model_validator(mode="after")
    def validate_limits(self) -> "StudioOptions":
        step = self.booking_step_minutes
        if 1440 % step or self.min_booking_minutes > self.max_booking_minutes:
            raise ValueError("Проверьте шаг и границы продолжительности")
        if self.min_booking_minutes % step or self.max_booking_minutes % step:
            raise ValueError("Продолжительность должна быть кратна шагу")
        if self.open_minute == self.close_minute:
            raise ValueError("Часы открытия и закрытия должны различаться")
        if self.studio_chat_id is not None and not -(2**52) < self.studio_chat_id < 0:
            raise ValueError("ID общего чата должен быть отрицательным")
        if self.group_notifications_enabled and self.studio_chat_id is None:
            raise ValueError("Для общих уведомлений укажите ID чата")
        return self


class Config(BaseSettings, StudioOptions):
    model_config = SettingsConfigDict(
        env_file=".env", env_ignore_empty=True, extra="ignore", hide_input_in_errors=True
    )

    bot_token: SecretStr = SecretStr("")
    admin_ids: Annotated[frozenset[int], NoDecode] = frozenset()
    whitelist_ids: Annotated[frozenset[int], NoDecode] = frozenset()
    database_url: str = "sqlite+aiosqlite:///data/flat.db"
    log_level: str = "INFO"
    sql_echo: bool = False
    notification_poll_interval_seconds: int = Field(default=30, gt=0)
    booking_status_check_interval_seconds: int = Field(default=60, gt=0)

    @field_validator("admin_ids", "whitelist_ids", mode="before")
    @classmethod
    def parse_telegram_ids(cls, value: object) -> object:
        if isinstance(value, str):
            ids = [item.strip() for item in value.split(",") if item.strip()]
            if any(not item.isascii() or not item.isdecimal() or len(item) > 16 for item in ids):
                raise ValueError("Некорректный Telegram User ID")
            return [int(item) for item in ids]
        return value

    @field_validator("admin_ids", "whitelist_ids")
    @classmethod
    def validate_telegram_ids(cls, value: frozenset[int]) -> frozenset[int]:
        if any(not 0 < item < 2**52 for item in value):
            raise ValueError("Некорректный Telegram User ID")
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        try:
            url = make_url(value)
        except ArgumentError:
            raise ValueError("Укажите корректный URL файловой SQLite-базы") from None
        if (
            url.drivername != "sqlite+aiosqlite"
            or not url.database
            or url.database == ":memory:"
            or url.query
        ):
            raise ValueError("Укажите SQLite URL без параметров")
        return value

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Некорректный уровень журналирования")
        return level

    def studio_options(self) -> StudioOptions:
        return StudioOptions.model_validate(
            self.model_dump(include=set(StudioOptions.model_fields))
        )
