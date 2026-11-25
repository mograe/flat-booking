from datetime import timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Chat, Message, Update

from app.bot.dispatcher import create_dispatcher
from app.database.models import BookingStatus, utcnow
from app.services.access import AccessService
from app.services.admin import AdminService
from app.services.availability import AvailabilityService
from app.services.booking import BookingService


async def test_booking_dialog_edits_screen_and_rejects_repeated_confirmation(studio, monkeypatch):
    db, _, settings, users, admin_user, member, _, guest = studio
    service = BookingService(db)
    dispatcher = create_dispatcher()
    bot = Bot("123456:fake")
    shown = []
    user = {"id": member.telegram_id, "is_bot": False, "first_name": "Музыкант"}
    chat = Chat(id=member.telegram_id, type="private")
    options = await settings.get()
    tomorrow = (utcnow().astimezone(ZoneInfo(options.timezone)) + timedelta(days=1)).date()

    async def request(bot, method, timeout=None):
        if isinstance(method, (SendMessage, EditMessageText)):
            screen = Message(
                message_id=42,
                date=utcnow(),
                chat=chat,
                text=method.text,
                reply_markup=method.reply_markup,
            )
            shown.append(screen)
            return screen
        return True

    api = AsyncMock(side_effect=request)
    monkeypatch.setattr(bot.session, "make_request", api)

    async def feed(**fields):
        update = Update.model_validate({"update_id": api.await_count + 1, **fields})
        await dispatcher.feed_update(
            bot,
            update,
            users=users,
            settings=settings,
            availability=AvailabilityService(db),
            booking=service,
            admin=AdminService(db),
            access=AccessService(db, users.admin_ids),
        )

    async def press(data, from_screen=None):
        await feed(
            callback_query={
                "id": "callback",
                "from": user,
                "chat_instance": "test",
                "data": data,
                "message": (from_screen or shown[-1]).model_dump(),
            }
        )

    async def message(text):
        await feed(
            message={"message_id": 1, "date": utcnow(), "chat": chat, "from": user, "text": text}
        )

    try:
        await feed(
            message={
                "message_id": 1,
                "date": utcnow(),
                "chat": chat,
                "from": user,
                "text": "/start",
            }
        )
        await press("book")
        today = utcnow().astimezone(ZoneInfo(options.timezone)).date()
        if tomorrow.month != today.month:
            await press(f"bk:month:{tomorrow.replace(day=1)}")
        await press(f"bk:day:{tomorrow}")
        await press("bk:time:600")
        lengths = {
            button.callback_data
            for row in shown[-1].reply_markup.inline_keyboard
            for button in row
            if button.callback_data.startswith("bk:length:")
        }
        assert lengths == {
            f"bk:length:{value}" for value in (60, 120, 180, 240, 360, 480, 720, 1080, 1440)
        }
        await press("bk:manual-length")
        await message("1:20")
        assert "соответствовать шагу" in shown[-1].text
        await message("24")
        await press("bk:purpose:vocal")
        await press("bk:skip")
        assert "Начало:" in shown[-1].text
        assert "Окончание:" in shown[-1].text
        assert "Продолжительность: 24 часа" in shown[-1].text
        confirmation = shown[-1].reply_markup.inline_keyboard[0][0].callback_data
        await press(confirmation)
        await press(confirmation)
        assert "подтверждено" in shown[-1].text
        records = await service.list(member.id, 0)
        assert len(records) == 1
        member_booking, _ = records[0]
        member_screen = shown[-1]
        assert sum(isinstance(call.args[1], SendMessage) for call in api.await_args_list) == 2
        user["id"] = admin_user.telegram_id
        chat = Chat(id=admin_user.telegram_id, type="private")
        await message("/start")
        await press("ad:home")
        await press("ad:announce")
        await message("Новые кабели на стойке")
        assert "Предпросмотр" in shown[-1].text
        preview = shown[-1].reply_markup.inline_keyboard[0][0].callback_data
        await press(preview)
        await press(preview)
        assert "поставлено в очередь" in shown[-1].text
        entries = await AdminService(db).audit_page(admin_user.id, 0)
        assert sum(item.action == "broadcast_queued" for item, _ in entries) == 1
        await press("ad:home")
        await press("p:admin")
        await press("ad:book")
        await press("ad:owner:0")
        await press("ad:manual")
        start = tomorrow + timedelta(days=30)
        await message(f"{start:%d.%m.%Y} 10:00\n{start:%d.%m.%Y} 15:00")
        await press("bk:purpose:mix")
        await press("bk:skip")
        confirmation = shown[-1].reply_markup.inline_keyboard[0][0].callback_data
        await press(confirmation)
        assert "подтверждено" in shown[-1].text
        assert len(await service.list(admin_user.id, 0, admin=True)) == 2

        await press("home")
        await press("ad:home")
        await press("ad:wl")
        await press("ad:wl:add")
        await press("ad:wl:id")
        await message("4321")
        await message("Гитарист")
        assert "Комментарий: Гитарист" in shown[-1].text
        assert "бот ещё не запускался" in shown[-1].text
        await press("ad:wl")
        await press("ad:wl:find")
        await message("4321")
        assert "Доступ: разрешён" in shown[-1].text
        await press("ad:wl")
        await press("ad:wl:add")
        await press("ad:wl:pick:0")
        await press(f"ad:wl:choose:{guest.id}")
        await press("ad:wl:skip")
        assert "зарегистрирован" in shown[-1].text
        await press("ad:wl")
        await press("ad:wl:remove-list:0")
        await press(f"ad:wl:remove:{member.telegram_id}")
        assert "Существующие бронирования сохранятся" in shown[-1].text
        await press("ad:wl:drop")
        assert "Предстоящих бронирований: 1" in shown[-1].text
        await press(f"ad:wl:up:{member.telegram_id}:0")
        await press(f"p:adminitem:{member_booking.id}:{member.telegram_id}")
        saved, _ = await service.get(admin_user.id, member_booking.id)
        assert saved.status == BookingStatus.CONFIRMED
        await press(f"p:admincancel:{member_booking.id}:{member.telegram_id}")
        await press(
            f"p:admindrop:{member_booking.id}:{member_booking.version}:{member.telegram_id}"
        )
        assert (await service.get(admin_user.id, member_booking.id))[
            0
        ].status == BookingStatus.CANCELLED
        await press(f"ad:wl:up:{member.telegram_id}:0")
        assert "Предстоящих бронирований на странице нет" in shown[-1].text

        user["id"] = member.telegram_id
        chat = Chat(id=member.telegram_id, type="private")
        await press(f"p:item:{member_booking.id}", from_screen=member_screen)
        denied = api.await_args.args[1]
        assert isinstance(denied, AnswerCallbackQuery)
        assert denied.text == "У вас нет доступа к этому действию."
        await message("/start")
        assert shown[-1].text.startswith("🔒 Студия «Флэт»")
        assert f"Ваш Telegram ID: {member.telegram_id}" in shown[-1].text
        assert [row[0].text for row in shown[-1].reply_markup.inline_keyboard] == [
            "📨 Запросить доступ",
            "💬 Связаться с администратором",
        ]

    finally:
        await dispatcher.fsm.close()
        await bot.session.close()
