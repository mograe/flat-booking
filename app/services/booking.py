from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import BlockedTimeSlot, Booking, BookingStatus, Role, User, utcnow
from app.database.repositories.accounts import audit
from app.database.repositories.schedule import blocks_between, day_bookings, occupied
from app.database.session import Database
from app.exceptions import AccessDenied, Conflict, DomainError
from app.services.availability import Interval
from app.services.notifications import cancel_reminders, enqueue_booking
from app.services.settings import load_options
from app.services.users import require_user
from app.utils.time import local_instant

logger = logging.getLogger(__name__)

PURPOSES = {
    "vocal": "🎤 Запись вокала",
    "instrument": "🎸 Запись инструментов",
    "rehearsal": "🥁 Репетиция",
    "mix": "🎚 Сведение и мастеринг",
    "production": "🎹 Продюсирование",
    "other": "🎵 Другое",
}


def validate_details(purpose: str, comment: str) -> None:
    if purpose not in PURPOSES or len(comment) > 500:
        raise DomainError("Выберите цель и укажите комментарий длиной до 500 знаков.")


async def get_booking(session: AsyncSession, actor: User, booking_id: int) -> Booking:
    booking = await session.get(Booking, booking_id)
    if booking is None:
        raise DomainError("Бронирование не найдено.")
    if actor.role != Role.ADMIN and booking.user_id != actor.id:
        raise AccessDenied()
    return booking


class BookingService:
    def __init__(self, db: Database, clock: Callable[[], datetime] = utcnow) -> None:
        self.db = db
        self.clock = clock

    async def create(
        self,
        actor_id: int,
        interval: Interval,
        purpose: str,
        comment: str = "",
        *,
        owner_id: int | None = None,
        confirmation_key: str | None = None,
    ) -> Booking:
        validate_details(purpose, comment)
        if confirmation_key is not None and not 1 <= len(confirmation_key) <= 48:
            raise DomainError("Подтверждение устарело.")
        async with self.db.write() as session:
            actor = await require_user(session, actor_id, admin=owner_id not in (None, actor_id))
            owner = actor if owner_id in (None, actor_id) else await require_user(session, owner_id)
            if confirmation_key is not None:
                previous = await session.scalar(
                    select(Booking).where(Booking.confirmation_key == confirmation_key)
                )
                if previous is not None:
                    if previous.user_id != owner.id:
                        raise AccessDenied()
                    return previous
            options = await load_options(session)
            now = self.clock()
            interval.validate(now, options, actor.role == Role.ADMIN)
            if await occupied(session, interval.start, interval.end):
                raise Conflict()
            booking = Booking(
                user_id=owner.id,
                start_at=interval.start,
                end_at=interval.end,
                purpose=purpose,
                comment=comment,
                confirmation_key=confirmation_key,
                created_at=now,
                updated_at=now,
            )
            session.add(booking)
            await session.flush()
            enqueue_booking(
                session,
                booking,
                owner,
                options,
                "created",
                "🎙 Новое бронирование",
                PURPOSES[purpose],
                now,
            )
            if actor.role == Role.ADMIN:
                audit(session, actor, "booking_created", "booking", booking.id, owner_id=owner.id)
        logger.info("Бронирование создано")
        return booking

    async def get(self, actor_id: int, booking_id: int) -> tuple[Booking, str]:
        async with self.db.sessions() as session:
            actor = await require_user(session, actor_id)
            booking = await get_booking(session, actor, booking_id)
            owner = await session.get_one(User, booking.user_id)
            return booking, owner.display_name

    async def change(
        self,
        actor_id: int,
        booking_id: int,
        version: int,
        *,
        interval: Interval | None = None,
        purpose: str | None = None,
        comment: str | None = None,
    ) -> Booking:
        async with self.db.reminder_lock, self.db.write() as session:
            actor = await require_user(
                session, actor_id, admin=purpose is not None or comment is not None
            )
            booking = await get_booking(session, actor, booking_id)
            now = self.clock()
            self._check_change(booking, version, now)
            options = await load_options(session)
            if interval is not None:
                interval.validate(now, options, actor.role == Role.ADMIN)
                if await occupied(session, interval.start, interval.end, booking.id):
                    raise Conflict()
                booking.start_at, booking.end_at = interval.start, interval.end
            selected_purpose = booking.purpose if purpose is None else purpose
            selected_comment = booking.comment if comment is None else comment
            validate_details(selected_purpose, selected_comment)
            booking.purpose, booking.comment = selected_purpose, selected_comment
            booking.version += 1
            booking.updated_at = now
            await cancel_reminders(session, booking.id)
            owner = await session.get_one(User, booking.user_id)
            title = (
                "✏️ Администратор изменил бронирование"
                if actor.role == Role.ADMIN and owner.id != actor.id
                else "📅 Бронирование перенесено"
                if interval is not None
                else "✏️ Бронирование изменено"
            )
            enqueue_booking(
                session, booking, owner, options, "changed", title, PURPOSES[booking.purpose], now
            )
            if actor.role == Role.ADMIN:
                audit(session, actor, "booking_changed", "booking", booking.id)
        logger.info("Бронирование перенесено" if interval is not None else "Бронирование изменено")
        return booking

    async def cancel(self, actor_id: int, booking_id: int, version: int) -> None:
        async with self.db.reminder_lock, self.db.write() as session:
            actor = await require_user(session, actor_id)
            booking = await get_booking(session, actor, booking_id)
            now = self.clock()
            self._check_change(booking, version, now)
            booking.status = BookingStatus.CANCELLED
            booking.cancelled_at, booking.cancelled_by = now, actor.id
            booking.updated_at = now
            booking.version += 1
            await cancel_reminders(session, booking.id)
            owner = await session.get_one(User, booking.user_id)
            enqueue_booking(
                session,
                booking,
                owner,
                await load_options(session),
                "cancelled",
                "❌ Бронирование отменено",
                PURPOSES[booking.purpose],
                now,
            )
            if actor.role == Role.ADMIN:
                audit(session, actor, "booking_cancelled", "booking", booking.id)
        logger.info("Бронирование отменено")

    @staticmethod
    def _check_change(booking: Booking, version: int, now: datetime) -> None:
        if booking.version != version:
            raise DomainError("Бронирование изменилось. Откройте его снова.")
        if booking.status != BookingStatus.CONFIRMED or booking.end_at <= now:
            raise DomainError("Это бронирование уже закрыто.")

    async def list(
        self, actor_id: int, offset: int, *, history: bool = False, admin: bool = False
    ) -> list[tuple[Booking, str]]:
        async with self.db.sessions() as session:
            actor = await require_user(session, actor_id, admin=admin)
            query = select(Booking, User.display_name).join(User, User.id == Booking.user_id)
            if not admin:
                query = query.where(Booking.user_id == actor.id)
            future = (Booking.end_at > self.clock()) & (Booking.status == BookingStatus.CONFIRMED)
            ordering = Booking.start_at.desc() if history else Booking.start_at.asc()
            query = query.where(~future if history else future)
            return [
                (booking, name)
                for booking, name in (
                    await session.execute(
                        query.order_by(ordering, Booking.id).offset(offset).limit(9)
                    )
                ).all()
            ]

    async def schedule(
        self,
        actor_id: int,
        day: date,
        days: int = 1,
    ) -> tuple[list[tuple[Booking, str]], list[BlockedTimeSlot]]:
        async with self.db.sessions() as session:
            await require_user(session, actor_id)
            zone = ZoneInfo((await load_options(session)).timezone)
            start = local_instant(day, 0, zone)
            end = local_instant(day + timedelta(days=days), 0, zone)
            return await day_bookings(session, start, end), list(
                await session.scalars(blocks_between(start, end).order_by(BlockedTimeSlot.start_at))
            )

    async def block(self, actor_id: int, interval: Interval, reason: str) -> BlockedTimeSlot:
        if not 1 <= len(reason.strip()) <= 300:
            raise DomainError("Укажите причину длиной до 300 знаков.")
        async with self.db.write() as session:
            actor = await require_user(session, actor_id, admin=True)
            if interval.start < self.clock():
                raise DomainError("Время начала уже прошло.")
            if await occupied(session, interval.start, interval.end):
                raise Conflict()
            block = BlockedTimeSlot(
                start_at=interval.start, end_at=interval.end, reason=reason, created_by=actor.id
            )
            session.add(block)
            await session.flush()
            audit(session, actor, "time_blocked", "blocked_time", block.id)
            return block

    async def list_blocks(self, actor_id: int, offset: int) -> list[BlockedTimeSlot]:
        async with self.db.sessions() as session:
            await require_user(session, actor_id, admin=True)
            return list(
                await session.scalars(
                    select(BlockedTimeSlot)
                    .where(BlockedTimeSlot.end_at > self.clock())
                    .order_by(BlockedTimeSlot.start_at, BlockedTimeSlot.id)
                    .offset(offset)
                    .limit(9)
                )
            )

    async def get_block(self, actor_id: int, block_id: int) -> BlockedTimeSlot:
        async with self.db.sessions() as session:
            await require_user(session, actor_id, admin=True)
            block = await session.get(BlockedTimeSlot, block_id)
            if block is None:
                raise DomainError("Блокировка уже снята.")
            return block

    async def unblock(self, actor_id: int, block_id: int) -> None:
        async with self.db.write() as session:
            actor = await require_user(session, actor_id, admin=True)
            block = await session.get(BlockedTimeSlot, block_id)
            if block is None:
                raise DomainError("Блокировка уже снята.")
            await session.delete(block)
            audit(session, actor, "time_unblocked", "blocked_time", block_id)
