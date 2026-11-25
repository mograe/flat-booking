from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from app.config import StudioOptions
from app.database.repositories.schedule import intervals
from app.database.session import Database
from app.exceptions import DomainError
from app.services.settings import load_options
from app.services.users import require_user
from app.utils.time import local_instant

MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if any(
            value.tzinfo is None or value.utcoffset() is None for value in (self.start, self.end)
        ):
            raise DomainError("Укажите время с часовым поясом.")
        object.__setattr__(self, "start", self.start.astimezone(UTC))
        object.__setattr__(self, "end", self.end.astimezone(UTC))
        if self.end <= self.start:
            raise DomainError("Конец интервала должен быть позже начала.")

    def validate(self, now: datetime, options: StudioOptions, admin: bool) -> None:
        zone = ZoneInfo(options.timezone)
        local = self.start.astimezone(zone)
        minutes = (self.end - self.start) / MINUTE
        if self.start < now:
            raise DomainError("Время начала уже прошло.")
        if not admin and local.date() > now.astimezone(zone).date() + timedelta(
            days=options.max_booking_days
        ):
            raise DomainError("Дата за пределами горизонта бронирования.")
        if not options.min_booking_minutes <= minutes <= options.max_booking_minutes:
            raise DomainError("Продолжительность за пределами настроек студии.")
        step = options.booking_step_minutes
        if (
            local.second
            or local.microsecond
            or (local.hour * 60 + local.minute) % step
            or minutes % step
        ):
            raise DomainError(
                "Время начала и продолжительность должны соответствовать шагу студии."
            )
        closing = working_end(self.start, options)
        if closing is None or self.end > closing:
            raise DomainError("Интервал выходит за часы работы студии.")


def working_end(start: datetime, options: StudioOptions) -> datetime | None:
    opening, closing = options.open_minute, options.close_minute
    if (opening, closing) == (0, 1440):
        return datetime.max.replace(tzinfo=UTC)
    zone = ZoneInfo(options.timezone)
    day = start.astimezone(zone).date()
    if closing < opening:
        closing += 1440
    for base in (day, day - timedelta(days=1)):
        try:
            first = local_instant(base, opening, zone)
            end = local_instant(base, closing, zone)
        except DomainError:
            continue
        if first <= start < end:
            return end
    return None


class SlotStatus(StrEnum):
    FREE = "🟢"
    BUSY = "🔴"
    CLOSED = "⚫"


@dataclass(frozen=True)
class Slot:
    minute: int
    status: SlotStatus
    lengths: tuple[int, ...] = ()


def day_slots(
    day: date,
    options: StudioOptions,
    now: datetime,
    busy: list[tuple[datetime, datetime, bool]],
    admin: bool,
    first_free_only: bool = False,
) -> list[Slot]:
    zone = ZoneInfo(options.timezone)
    step = options.booking_step_minutes
    minimum = options.min_booking_minutes
    maximum = options.max_booking_minutes
    result = []
    for minute in range(0, 1440, step):
        try:
            start = local_instant(day, minute, zone)
            Interval(start, start + minimum * MINUTE).validate(now, options, admin)
        except DomainError:
            result.append(Slot(minute, SlotStatus.CLOSED))
            continue
        closing = working_end(start, options)
        end_limit = min(start + maximum * MINUTE, closing) if closing else start
        unavailable = SlotStatus.BUSY
        for first, last, blocked in busy:
            if last > start:
                end_limit = min(end_limit, max(start, first))
                if blocked and first < start + minimum * MINUTE:
                    unavailable = SlotStatus.CLOSED
        available = tuple(range(minimum, int((end_limit - start) / MINUTE) + 1, step))
        result.append(Slot(minute, SlotStatus.FREE if available else unavailable, available))
        if available and first_free_only:
            break
    return result


class AvailabilityService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def slots(
        self,
        actor_id: int,
        days: list[date],
        now: datetime,
        *,
        admin: bool = False,
        exclude: int | None = None,
        first_free_only: bool = False,
    ) -> dict[date, list[Slot]]:
        async with self.db.sessions() as session:
            await require_user(session, actor_id, admin=admin)
            if not days:
                return {}
            options = await load_options(session)
            zone = ZoneInfo(options.timezone)
            start = local_instant(min(days), 0, zone)
            end = local_instant(max(days) + timedelta(days=1), 0, zone) + timedelta(
                minutes=options.max_booking_minutes
            )
            busy = await intervals(session, start, end, exclude)
            return {day: day_slots(day, options, now, busy, admin, first_free_only) for day in days}
