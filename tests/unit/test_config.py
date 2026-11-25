import pytest
from pydantic import ValidationError

from app.config import Config, StudioOptions


def test_environment(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", " 15, 25 ")
    monkeypatch.setenv("WHITELIST_IDS", " 31, 31, 42 ")
    monkeypatch.setenv("STUDIO_CHAT_ID", "")
    config = Config(_env_file=None)
    assert config.admin_ids == {15, 25}
    assert config.whitelist_ids == {31, 42}
    assert config.max_booking_minutes == 1440
    assert config.studio_options().studio_chat_id is None
    assert config.studio_options().timezone == "Asia/Yekaterinburg"


@pytest.mark.parametrize("value", ["0", "-42", "42.0", "username", str(2**52)])
def test_invalid_whitelist_id(value):
    with pytest.raises(ValidationError, match="whitelist_ids"):
        Config(_env_file=None, whitelist_ids=value)


def test_background_settings_defaults(monkeypatch):
    for variable in (
        "LOG_LEVEL",
        "SQL_ECHO",
        "NOTIFICATION_POLL_INTERVAL_SECONDS",
        "BOOKING_STATUS_CHECK_INTERVAL_SECONDS",
    ):
        monkeypatch.delenv(variable, raising=False)
    config = Config(_env_file=None)
    assert (config.log_level, config.sql_echo) == ("INFO", False)
    assert config.notification_poll_interval_seconds == 30
    assert config.booking_status_check_interval_seconds == 60


def test_background_settings_from_environment(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("SQL_ECHO", "true")
    monkeypatch.setenv("NOTIFICATION_POLL_INTERVAL_SECONDS", "45")
    monkeypatch.setenv("BOOKING_STATUS_CHECK_INTERVAL_SECONDS", "75")
    config = Config(_env_file=None)
    assert (config.log_level, config.sql_echo) == ("DEBUG", True)
    assert config.notification_poll_interval_seconds == 45
    assert config.booking_status_check_interval_seconds == 75


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("log_level", "verbose"),
        ("sql_echo", "sometimes"),
        ("notification_poll_interval_seconds", 0),
        ("notification_poll_interval_seconds", 1.5),
        ("booking_status_check_interval_seconds", -1),
    ],
)
def test_invalid_background_settings(field, value):
    with pytest.raises(ValidationError, match=field):
        Config(_env_file=None, **{field: value})


@pytest.mark.parametrize(
    "values",
    [
        {"timezone": "Mars/Base"},
        {"min_booking_minutes": 300, "max_booking_minutes": 240},
        {"booking_step_minutes": 13},
        {"group_notifications_enabled": True},
        {"open_minute": 0, "close_minute": 0},
    ],
)
def test_invalid_limits(values):
    with pytest.raises(ValidationError):
        StudioOptions(**values)


def test_invalid_database_url_is_reported_as_configuration_error():
    with pytest.raises(ValidationError, match="database_url"):
        Config(_env_file=None, database_url="bad url")
