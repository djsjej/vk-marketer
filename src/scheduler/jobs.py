"""Задачи планировщика."""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Bot

from src.config import settings

logger = logging.getLogger(__name__)


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    """Конфигурирует планировщик со всеми задачами."""
    scheduler = AsyncIOScheduler(timezone=settings.tz)

    # Утренний отчёт каждый день
    hour, minute = map(int, settings.morning_report_time.split(":"))
    scheduler.add_job(
        send_morning_report,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=settings.tz),
        args=[bot],
        id="morning_report",
        replace_existing=True,
    )

    # Проверка метрик и аномалий
    scheduler.add_job(
        check_metrics_and_anomalies,
        trigger=IntervalTrigger(minutes=settings.metrics_check_interval_min),
        args=[bot],
        id="metrics_check",
        replace_existing=True,
    )

    # Проверка дневного бюджета (раз в 10 минут — критично)
    scheduler.add_job(
        check_daily_budget,
        trigger=IntervalTrigger(minutes=10),
        args=[bot],
        id="budget_check",
        replace_existing=True,
    )

    logger.info(
        f"Запланировано задач: морнинг ({settings.morning_report_time}), "
        f"метрики (каждые {settings.metrics_check_interval_min} мин), "
        f"бюджет (каждые 10 мин)"
    )
    return scheduler


async def send_morning_report(bot: Bot) -> None:
    """Утренний отчёт владельцу: вчерашние метрики и рекомендации."""
    logger.info("Запуск утреннего отчёта")
    # TODO: 
    # 1. Через VKAdsClient получить метрики за вчера
    # 2. Через ClaudeAnalyzer получить рекомендации
    # 3. Сформировать сообщение с inline-кнопками для подтверждений
    # 4. Отправить владельцу
    try:
        await bot.send_message(
            chat_id=settings.telegram_owner_id,
            text="🌅 Утренний отчёт\n\nTODO: подключить VK Ads и аналитику.",
        )
    except Exception as e:
        logger.error(f"Не удалось отправить утренний отчёт: {e}")


async def check_metrics_and_anomalies(bot: Bot) -> None:
    """Каждые N минут — проверка кампаний на аномалии."""
    logger.debug("Проверка метрик и аномалий")
    # TODO:
    # 1. Получить активные кампании и их свежие метрики
    # 2. Применить правила из src.scheduler.safety
    # 3. При нарушении правила — пауза и алерт владельцу
    pass


async def check_daily_budget(bot: Bot) -> None:
    """Каждые 10 минут — проверка дневного расхода. При превышении — стоп всё."""
    logger.debug("Проверка дневного бюджета")
    # TODO:
    # 1. Получить суммарный расход за сегодня
    # 2. Если >= settings.max_daily_spend_rub → остановить ВСЕ активные кампании
    # 3. Отправить алерт владельцу
    pass
