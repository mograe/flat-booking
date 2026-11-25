import asyncio
from datetime import timedelta

import pytest

from app.database.models import BookingStatus, RequestStatus, Role
from app.exceptions import AccessDenied, DomainError
from app.services.access import AccessService
from app.services.admin import AdminService
from app.services.availability import Interval


async def test_access_request_and_review(studio):
    db, _, _, users, admin, member, _, guest = studio
    access = AccessService(db, frozenset({admin.telegram_id}))
    assert await access.status(guest.id) is None
    await asyncio.gather(access.submit(guest.id), access.submit(guest.id))
    pending = await access.pending(admin.id, 0)
    assert len(pending) == 1
    request = pending[0][0]
    with pytest.raises(AccessDenied):
        await access.review(member.id, request.id, True)
    await access.review(admin.id, request.id, False)
    assert await access.status(guest.id) == RequestStatus.REJECTED
    with pytest.raises(DomainError):
        await access.review(admin.id, request.id, True)
    await access.submit(guest.id)
    second = (await access.pending(admin.id, 0))[0][0]
    await access.review(admin.id, second.id, True)
    assert await access.status(guest.id) == RequestStatus.APPROVED
    assert (await users.get(admin.id, guest.id)).role == Role.MEMBER
    assert (await users.whitelist_entry(admin.id, guest.telegram_id)).whitelist_added_at is not None
    with pytest.raises(AccessDenied):
        await access.submit(guest.id)


async def test_add_approves_pending_and_settings_survive_initialization(studio):
    db, _, settings, users, admin, _, _, guest = studio
    access = AccessService(db, frozenset())
    await access.submit(guest.id)
    await users.add(admin.id, guest.telegram_id)
    assert await access.status(guest.id) == RequestStatus.APPROVED
    original = await settings.get()
    await settings.update(admin.id, {"studio_name": "Флэт 2"})
    await settings.initialize(original)
    assert (await settings.get()).studio_name == "Флэт 2"


async def test_whitelist_grants_access_by_id_and_tracks_first_registration(studio):
    _, booking, _, users, admin, member, _, _ = studio
    day = booking.clock().date()
    assert await booking.schedule(admin.id, day) == ([], [])
    assert (await users.get(admin.id, admin.id)).whitelist_added_at is None
    assert await booking.schedule(member.id, day) == ([], [])

    telegram_id = 11223344
    added = await users.add(admin.id, telegram_id, "Вокал")
    assert added.registered_at is None
    assert added.whitelist_added_at is not None
    repeat = await users.add(admin.id, telegram_id, "Другой комментарий")
    assert (repeat.id, repeat.whitelist_added_at, repeat.whitelist_comment) == (
        added.id,
        added.whitelist_added_at,
        "Вокал",
    )
    registered = await users.register(telegram_id, "Имя", None, "same")
    assert registered.id == added.id
    assert registered.role == Role.MEMBER and registered.registered_at is not None
    assert await booking.schedule(registered.id, day) == ([], [])

    stranger = await users.register(99887766, "Имя", None, "same")
    start = booking.clock().replace(hour=10, minute=0, second=0, microsecond=0)
    with pytest.raises(AccessDenied):
        await booking.create(stranger.id, Interval(start, start + timedelta(hours=1)), "mix")
    with pytest.raises(AccessDenied):
        await booking.schedule(stranger.id, day)
    with pytest.raises(AccessDenied):
        await users.whitelist_page(stranger.id, 0)
    with pytest.raises(AccessDenied):
        await users.add(member.id, 99887766)
    with pytest.raises(AccessDenied):
        await users.remove(member.id, telegram_id)


async def test_revocation_keeps_bookings_and_readdition_preserves_block(studio, monkeypatch):
    db, service, _, users, admin, member, *_ = studio
    monkeypatch.setattr("app.services.users.utcnow", service.clock)
    start = service.clock().replace(hour=10, minute=0, second=0, microsecond=0)
    item = await service.create(member.id, Interval(start, start + timedelta(hours=24)), "mix")
    await users.set_active(admin.id, member.id, False)
    with pytest.raises(AccessDenied):
        await service.schedule(member.id, start.date())
    with pytest.raises(DomainError, match="ADMIN_IDS"):
        await users.remove(admin.id, admin.telegram_id)

    assert await users.remove(admin.id, member.telegram_id) == 1
    with pytest.raises(DomainError, match="не найден"):
        await users.whitelist_entry(admin.id, member.telegram_id)
    with pytest.raises(AccessDenied):
        await service.get(member.id, item.id)
    assert [row.id for row in await users.upcoming(admin.id, member.telegram_id, 0)] == [item.id]
    saved, _ = await service.get(admin.id, item.id)
    assert saved.status == BookingStatus.CONFIRMED

    readded = await users.add(admin.id, member.telegram_id)
    assert not readded.is_active
    with pytest.raises(AccessDenied):
        await service.get(member.id, item.id)
    await service.cancel(admin.id, item.id, item.version)
    assert (await service.get(admin.id, item.id))[0].status == BookingStatus.CANCELLED
    actions = {event.action for event, _ in await AdminService(db).audit_page(admin.id, 0)}
    assert {"whitelist_added", "whitelist_removed"} <= actions
