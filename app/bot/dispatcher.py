from aiogram import Dispatcher, Router
from aiogram.fsm.storage.memory import SimpleEventIsolation

from app.bot.handlers import access, admin, booking, profile, schedule, start
from app.bot.middleware import InteractionMiddleware, unknown_callback, unknown_message


def create_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    middleware = InteractionMiddleware()
    dispatcher.message.outer_middleware(middleware)
    dispatcher.callback_query.outer_middleware(middleware)
    for module in (start, booking, profile, schedule, access, admin):
        dispatcher.include_router(copy_handlers(module.router))

    fallback = Router(name="fallback")
    fallback.callback_query.register(unknown_callback)
    fallback.message.register(unknown_message)
    dispatcher.include_router(fallback)
    return dispatcher


def copy_handlers(source: Router) -> Router:
    router = Router(name=source.name)
    for event in ("message", "callback_query"):
        router.observers[event].handlers.extend(source.observers[event].handlers)
    for child in source.sub_routers:
        router.include_router(copy_handlers(child))
    return router
