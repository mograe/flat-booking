import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.bot.handlers.schedule import schedule_lines
from app.database.models import BookingStatus
from app.database.session import Database
from app.exceptions import AccessDenied, Conflict, DomainError
from app.services.availability import AvailabilityService, Interval, SlotStatus
from app.services.booking import BookingService
from app.utils.time import format_booking_period, format_interval, local_instant


def slot(hour, minutes=60, *, day=28, minute=0):
    start = datetime(2026, 11, day, hour, minute, tzinfo=UTC)
    return Interval(start, start + timedelta(minutes=minutes))


async def test_same_day_immediately_before_slot_and_idempotency(studio):
    _, service, _, _, _, member, *_ = studio
    expected = slot(8)
    first = await service.create(member.id, expected, "vocal", confirmation_key="key")
    second = await service.create(member.id, expected, "vocal", confirmation_key="key")
    assert first.id == second.id
    assert first.start_at == expected.start
    assert first.created_at < first.start_at


async def test_business_logs_record_committed_events_without_personal_data(studio, caplog):
    _, service, _, users, admin, member, *_ = studio
    private_data = "Данные участника"
    telegram_id = 987654321
    with caplog.at_level(logging.INFO):
        await users.register(telegram_id, private_data, None, None)
        await users.register(telegram_id, private_data, None, None)
        booking = await service.create(
            admin.id, slot(9), "mix", private_data, owner_id=member.id, confirmation_key="key"
        )
        await service.create(
            admin.id, slot(9), "mix", private_data, owner_id=member.id, confirmation_key="key"
        )
        moved = await service.change(admin.id, booking.id, booking.version, interval=slot(10))
        await service.cancel(admin.id, moved.id, moved.version)

    assert caplog.messages.count("Пользователь зарегистрирован") == 1
    assert caplog.messages.count("Бронирование создано") == 1
    assert "Бронирование перенесено" in caplog.messages
    assert "Бронирование отменено" in caplog.messages
    for action in ("booking_created", "booking_changed", "booking_cancelled"):
        assert f"Административное действие выполнено ({action})" in caplog.messages
    assert private_data not in caplog.text
    assert str(telegram_id) not in caplog.text


@pytest.mark.parametrize("interval", [slot(7), slot(9, 30), slot(9, 1470)])
async def test_invalid_interval(studio, interval):
    _, service, _, _, _, member, *_ = studio
    with pytest.raises(DomainError):
        await service.create(member.id, interval, "vocal")


async def test_horizon_and_admin_override(studio):
    _, service, _, _, admin, member, *_ = studio
    start = datetime(2026, 12, 13, 8, tzinfo=UTC)
    interval = Interval(start, start + timedelta(hours=5))
    with pytest.raises(DomainError):
        await service.create(member.id, interval, "other")
    booking = await service.create(admin.id, interval, "other", owner_id=member.id)
    assert booking.user_id == member.id


async def test_hours_and_overnight(studio):
    _, service, settings, _, admin, member, *_ = studio
    await settings.update(admin.id, {"open_minute": 18 * 60, "close_minute": 2 * 60})
    with pytest.raises(DomainError):
        await service.create(member.id, slot(8), "rehearsal")
    booking = await service.create(member.id, slot(18, 120), "rehearsal")
    assert "28 ноября 2026, 23:00–29 ноября 2026, 01:00" == format_interval(
        booking.start_at, booking.end_at, "Asia/Yekaterinburg"
    )


async def test_overlap_and_adjacency(studio):
    _, service, _, _, _, member, other, _ = studio
    await service.create(member.id, slot(9, 120), "vocal")
    for conflict in (slot(8, 120), slot(9, 120), slot(9), slot(8, 240)):
        with pytest.raises(Conflict):
            await service.create(other.id, conflict, "vocal")
    await service.create(other.id, slot(11), "vocal")


@pytest.mark.parametrize("duration", [60, 1440])
async def test_concurrent_connections(studio, duration):
    db, service, _, _, _, member, other, _ = studio
    second_db = Database(str(db.engine.url))
    try:
        second_service = BookingService(second_db, service.clock)
        results = await asyncio.gather(
            service.create(member.id, slot(9, duration), "vocal"),
            second_service.create(other.id, slot(9, duration), "vocal"),
            return_exceptions=True,
        )
    finally:
        await second_db.engine.dispose()
    assert sum(isinstance(result, Conflict) for result in results) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1


async def test_block_and_atomic_move(studio):
    db, service, _, _, admin, member, other, _ = studio
    taken = await service.create(member.id, slot(9), "vocal")
    original = await service.create(other.id, slot(10), "other")
    block = await service.block(admin.id, slot(11), "ТО")
    blocked_day = date(2026, 11, 28)
    slots = await AvailabilityService(db).slots(member.id, [blocked_day], service.clock())
    assert slots[blocked_day][32].status == SlotStatus.CLOSED
    with pytest.raises(Conflict):
        await service.block(admin.id, slot(9), "ТО")
    with pytest.raises(Conflict):
        await service.create(member.id, slot(11), "other")
    with pytest.raises(Conflict):
        await service.change(other.id, original.id, original.version, interval=slot(9))
    assert (await service.get(other.id, original.id))[0].start_at == slot(10).start
    await service.cancel(member.id, taken.id, taken.version)
    assert (await service.get(member.id, taken.id))[0].status == BookingStatus.CANCELLED
    moved = await service.change(other.id, original.id, original.version, interval=slot(9))
    assert moved.start_at == slot(9).start
    assert moved.version == 2
    await service.unblock(admin.id, block.id)
    days = await AvailabilityService(db).slots(member.id, [date(2026, 11, 28)], service.clock())
    assert days[date(2026, 11, 28)][28].status == SlotStatus.BUSY
    assert days[date(2026, 11, 28)][32].status == SlotStatus.FREE


async def test_permissions(studio):
    _, service, settings, users, admin, member, other, guest = studio
    booking = await service.create(member.id, slot(9), "other")
    with pytest.raises(AccessDenied):
        await service.cancel(other.id, booking.id, booking.version)
    with pytest.raises(AccessDenied):
        await service.create(guest.id, slot(10), "other")
    with pytest.raises(AccessDenied):
        await settings.update(member.id, {"studio_name": "Новая"})
    await users.set_active(admin.id, member.id, False)
    with pytest.raises(AccessDenied):
        await service.get(member.id, booking.id)
    assert (await service.get(admin.id, booking.id))[0].id == booking.id


def test_local_month_boundary():
    instant = local_instant(date(2026, 12, 1), 0, ZoneInfo("Asia/Yekaterinburg"))
    assert instant == datetime(2026, 11, 30, 19, tzinfo=UTC)


@pytest.mark.parametrize("duration", [720, 1440])
async def test_long_booking_and_adjacent_slot(studio, duration):
    _, service, _, _, _, member, other, _ = studio
    interval = slot(13, duration)
    booking = await service.create(member.id, interval, "instrument")
    assert booking.end_at - booking.start_at == timedelta(minutes=duration)
    with pytest.raises(Conflict):
        await service.create(
            other.id,
            Interval(
                booking.end_at - timedelta(minutes=30), booking.end_at + timedelta(minutes=30)
            ),
            "mix",
        )
    await service.create(
        other.id, Interval(booking.end_at, booking.end_at + timedelta(hours=1)), "mix"
    )


async def test_duration_limit_applies_to_administrative_operations(studio):
    _, service, settings, _, admin, member, *_ = studio
    with pytest.raises(DomainError, match="Продолжительность"):
        await service.create(admin.id, slot(9, 1470), "mix", owner_id=member.id)
    with pytest.raises(DomainError):
        await settings.update(admin.id, {"max_booking_minutes": 1470})
    await settings.update(admin.id, {"max_booking_minutes": 720})
    booking = await service.create(admin.id, slot(9, 720), "mix", owner_id=member.id)
    with pytest.raises(DomainError, match="Продолжительность"):
        await service.change(admin.id, booking.id, booking.version, interval=slot(9, 1440))
    assert (await service.get(admin.id, booking.id))[0].end_at == booking.end_at


async def test_move_and_cancel_long_booking_with_block_on_following_day(studio):
    db, service, _, _, admin, member, other, _ = studio
    long = await service.create(member.id, slot(13, 1440), "mix")
    block = await service.block(admin.id, slot(12, day=30), "ТО")
    target = slot(13, 1440, day=29)
    with pytest.raises(Conflict):
        await service.change(member.id, long.id, long.version, interval=target)
    with pytest.raises(Conflict):
        await service.create(other.id, target, "mix")
    assert (await service.get(member.id, long.id))[0].start_at == long.start_at
    available = await AvailabilityService(db).slots(
        member.id, [date(2026, 11, 29)], service.clock()
    )
    slot_at_18 = next(item for item in available[date(2026, 11, 29)] if item.minute == 18 * 60)
    assert 720 in slot_at_18.lengths
    assert 1440 not in slot_at_18.lengths
    await service.unblock(admin.id, block.id)
    moved = await service.change(member.id, long.id, long.version, interval=target)
    assert (moved.start_at, moved.end_at) == (target.start, target.end)
    await service.cancel(member.id, moved.id, moved.version)
    assert (await service.get(member.id, moved.id))[0].status == BookingStatus.CANCELLED
    await service.create(other.id, target, "rehearsal")


@pytest.mark.parametrize(
    ("day", "first", "last"),
    [
        (date(2026, 11, 28), "28 ноября 2026", "29 ноября 2026"),
        (date(2026, 11, 30), "30 ноября 2026", "1 декабря 2026"),
        (date(2026, 12, 31), "31 декабря 2026", "1 января 2027"),
    ],
)
async def test_long_booking_dates_and_daily_schedule(studio, day, first, last):
    db, _, _, _, _, member, *_ = studio
    zone = ZoneInfo("Asia/Yekaterinburg")
    start = local_instant(day, 18 * 60, zone)
    service = BookingService(db, lambda: start - timedelta(seconds=1))
    end = start + timedelta(hours=24)
    await service.create(member.id, Interval(start, end), "instrument")
    assert format_booking_period(start, end, zone.key) == (
        f"📅 Начало: {first}, 18:00\n📅 Окончание: {last}, 18:00\n⏳ Продолжительность: 24 часа"
    )
    full = [f"{first}, 18:00–24:00 · занято", f"{last}, 00:00–18:00 · занято"]
    for wanted_day, wanted_days, expected in (
        (day, 1, full[:1]),
        (day + timedelta(days=1), 1, full[1:]),
        (day, 2, full),
        (day + timedelta(days=2), 1, []),
    ):
        bookings, blocks = await service.schedule(member.id, wanted_day, wanted_days)
        entries = [(item.start_at, item.end_at, "занято") for item, _ in bookings]
        assert not blocks
        assert schedule_lines(wanted_day, wanted_days, entries, zone) == expected
