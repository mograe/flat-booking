from sqlalchemy import select

from app.database.models import AuditLog, Role, User
from app.database.repositories.accounts import audit
from app.database.session import Database
from app.exceptions import DomainError
from app.services.notifications import enqueue
from app.services.users import require_user


class AdminService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def broadcast(self, actor_id: int, body: str) -> int:
        if not 1 <= len(body.strip()) <= 3000:
            raise DomainError("Объявление должно содержать от 1 до 3000 знаков.")
        async with self.db.write() as session:
            actor = await require_user(session, actor_id, admin=True)
            recipients = list(
                await session.scalars(
                    select(User.telegram_id).where(
                        User.role.in_((Role.MEMBER, Role.ADMIN)),
                        User.is_active.is_(True),
                    )
                )
            )
            for telegram_id in recipients:
                enqueue(session, telegram_id, "broadcast", f"📢 Объявление студии\n\n{body}")
            audit(session, actor, "broadcast_queued", "announcement", recipients=len(recipients))
            return len(recipients)

    async def audit_page(self, actor_id: int, offset: int) -> list[tuple[AuditLog, str]]:
        async with self.db.sessions() as session:
            await require_user(session, actor_id, admin=True)
            query = select(AuditLog, User.display_name).join(User, User.id == AuditLog.actor_id)
            return [
                (item, name)
                for item, name in (
                    await session.execute(
                        query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                        .offset(offset)
                        .limit(9)
                    )
                ).all()
            ]
