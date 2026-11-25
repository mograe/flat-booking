from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.ui import Screen, navigation
from app.database.models import RequestStatus, Role, User
from app.exceptions import AccessDenied
from app.services.access import AccessService

router = Router(name="access")
REQUEST_STATUSES = {
    RequestStatus.PENDING: "На рассмотрении",
    RequestStatus.APPROVED: "Одобрена",
    RequestStatus.REJECTED: "Отклонена",
}


@router.callback_query(F.data.in_({"access", "access:send"}))
async def access_panel(
    query: CallbackQuery,
    screen: Screen,
    actor: User,
    access: AccessService,
) -> None:
    if not actor.is_active or actor.role != Role.GUEST:
        raise AccessDenied()
    await screen.reset()
    if query.data == "access:send":
        await access.submit(actor.id)
    status = await access.status(actor.id)
    label = REQUEST_STATUSES.get(status, "Заявка ещё не отправлена")
    rows = []
    if status != RequestStatus.PENDING:
        rows.append([("Отправить заявку", "access:send")])
    rows.append([("Связь с администратором", "contact")])
    await screen.show(f"📨 Заявка на доступ\n\n{label}", rows + navigation())
