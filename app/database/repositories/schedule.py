from datetime import datetime

from sqlalchemy import Select, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import BlockedTimeSlot, Booking, BookingStatus, User


def bookings_between(start: datetime, end: datetime) -> Select:
    return select(Booking).where(
        Booking.status.in_((BookingStatus.CONFIRMED, BookingStatus.COMPLETED)),
        Booking.start_at < end,
        Booking.end_at > start,
    )


def blocks_between(start: datetime, end: datetime) -> Select:
    return select(BlockedTimeSlot).where(
        BlockedTimeSlot.start_at < end, BlockedTimeSlot.end_at > start
    )


def upcoming_bookings(user_id: int, now: datetime) -> Select:
    return select(Booking).where(
        Booking.user_id == user_id,
        Booking.status == BookingStatus.CONFIRMED,
        Booking.start_at > now,
    )


async def occupied(
    session: AsyncSession, start: datetime, end: datetime, exclude: int | None = None
) -> bool:
    bookings = bookings_between(start, end).where(Booking.status == BookingStatus.CONFIRMED)
    if exclude is not None:
        bookings = bookings.where(Booking.id != exclude)
    return (
        await session.scalar(bookings.with_only_columns(Booking.id).limit(1)) is not None
        or await session.scalar(blocks_between(start, end).limit(1)) is not None
    )


async def day_bookings(
    session: AsyncSession, start: datetime, end: datetime
) -> list[tuple[Booking, str]]:
    query = (
        bookings_between(start, end)
        .join(User, User.id == Booking.user_id)
        .add_columns(User.display_name)
        .order_by(Booking.start_at)
    )
    return [(booking, name) for booking, name in (await session.execute(query)).all()]


async def intervals(
    session: AsyncSession, start: datetime, end: datetime, exclude: int | None = None
) -> list[tuple[datetime, datetime, bool]]:
    bookings = bookings_between(start, end)
    if exclude is not None:
        bookings = bookings.where(Booking.id != exclude)
    confirmed = bookings.with_only_columns(Booking.start_at, Booking.end_at, literal(False))
    blocks = blocks_between(start, end).with_only_columns(
        BlockedTimeSlot.start_at, BlockedTimeSlot.end_at, literal(True)
    )
    return [
        (a, b, blocked)
        for a, b, blocked in (await session.execute(confirmed.union_all(blocks))).all()
    ]
