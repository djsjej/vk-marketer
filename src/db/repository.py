"""Запись истории кампаний в БД (Трек B — память организации).

До этого модуля модели `StatsSnapshot` и `ActionLog` были объявлены, но
никогда не писались — бот был «слепым»: открутил рекламу, но после
рестарта Railway не помнил ни метрик, ни своих действий. Без истории
невозможны ни честный утренний отчёт с динамикой, ни детект усталости
креативов, ни записи в базу знаний.

Принцип: запись в БД НИКОГДА не должна ронять вызывающего. Сторож и
бюджетный стоп — критичны; если SQLite/Postgres моргнул, мы логируем
ошибку и продолжаем работать, а не падаем.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from src.db.models import ActionLog, StatsSnapshot
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)


async def save_stats_snapshots(stats_list: Iterable) -> int:
    """Сохраняет снимки метрик кампаний (по одному на кампанию за проход).

    Принимает любые объекты с атрибутами campaign_id / impressions /
    clicks / spent_rub / leads (наш `CampaignStats`). Возвращает число
    записанных строк (0 при ошибке — не поднимает исключение).
    """
    rows = [
        StatsSnapshot(
            campaign_id=s.campaign_id,
            impressions=s.impressions,
            clicks=s.clicks,
            spent_rub=s.spent_rub,
            leads=s.leads,
        )
        for s in stats_list
    ]
    if not rows:
        return 0
    try:
        async with SessionLocal() as session:
            session.add_all(rows)
            await session.commit()
        return len(rows)
    except Exception:
        logger.exception("save_stats_snapshots: не смог записать снапшоты")
        return 0


async def log_action(
    action: str,
    *,
    target_id: int | None = None,
    reason: str = "",
    auto: bool = True,
) -> None:
    """Пишет одну запись в аудит-лог действий бота (pause/resume/create/…).

    Не поднимает исключение — аудит не должен ломать само действие.
    """
    try:
        async with SessionLocal() as session:
            session.add(
                ActionLog(
                    action=action,
                    target_id=target_id,
                    reason=reason,
                    auto=auto,
                )
            )
            await session.commit()
    except Exception:
        logger.exception(f"log_action: не смог записать действие {action}")
