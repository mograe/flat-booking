from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AuditLog, User


async def by_telegram(session: AsyncSession, telegram_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def user_for_whitelist(session: AsyncSession, telegram_id: int) -> User:
    user = await by_telegram(session, telegram_id)
    if user is None:
        name = f"Участник {telegram_id}"
        user = User(telegram_id=telegram_id, first_name=name, display_name=name)
        session.add(user)
    return user


def audit(
    session: AsyncSession,
    actor: User,
    action: str,
    entity: str,
    entity_id: int | None = None,
    **details: object,
) -> None:
    session.add(
        AuditLog(
            actor_id=actor.id,
            action=action,
            entity_type=entity,
            entity_id=entity_id,
            details=details,
        )
    )
    session.info.setdefault("admin_actions", []).append(action)
