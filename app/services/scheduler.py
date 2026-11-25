import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, update

from app.database.models import Booking, BookingStatus, Notification, NotificationStatus, utcnow
from app.database.session import Database
from app.services.notifications import reminder_body
from app.services.settings import load_options

logger = logging.getLogger(__name__)
NOTIFICATION_BATCH_SIZE = 12


class DeliveryError(Exception):
    def __init__(self, reason: str, *, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


class NotificationWorker:
    def __init__(
        self,
        db: Database,
        send: Callable[[int, str], Awaitable[None]],
        clock: Callable[[], datetime] = utcnow,
        *,
        notification_poll_interval_seconds: int = 30,
        booking_status_check_interval_seconds: int = 60,
    ) -> None:
        self.db = db
        self.send = send
        self.clock = clock
        self.notification_poll_interval_seconds = notification_poll_interval_seconds
        self.booking_status_check_interval_seconds = booking_status_check_interval_seconds
        self.scheduler = AsyncIOScheduler(timezone=UTC)
        self.closing = False
        self.rate_limited_until: datetime | None = None

    async def start(self) -> None:
        now = self.clock()
        for job, job_id, seconds in (
            (self.run_once, "notifications", self.notification_poll_interval_seconds),
            (self.complete_bookings, "booking_status", self.booking_status_check_interval_seconds),
        ):
            self.scheduler.add_job(
                job,
                "interval",
                id=job_id,
                seconds=seconds,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=None,
                next_run_time=now,
            )
        self.scheduler.start()

    async def stop(self) -> None:
        self.closing = True
        self.scheduler.pause()
        async with self.db.notification_lock, self.db.booking_status_lock:
            self.scheduler.shutdown()
        await asyncio.sleep(0)

    async def complete_bookings(self) -> None:
        if self.closing or self.db.booking_status_lock.locked():
            return
        async with self.db.booking_status_lock:
            now = self.clock()
            completed = (Booking.status == BookingStatus.CONFIRMED, Booking.end_at <= now)
            async with self.db.sessions() as session:
                if await session.scalar(select(Booking.id).where(*completed).limit(1)) is None:
                    return
            async with self.db.write() as session:
                await session.execute(
                    update(Booking)
                    .where(*completed)
                    .values(status=BookingStatus.COMPLETED, updated_at=now)
                )

    async def run_once(self) -> None:
        if self.closing or self.db.notification_lock.locked():
            return
        async with self.db.notification_lock:
            now = self.clock()
            if self.rate_limited_until is not None and now < self.rate_limited_until:
                return
            await self._send_due(now)

    async def _send_due(self, now: datetime) -> None:
        while not self.closing:
            async with self.db.sessions() as session:
                queue = (
                    await session.execute(
                        select(Notification.id, Notification.starts_at)
                        .where(
                            Notification.status == NotificationStatus.PENDING,
                            Notification.scheduled_at <= now,
                        )
                        .order_by(
                            Notification.starts_at.is_(None),
                            Notification.scheduled_at,
                            Notification.id,
                        )
                        .limit(NOTIFICATION_BATCH_SIZE)
                    )
                ).all()
            expired = [
                notification_id
                for notification_id, starts_at in queue
                if starts_at is not None and starts_at <= now
            ]
            if expired:
                async with self.db.write() as session:
                    await session.execute(
                        update(Notification)
                        .where(
                            Notification.id.in_(expired),
                            Notification.status == NotificationStatus.PENDING,
                        )
                        .values(status=NotificationStatus.SKIPPED)
                    )
            for notification_id, starts_at in queue:
                if self.closing:
                    return
                if starts_at is not None:
                    if starts_at <= now:
                        continue
                    async with self.db.reminder_lock:
                        if self.closing:
                            return
                        rate_limited = await self._deliver(notification_id)
                else:
                    rate_limited = await self._deliver(notification_id)
                if rate_limited:
                    return
            if len(queue) < NOTIFICATION_BATCH_SIZE:
                return

    async def _deliver(self, notification_id: int) -> bool:
        async with self.db.sessions() as session:
            item = await session.get(Notification, notification_id)
            if item is None or item.status != NotificationStatus.PENDING:
                return False
            skip = False
            body = item.body
            if item.starts_at is not None:
                booking = await session.get(Booking, item.booking_id)
                options = await load_options(session)
                enabled = (
                    options.reminder_24h_enabled
                    if item.notification_type == "reminder_24"
                    else options.reminder_1h_enabled
                )
                skip = (
                    not enabled
                    or booking is None
                    or booking.status != BookingStatus.CONFIRMED
                    or booking.version != item.booking_version
                )
                if not skip and booking is not None:
                    body = reminder_body(booking, options)
            elif item.notification_type.startswith("group_"):
                options = await load_options(session)
                skip = (
                    not options.group_notifications_enabled
                    or item.chat_id != options.studio_chat_id
                )
        if item.starts_at is not None and item.starts_at <= self.clock():
            skip = True
        attempts = item.attempts
        last_error = None
        sent_at = None
        scheduled_at = item.scheduled_at
        rate_limited = False
        if skip:
            status = NotificationStatus.SKIPPED
        else:
            attempts += 1
            try:
                await self.send(item.chat_id, body)
            except DeliveryError as error:
                last_error = str(error)[:100]
                delay = error.retry_after
                status = NotificationStatus.FAILED
                if delay is not None:
                    rate_limited = last_error == "TelegramRetryAfter"
                    resume = self.clock() + timedelta(seconds=delay)
                    if rate_limited:
                        self.rate_limited_until = resume
                    if attempts < 5 and (
                        item.starts_at is None or (rate_limited and resume < item.starts_at)
                    ):
                        scheduled_at = resume
                        status = NotificationStatus.PENDING
            else:
                status, sent_at = NotificationStatus.SENT, self.clock()
        # Telegram I/O finishes before SQLite enters the write transaction.
        async with self.db.write() as session:
            await session.execute(
                update(Notification)
                .where(
                    Notification.id == item.id,
                    Notification.status == NotificationStatus.PENDING,
                )
                .values(
                    status=status,
                    sent_at=sent_at,
                    scheduled_at=scheduled_at,
                    attempts=attempts,
                    last_error=last_error,
                )
            )
        if status == NotificationStatus.SENT:
            logger.info("Уведомление отправлено (%s)", item.notification_type)
        elif status == NotificationStatus.FAILED:
            logger.warning("Уведомление не доставлено (%s)", item.notification_type)
        return rate_limited
