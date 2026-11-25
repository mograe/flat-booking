from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AccessRequest,
    Booking,
    Notification,
    NotificationStatus,
    RequestStatus,
    Role,
    User,
    utcnow,
)
from app.database.repositories.accounts import audit, by_telegram, user_for_whitelist
from app.database.repositories.schedule import upcoming_bookings
from app.database.session import Database
from app.exceptions import AccessDenied, DomainError
from app.services.notifications import enqueue

logger = logging.getLogger(__name__)


def grant_membership(user: User, now: datetime, comment: str | None = None) -> bool:
    if user.whitelist_added_at is not None:
        return False
    user.whitelist_added_at = now
    user.whitelist_comment = comment
    if user.role != Role.ADMIN:
        user.role = Role.MEMBER
    return True


async def approve_pending_request(
    session: AsyncSession, user: User, reviewer: User | None, now: datetime
) -> None:
    if user.id is None:
        return
    pending = await session.scalar(
        select(AccessRequest).where(
            AccessRequest.user_id == user.id, AccessRequest.status == RequestStatus.PENDING
        )
    )
    if pending is not None:
        pending.status = RequestStatus.APPROVED
        pending.reviewed_by = reviewer.id if reviewer else None
        pending.reviewed_at = now
        enqueue(session, user.telegram_id, "access", "✅ Вам открыт доступ к студии.")


async def require_user(session: AsyncSession, user_id: int, *, admin: bool = False) -> User:
    user = await session.get(User, user_id)
    roles = (Role.ADMIN,) if admin else (Role.ADMIN, Role.MEMBER)
    if user is None or not user.is_active or user.role not in roles:
        raise AccessDenied()
    return user


class UsersService:
    def __init__(self, db: Database, admin_ids: frozenset[int]) -> None:
        self.db = db
        self.admin_ids = admin_ids

    async def register(
        self, telegram_id: int, first_name: str, last_name: str | None, username: str | None
    ) -> User:
        name = " ".join(filter(None, (first_name, last_name)))[:128]
        fields = dict(first_name=first_name[:128], last_name=last_name, username=username)
        target_admin = telegram_id in self.admin_ids
        async with self.db.sessions() as session:
            user = await by_telegram(session, telegram_id)
            if (
                user
                and user.registered_at is not None
                and all(getattr(user, key) == value for key, value in fields.items())
                and (user.role == Role.ADMIN) == target_admin
            ):
                return user

        async with self.db.write() as session:
            user = await by_telegram(session, telegram_id)
            registered = user is None or user.registered_at is None
            if user is None:
                user = User(telegram_id=telegram_id, display_name=name, **fields)
                session.add(user)
            else:
                for key, value in fields.items():
                    setattr(user, key, value)
                user.display_name = name
            if registered:
                user.registered_at = utcnow()
            if target_admin:
                user.role = Role.ADMIN
                user.is_active = True
            elif user.role == Role.ADMIN:
                user.role = Role.MEMBER if user.whitelist_added_at is not None else Role.GUEST
            await session.flush()
        if registered:
            logger.info("Пользователь зарегистрирован")
        return user

    async def list(self, actor_id: int, offset: int, *, active_only: bool = False) -> list[User]:
        async with self.db.sessions() as session:
            await require_user(session, actor_id, admin=True)
            query = select(User).where(User.role != Role.GUEST)
            if active_only:
                query = query.where(User.is_active.is_(True))
            return list(await session.scalars(query.order_by(User.id).offset(offset).limit(9)))

    async def get(self, actor_id: int, user_id: int) -> User:
        async with self.db.sessions() as session:
            await require_user(session, actor_id, admin=True)
            user = await session.get(User, user_id)
            if user is None:
                raise DomainError("Участник не найден.")
            return user

    async def add(self, actor_id: int, telegram_id: int, comment: str = "") -> User:
        if not 0 < telegram_id < 2**52:
            raise DomainError("Некорректный Telegram User ID.")
        comment = comment.strip()
        if len(comment) > 200:
            raise DomainError("Комментарий должен содержать до 200 знаков.")
        async with self.db.write() as session:
            actor = await require_user(session, actor_id, admin=True)
            user = await user_for_whitelist(session, telegram_id)
            now = utcnow()
            if grant_membership(user, now, comment or None):
                await approve_pending_request(session, user, actor, now)
                await session.flush()
                audit(session, actor, "whitelist_added", "user", user.id)
            return user

    async def whitelist_page(
        self, actor_id: int, offset: int, *, candidates: bool = False
    ) -> list[User]:
        async with self.db.sessions() as session:
            await require_user(session, actor_id, admin=True)
            query = select(User)
            if candidates:
                query = query.where(
                    User.registered_at.is_not(None), User.whitelist_added_at.is_(None)
                )
            else:
                query = query.where(User.whitelist_added_at.is_not(None))
            return list(await session.scalars(query.order_by(User.id).offset(offset).limit(9)))

    async def whitelist_entry(self, actor_id: int, telegram_id: int) -> User:
        async with self.db.sessions() as session:
            await require_user(session, actor_id, admin=True)
            user = await by_telegram(session, telegram_id)
            if user is None or user.whitelist_added_at is None:
                raise DomainError("Пользователь не найден в белом списке.")
            return user

    async def remove(
        self, actor_id: int, telegram_id: int, *, expected_added_at: datetime | None = None
    ) -> int:
        async with self.db.write() as session:
            actor = await require_user(session, actor_id, admin=True)
            user = await by_telegram(session, telegram_id)
            if telegram_id in self.admin_ids or (user is not None and user.role == Role.ADMIN):
                raise DomainError("Доступ администратора определяется списком ADMIN_IDS.")
            if user is None or user.role != Role.MEMBER:
                raise DomainError("Пользователь не найден в белом списке.")
            if expected_added_at is not None and user.whitelist_added_at != expected_added_at:
                raise DomainError("Запись изменилась. Откройте белый список снова.")
            user.role = Role.GUEST
            user.whitelist_added_at = user.whitelist_comment = None
            audit(session, actor, "whitelist_removed", "user", user.id)
            await session.execute(
                update(Notification)
                .where(
                    Notification.chat_id == telegram_id,
                    Notification.notification_type == "broadcast",
                    Notification.status == NotificationStatus.PENDING,
                )
                .values(status=NotificationStatus.CANCELLED)
            )
            return (
                await session.scalar(
                    upcoming_bookings(user.id, utcnow()).with_only_columns(func.count())
                )
                or 0
            )

    async def upcoming(self, actor_id: int, telegram_id: int, offset: int) -> list[Booking]:
        async with self.db.sessions() as session:
            await require_user(session, actor_id, admin=True)
            user = await by_telegram(session, telegram_id)
            if user is None:
                raise DomainError("Пользователь не найден.")
            query = upcoming_bookings(user.id, utcnow()).order_by(Booking.start_at, Booking.id)
            return list(await session.scalars(query.offset(offset).limit(9)))

    async def set_active(self, actor_id: int, user_id: int, active: bool) -> None:
        async with self.db.write() as session:
            actor = await require_user(session, actor_id, admin=True)
            user = await session.get(User, user_id)
            if user is None or user.role != Role.MEMBER or user.telegram_id in self.admin_ids:
                raise DomainError("Действие доступно только для участников.")
            user.is_active = active
            audit(
                session, actor, "member_unblocked" if active else "member_blocked", "user", user.id
            )
