import pytest

from app.exceptions import DomainError
from app.utils.time import parse_duration


@pytest.mark.parametrize(
    ("text", "minutes"),
    [("1:30", 90), ("2.5", 150), ("2,5", 150), ("12", 720), ("24", 1440), ("24:00", 1440)],
)
def test_parse_duration(text, minutes):
    assert parse_duration(text) == minutes


@pytest.mark.parametrize("text", ["", "0", "24:30", "24,5", "1:60", "-2", "1e1", "1.123", "nan"])
def test_invalid_duration(text):
    with pytest.raises(DomainError):
        parse_duration(text)
