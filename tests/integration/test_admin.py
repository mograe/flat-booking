from sqlalchemy import select

from app.database.models import Notification
from app.services.admin import AdminService


async def test_broadcast_uses_only_active_members_and_audits_without_body(studio):
    db, _, _, users, actor, member, blocked, guest = studio
    admin = AdminService(db)
    await users.set_active(actor.id, blocked.id, False)
    assert await admin.broadcast(actor.id, "Смена расписания") == 2
    async with db.sessions() as session:
        deliveries = list(
            await session.scalars(
                select(Notification).where(Notification.notification_type == "broadcast")
            )
        )
    assert {item.chat_id for item in deliveries} == {actor.telegram_id, member.telegram_id}
    entry, _ = (await admin.audit_page(actor.id, 0))[0]
    assert entry.details == {"recipients": 2}
