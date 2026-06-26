"""Тесты слоя памяти (Трек B) — запись StatsSnapshot и ActionLog в БД.

До этого модели существовали, но в них никто не писал. Эти тесты
гарантируют что:
- метрики Сторожа сохраняются (история динамики)
- авто-действия пишутся в аудит-лог
- падение БД не роняет вызывающего (graceful degradation)

Тест self-contained: поднимает свой временный SQLite-движок в рамках
event loop теста и подменяет SessionLocal в репозитории — без касания
реальной ./data базы и без конфликта event loop'ов.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import src.db.repository as repo
from src.db.models import ActionLog, Base, StatsSnapshot
from src.vk_ads.models import CampaignStats


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    """Временный SQLite на диске с созданными таблицами."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    with patch.object(repo, "SessionLocal", factory):
        yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_save_stats_snapshots_writes_rows(session_factory):
    stats = [
        CampaignStats(campaign_id=10, impressions=1000, clicks=20, spent_rub=150.0, leads=3),
        CampaignStats(campaign_id=11, impressions=2000, clicks=5, spent_rub=80.0, leads=0),
    ]
    n = await repo.save_stats_snapshots(stats)
    assert n == 2

    async with session_factory() as s:
        rows = (await s.execute(select(StatsSnapshot).order_by(StatsSnapshot.campaign_id))).scalars().all()
    assert [r.campaign_id for r in rows] == [10, 11]
    assert rows[0].spent_rub == 150.0
    assert rows[0].leads == 3
    assert rows[1].clicks == 5


@pytest.mark.asyncio
async def test_save_empty_is_noop(session_factory):
    assert await repo.save_stats_snapshots([]) == 0
    async with session_factory() as s:
        count = (await s.execute(select(func.count()).select_from(StatsSnapshot))).scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_log_action_writes_audit_row(session_factory):
    await repo.log_action("pause", target_id=42, reason="лимит достигнут", auto=True)

    async with session_factory() as s:
        rows = (await s.execute(select(ActionLog))).scalars().all()
    assert len(rows) == 1
    assert rows[0].action == "pause"
    assert rows[0].target_id == 42
    assert rows[0].auto is True
    assert "лимит" in rows[0].reason


@pytest.mark.asyncio
async def test_db_failure_does_not_raise():
    """Если БД недоступна — функции логируют и возвращают, не падают.

    Сторож и бюджетный стоп критичны: сбой записи истории не должен
    срывать само действие (выключение кампании)."""
    class _Boom:
        def __call__(self):
            raise RuntimeError("db down")

    with patch.object(repo, "SessionLocal", _Boom()):
        # Не должно поднять исключение
        assert await repo.save_stats_snapshots(
            [CampaignStats(campaign_id=1, impressions=1, clicks=1, spent_rub=1.0, leads=0)]
        ) == 0
        await repo.log_action("pause", target_id=1)
