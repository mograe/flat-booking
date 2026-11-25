from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import StudioOptions
from app.database.models import StudioSettings, utcnow
from app.database.repositories.accounts import audit, user_for_whitelist
from app.database.session import Database
from app.exceptions import DomainError
from app.services.notifications import restore_enabled_reminders
from app.services.users import approve_pending_request, grant_membership, require_user


async def load_options(session: AsyncSession) -> StudioOptions:
    row = await session.get(StudioSettings, 1)
    if row is None:
        raise RuntimeError("Настройки студии не инициализированы")
    return StudioOptions.model_validate(row.options)


class SettingsService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def initialize(
        self, defaults: StudioOptions, whitelist_ids: frozenset[int] = frozenset()
    ) -> None:
        async with self.db.write() as session:
            row = await session.get(StudioSettings, 1)
            if row is None:
                row = StudioSettings(id=1, options=defaults.model_dump())
                session.add(row)
            if not row.whitelist_initialized:
                now = utcnow()
                for telegram_id in sorted(whitelist_ids):
                    user = await user_for_whitelist(session, telegram_id)
                    if grant_membership(user, now):
                        await approve_pending_request(session, user, None, now)
                row.whitelist_initialized = True

    async def get(self) -> StudioOptions:
        async with self.db.sessions() as session:
            return await load_options(session)

    async def update(self, actor_id: int, changes: dict[str, object]) -> StudioOptions:
        async with self.db.reminder_lock, self.db.write() as session:
            actor = await require_user(session, actor_id, admin=True)
            row = await session.get(StudioSettings, 1)
            if row is None:
                raise RuntimeError("Настройки студии не инициализированы")
            if changes.keys() - StudioOptions.model_fields.keys():
                raise DomainError("Неизвестная настройка.")
            try:
                options = StudioOptions.model_validate({**row.options, **changes})
            except ValidationError as error:
                raise DomainError("Проверьте формат и допустимые значения настроек.") from error
            previous = StudioOptions.model_validate(row.options)
            await restore_enabled_reminders(session, previous, options, utcnow())
            row.options = options.model_dump()
            audit(session, actor, "settings_updated", "studio", 1, fields=list(changes))
            return options
