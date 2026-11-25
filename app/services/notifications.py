from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import StudioOptions
from app.database.models import (
    Booking,
    BookingStatus,
    Notification,
    NotificationStatus,
    User,
    utcnow,
)
from app.utils.time import format_booking_period


def enqueue(
    session: AsyncSession,
    chat_id: int,
    kind: str,
    body: str,
    *,
    booking: Booking | None = None,
    at: datetime | None = None,
    starts_at: datetime | None = None,
) -> None:
    session.add(
        Notification(
            chat_id=chat_id,
            notification_type=kind,
            body=body,
            scheduled_at=at or utcnow(),
            booking_id=booking.id if booking else None,
            booking_version=booking.version if booking else None,
            starts_at=starts_at,
        )
    )


async def cancel_reminders(session: AsyncSession, booking_id: int) -> None:
    await session.execute(
        update(Notification)
        .where(
            Notification.booking_id == booking_id,
            Notification.starts_at.is_not(None),
            Notification.status == NotificationStatus.PENDING,
        )
        .values(status=NotificationStatus.CANCELLED)
    )


def enqueue_booking(
    session: AsyncSession,
    booking: Booking,
    owner: User,
    options: StudioOptions,
    event: str,
    title: str,
    purpose: str,
    now: datetime,
) -> None:
    period = format_booking_period(booking.start_at, booking.end_at, options.timezone)
    body = f"{title}\n{period}\n🎵 {purpose}"
    enqueue(session, owner.telegram_id, event, body, booking=booking, at=now)
    if options.group_notifications_enabled and options.studio_chat_id is not None:
        enqueue(
            session,
            options.studio_chat_id,
            f"group_{event}",
            f"{body}\n👤 {owner.display_name}",
            booking=booking,
            at=now,
        )
    if event == "cancelled":
        return
    for hours, enabled in ((24, options.reminder_24h_enabled), (1, options.reminder_1h_enabled)):
        if enabled and booking.start_at - timedelta(hours=hours) >= now:
            enqueue_reminder(session, booking, owner.telegram_id, options, hours)


def enqueue_reminder(
    session: AsyncSession,
    booking: Booking,
    chat_id: int,
    options: StudioOptions,
    hours: int,
) -> None:
    enqueue(
        session,
        chat_id,
        f"reminder_{hours}",
        reminder_body(booking, options),
        booking=booking,
        at=booking.start_at - timedelta(hours=hours),
        starts_at=booking.start_at,
    )


def reminder_body(booking: Booking, options: StudioOptions) -> str:
    period = format_booking_period(booking.start_at, booking.end_at, options.timezone)
    return f"⏰ Напоминание о бронировании\n{period}"


async def restore_enabled_reminders(
    session: AsyncSession,
    previous: StudioOptions,
    options: StudioOptions,
    now: datetime,
) -> None:
    for hours in (24, 1):
        field = f"reminder_{hours}h_enabled"
        if getattr(previous, field) or not getattr(options, field):
            continue
        threshold = timedelta(hours=hours)
        exists = (
            select(Notification.id)
            .where(
                Notification.booking_id == Booking.id,
                Notification.booking_version == Booking.version,
                Notification.notification_type == f"reminder_{hours}",
            )
            .exists()
        )
        query = (
            select(Booking, User.telegram_id)
            .join(User, User.id == Booking.user_id)
            .where(
                Booking.status == BookingStatus.CONFIRMED,
                Booking.start_at >= now + threshold,
                ~exists,
            )
        )
        for booking, chat_id in (await session.execute(query)).all():
            if booking.start_at - threshold >= booking.updated_at:
                enqueue_reminder(session, booking, chat_id, options, hours)
