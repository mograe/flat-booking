from sqlalchemy import select

from app.database.models import AccessRequest, RequestStatus, Role, User, utcnow
from app.database.repositories.accounts import audit
from app.database.session import Database
from app.exceptions import AccessDenied, DomainError
from app.services.notifications import enqueue
from app.services.users import grant_membership, require_user


class AccessService:
    def __init__(self, db: Database, admin_ids: frozenset[int]) -> None:
        self.db = db
        self.admin_ids = admin_ids

    @staticmethod
    def latest(user_id: int):
        return (
            select(AccessRequest)
            .where(AccessRequest.user_id == user_id)
            .order_by(AccessRequest.id.desc())
            .limit(1)
        )

    async def status(self, user_id: int) -> RequestStatus | None:
        async with self.db.sessions() as session:
            return await session.scalar(
                self.latest(user_id).with_only_columns(AccessRequest.status)
            )

    async def submit(self, user_id: int) -> None:
        async with self.db.write() as session:
            user = await session.get(User, user_id)
            if user is None or user.role != Role.GUEST or not user.is_active:
                raise AccessDenied()
            latest = await session.scalar(self.latest(user_id))
            if latest is not None and latest.status == RequestStatus.PENDING:
                return
            request = AccessRequest(user_id=user_id)
            session.add(request)
            await session.flush()
            for admin in self.admin_ids:
                enqueue(
                    session,
                    admin,
                    "access_new",
                    f"📨 Новая заявка на доступ\n👤 {user.display_name}",
                )

    async def pending(self, actor_id: int, offset: int) -> list[tuple[AccessRequest, User]]:
        async with self.db.sessions() as session:
            await require_user(session, actor_id, admin=True)
            query = select(AccessRequest, User).join(User, User.id == AccessRequest.user_id)
            return [
                (item, user)
                for item, user in (
                    await session.execute(
                        query.where(AccessRequest.status == RequestStatus.PENDING)
                        .order_by(AccessRequest.id)
                        .offset(offset)
                        .limit(9)
                    )
                ).all()
            ]

    async def details(self, actor_id: int, request_id: int) -> tuple[AccessRequest, User]:
        async with self.db.sessions() as session:
            await require_user(session, actor_id, admin=True)
            request = await session.get(AccessRequest, request_id)
            if request is None:
                raise DomainError("Заявка не найдена.")
            return request, await session.get_one(User, request.user_id)

    async def review(self, actor_id: int, request_id: int, approve: bool) -> None:
        async with self.db.write() as session:
            actor = await require_user(session, actor_id, admin=True)
            request = await session.get(AccessRequest, request_id)
            if request is None or request.status != RequestStatus.PENDING:
                raise DomainError("Заявка уже рассмотрена.")
            user = await session.get_one(User, request.user_id)
            request.status = RequestStatus.APPROVED if approve else RequestStatus.REJECTED
            request.reviewed_by, request.reviewed_at = actor.id, utcnow()
            if approve and grant_membership(user, request.reviewed_at):
                audit(session, actor, "whitelist_added", "user", user.id)
            enqueue(
                session,
                user.telegram_id,
                "access",
                "✅ Вам открыт доступ к студии."
                if approve
                else "Заявка отклонена. Свяжитесь с администратором.",
            )
            audit(session, actor, f"request_{request.status}", "access_request", request.id)
