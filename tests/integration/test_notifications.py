import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

from sqlalchemy import event, select

from app.database.models import BookingStatus, Notification, NotificationStatus
from app.services.availability import Interval
from app.services.notifications import enqueue
from app.services.scheduler import DeliveryError, NotificationWorker


async def reminders(db, booking):
    async with db.sessions() as session:
        return list(
            await session.scalars(
                select(Notification)
                .where(Notification.booking_id == booking.id, Notification.starts_at.is_not(None))
                .order_by(Notification.booking_version, Notification.scheduled_at)
            )
        )


def later(service, hours, minutes=0):
    start = service.clock().replace(minute=0, second=0, microsecond=0)
    start += timedelta(hours=hours, minutes=minutes)
    return Interval(start, start + timedelta(hours=1))


async def test_reminder_creation_inside_24_hours_and_inside_one_hour(studio):
    db, service, _, _, _, member, *_ = studio
    two = await service.create(member.id, later(service, 25), "mix")
    one = await service.create(member.id, later(service, 4), "mix")
    none = await service.create(member.id, later(service, 1, 30), "mix")
    assert [n.notification_type for n in await reminders(db, two)] == ["reminder_24", "reminder_1"]
    assert [n.notification_type for n in await reminders(db, one)] == ["reminder_1"]
    assert await reminders(db, none) == []


async def test_long_booking_reminders_reference_start_and_completion_references_end(studio):
    db, service, _, _, _, member, *_ = studio
    start = later(service, 26).start
    item = await service.create(member.id, Interval(start, start + timedelta(hours=24)), "mix")
    notices = await reminders(db, item)
    assert [notice.scheduled_at for notice in notices] == [
        start - timedelta(hours=24),
        start - timedelta(hours=1),
    ]
    assert all(notice.starts_at == start for notice in notices)

    now = item.end_at - timedelta(microseconds=1)
    worker = NotificationWorker(db, AsyncMock(), lambda: now)
    await worker.complete_bookings()
    assert (await service.get(member.id, item.id))[0].status == BookingStatus.CONFIRMED
    now = item.end_at
    await worker.complete_bookings()
    assert (await service.get(member.id, item.id))[0].status == BookingStatus.COMPLETED


async def test_move_and_cancel_invalidate_previous_reminders(studio):
    db, service, _, _, _, member, *_ = studio
    item = await service.create(member.id, later(service, 25), "mix")
    moved = await service.change(member.id, item.id, item.version, interval=later(service, 48))
    assert [n.status for n in await reminders(db, item)] == [
        NotificationStatus.CANCELLED,
        NotificationStatus.CANCELLED,
        NotificationStatus.PENDING,
        NotificationStatus.PENDING,
    ]
    await service.cancel(member.id, item.id, moved.version)
    assert all(n.status == NotificationStatus.CANCELLED for n in await reminders(db, item))


async def test_worker_sends_due_reminders_after_poll_delay_or_restart_without_repeats(studio):
    db, service, _, _, _, member, *_ = studio
    send = AsyncMock()
    item = await service.create(member.id, later(service, 26), "mix")
    now = item.start_at - timedelta(hours=24, seconds=1)
    worker = NotificationWorker(db, send, lambda: now)
    await worker.run_once()
    assert send.await_count == 1  # Confirmation.
    now += timedelta(seconds=30)
    await worker.run_once()
    assert [n.status for n in await reminders(db, item)] == [
        NotificationStatus.SENT,
        NotificationStatus.PENDING,
    ]
    assert send.await_count == 2
    assert (await reminders(db, item))[0].sent_at == now
    now = item.start_at - timedelta(minutes=40)
    restarted = NotificationWorker(db, send, lambda: now)
    await restarted.run_once()
    assert send.await_count == 3
    assert (await reminders(db, item))[-1].sent_at == now
    await restarted.run_once()
    assert send.await_count == 3


async def test_reminders_are_skipped_at_booking_start(studio):
    db, service, _, _, _, member, *_ = studio
    item = await service.create(member.id, later(service, 25), "mix")
    send = AsyncMock()
    worker = NotificationWorker(db, send, lambda: item.start_at)
    await worker.run_once()
    assert {n.status for n in await reminders(db, item)} == {NotificationStatus.SKIPPED}
    send.assert_awaited_once()  # Confirmation survives a restart.


async def test_booking_status_is_checked_separately_at_end_of_interval(studio):
    db, service, _, _, _, member, *_ = studio
    item = await service.create(member.id, later(service, 4), "mix")
    now = item.end_at - timedelta(microseconds=1)
    worker = NotificationWorker(db, AsyncMock(), lambda: now)
    await worker.complete_bookings()
    assert (await service.get(member.id, item.id))[0].status == BookingStatus.CONFIRMED
    now = item.end_at
    await worker.run_once()
    assert (await service.get(member.id, item.id))[0].status == BookingStatus.CONFIRMED
    await worker.complete_bookings()
    assert (await service.get(member.id, item.id))[0].status == BookingStatus.COMPLETED


async def test_idle_jobs_each_issue_one_read_and_no_writes(studio):
    db, service, *_ = studio
    statements = []

    @event.listens_for(db.engine.sync_engine, "before_cursor_execute")
    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    worker = NotificationWorker(db, AsyncMock(), service.clock)
    await worker.run_once()
    await worker.complete_bookings()
    assert len(statements) == 2
    assert all(statement.startswith("SELECT") for statement in statements)


async def test_scheduler_configures_independent_non_overlapping_jobs(studio):
    db, *_ = studio
    worker = NotificationWorker(
        db,
        AsyncMock(),
        notification_poll_interval_seconds=45,
        booking_status_check_interval_seconds=90,
    )
    await worker.start()
    try:
        jobs = {job.id: job for job in worker.scheduler.get_jobs()}
        assert {name: job.trigger.interval.total_seconds() for name, job in jobs.items()} == {
            "notifications": 45,
            "booking_status": 90,
        }
        assert all(job.max_instances == 1 and job.coalesce for job in jobs.values())
    finally:
        await worker.stop()


async def test_concurrent_notification_check_does_not_send_twice_or_hold_sqlite_lock(studio):
    db, service, _, _, _, member, *_ = studio
    item = await service.create(member.id, later(service, 4), "mix")
    now = item.start_at - timedelta(minutes=59)
    sending = asyncio.Event()
    release = asyncio.Event()

    async def pause_first_send(chat_id, body):
        sending.set()
        await release.wait()

    send = AsyncMock(side_effect=pause_first_send)
    worker = NotificationWorker(db, send, lambda: now)
    competing_send = AsyncMock()
    competing_worker = NotificationWorker(db, competing_send, lambda: now)
    delivery = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(sending.wait(), timeout=2)
        await asyncio.wait_for(competing_worker.run_once(), timeout=2)
        await asyncio.wait_for(service.create(member.id, later(service, 5), "mix"), timeout=2)
        send.assert_awaited_once()
        competing_send.assert_not_awaited()
    finally:
        release.set()
        await delivery

    assert (await reminders(db, item))[0].status == NotificationStatus.SENT
    assert send.await_count == 2  # First reminder and first booking confirmation.


async def test_rate_limited_reminder_retries_before_start(studio):
    db, service, _, _, _, member, *_ = studio
    item = await service.create(member.id, later(service, 4), "mix")
    now = item.start_at - timedelta(minutes=59)
    send = AsyncMock(side_effect=[DeliveryError("TelegramRetryAfter", retry_after=40), None, None])
    worker = NotificationWorker(db, send, lambda: now)

    await worker.run_once()
    assert (await reminders(db, item))[0].status == NotificationStatus.PENDING
    now += timedelta(seconds=30)
    await worker.run_once()
    assert send.await_count == 1
    now += timedelta(seconds=30)
    await worker.run_once()
    assert (await reminders(db, item))[0].status == NotificationStatus.SENT
    assert send.await_count == 3
    await worker.run_once()
    assert send.await_count == 3


async def test_due_queue_drains_across_multiple_batches(studio):
    db, service, _, _, _, member, *_ = studio
    async with db.write() as session:
        for number in range(13):
            enqueue(session, member.telegram_id, "broadcast", str(number), at=service.clock())
    send = AsyncMock()
    worker = NotificationWorker(db, send, service.clock)
    await worker.run_once()
    assert [call.args[1] for call in send.await_args_list] == [str(n) for n in range(13)]
    await worker.run_once()
    assert send.await_count == 13


async def test_group_notifications_exclude_private_comment(studio):
    db, service, settings, _, admin, member, *_ = studio
    await settings.update(
        admin.id, {"studio_chat_id": -100123, "group_notifications_enabled": True}
    )
    await service.create(member.id, later(service, 4), "mix", comment="Личное")
    send = AsyncMock()
    await NotificationWorker(db, send, service.clock).run_once()
    assert {call.args[0] for call in send.await_args_list} == {-100123, member.telegram_id}
    assert all("Личное" not in call.args[1] for call in send.await_args_list)


async def test_enabling_reminder_updates_future_bookings(studio):
    db, service, settings, _, admin, member, *_ = studio
    await settings.update(admin.id, {"reminder_24h_enabled": False})
    item = await service.create(member.id, later(service, 26), "mix")
    assert [row.notification_type for row in await reminders(db, item)] == ["reminder_1"]
    await settings.update(admin.id, {"reminder_24h_enabled": True})
    assert [row.notification_type for row in await reminders(db, item)] == [
        "reminder_24",
        "reminder_1",
    ]
