from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Требуется время с часовым поясом")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=UTC) if value is not None else None


class Role(StrEnum):
    GUEST = "guest"
    MEMBER = "member"
    ADMIN = "admin"


class BookingStatus(StrEnum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class RequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    FAILED = "failed"


def enum_column(enum: type[StrEnum]) -> Enum:
    return Enum(
        enum,
        values_callable=lambda values: [item.value for item in values],
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
    )


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime()}


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class User(Timestamped, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(128))
    role: Mapped[Role] = mapped_column(enum_column(Role), default=Role.GUEST)
    is_active: Mapped[bool] = mapped_column(default=True)
    registered_at: Mapped[datetime | None]
    whitelist_added_at: Mapped[datetime | None]
    whitelist_comment: Mapped[str | None] = mapped_column(String(200))


class Booking(Timestamped, Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("end_at > start_at", name="booking_positive_interval"),
        Index("ix_bookings_status_interval", "status", "start_at", "end_at"),
        Index("ix_bookings_user_start", "user_id", "start_at"),
        Index("ix_bookings_status_end", "status", "end_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    start_at: Mapped[datetime]
    end_at: Mapped[datetime]
    purpose: Mapped[str] = mapped_column(String(32))
    comment: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[BookingStatus] = mapped_column(
        enum_column(BookingStatus), default=BookingStatus.CONFIRMED
    )
    cancelled_at: Mapped[datetime | None]
    cancelled_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    confirmation_key: Mapped[str | None] = mapped_column(String(48), unique=True)
    version: Mapped[int] = mapped_column(default=1)


class StudioSettings(Base):
    __tablename__ = "studio_settings"
    __table_args__ = (CheckConstraint("id = 1", name="studio_settings_singleton"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    options: Mapped[dict] = mapped_column(JSON)
    whitelist_initialized: Mapped[bool] = mapped_column(default=False, server_default=false())


class AccessRequest(Base):
    __tablename__ = "access_requests"
    __table_args__ = (
        Index(
            "ix_access_requests_one_pending",
            "user_id",
            unique=True,
            sqlite_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[RequestStatus] = mapped_column(
        enum_column(RequestStatus), default=RequestStatus.PENDING, index=True
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    reviewed_at: Mapped[datetime | None]
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class BlockedTimeSlot(Base):
    __tablename__ = "blocked_time_slots"
    __table_args__ = (
        CheckConstraint("end_at > start_at", name="blocked_positive_interval"),
        Index("ix_blocked_interval", "start_at", "end_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    start_at: Mapped[datetime]
    end_at: Mapped[datetime]
    reason: Mapped[str] = mapped_column(String(300))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("booking_id", "booking_version", "notification_type"),
        Index("ix_notifications_due", "status", "scheduled_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id"))
    booking_version: Mapped[int | None]
    notification_type: Mapped[str] = mapped_column(String(32))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    body: Mapped[str] = mapped_column(Text)
    scheduled_at: Mapped[datetime]
    starts_at: Mapped[datetime | None]
    sent_at: Mapped[datetime | None]
    status: Mapped[NotificationStatus] = mapped_column(
        enum_column(NotificationStatus), default=NotificationStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(String(100))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(40))
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[int | None]
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
