"""SQLAlchemy модели для локальной базы данных."""

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CampaignRecord(Base):
    """Локальный кеш кампании из VK Ads + наша мета."""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vk_campaign_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    topic: Mapped[str] = mapped_column(String(255))  # «Святой Спиридон, Корфу»
    status: Mapped[str] = mapped_column(String(32), default="active")
    daily_budget_rub: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_decision_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class StatsSnapshot(Base):
    """Снимок метрик кампании в моменте."""

    __tablename__ = "stats_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(Integer, index=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    spent_rub: Mapped[float] = mapped_column(Float, default=0.0)
    leads: Mapped[int] = mapped_column(Integer, default=0)


class ActionLog(Base):
    """Лог всех действий бота с рекламой."""

    __tablename__ = "action_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    action: Mapped[str] = mapped_column(String(64))  # pause, resume, create, etc.
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    auto: Mapped[bool] = mapped_column(default=True)  # автомат или подтверждение
