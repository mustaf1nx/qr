from datetime import datetime, timezone
from sqlalchemy import create_engine, String, Integer, ForeignKey, UniqueConstraint, Text, Boolean
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from .config import settings


def now():
    return int(datetime.now(timezone.utc).timestamp())


url = settings.database_url.replace("postgres://", "postgresql+psycopg://", 1)
if url.startswith("postgresql://"):
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)
engine = create_engine(url, pool_pre_ping=True, connect_args={"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {})
Session = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    starts_at: Mapped[int | None] = mapped_column(nullable=True)
    expires_at: Mapped[int | None] = mapped_column(nullable=True)
    capacity: Mapped[int | None] = mapped_column(nullable=True)
    issued_count: Mapped[int] = mapped_column(default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    require_follow: Mapped[bool] = mapped_column(Boolean, default=True)
    keyword: Mapped[str] = mapped_column(String(50), unique=True)


class Participant(Base):
    __tablename__ = "participants"
    id: Mapped[int] = mapped_column(primary_key=True)
    igsid: Mapped[str] = mapped_column(String(100), unique=True)
    username: Mapped[str] = mapped_column(String(100))
    follows: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (UniqueConstraint("event_id", "participant_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"))
    token: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="issued")
    created_at: Mapped[int] = mapped_column(default=now)
    redeemed_at: Mapped[int | None] = mapped_column(nullable=True)
    redeemed_by: Mapped[int | None] = mapped_column(nullable=True)


class Staff(Base):
    __tablename__ = "staff_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class LoginSession(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff_users.id"))
    expires_at: Mapped[int]


class Checkin(Base):
    __tablename__ = "checkins"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int]
    ticket_id: Mapped[int | None] = mapped_column(nullable=True)
    staff_id: Mapped[int]
    result: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[int] = mapped_column(default=now)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    mid: Mapped[str] = mapped_column(String(255), primary_key=True)
    sender: Mapped[str] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(String(1000))
    state: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[int] = mapped_column(default=now)
    reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[int] = mapped_column(default=now)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(100), index=True)
    created_at: Mapped[int] = mapped_column(default=now)
