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
    """Сторож — Phase 5.12 (17.05.2026).

    Каждые N минут проверяет активные кампании, при пробитии порогов
    шлёт уведомление владельцу в Telegram. **Auto-pause не делает** —
    Vizit решает и выключает через `/pause <id>` или `/kill_bad`.

    Логика:
    1. GET всех активных кампаний (status='active')
    2. Для каждой кампании читаем метрики за сегодня
    3. Применяем safety правила (check_campaign_anomaly)
    4. Группируем все алерты в одно сообщение Vizit'у
    5. Сохраняем список «жёлтых» ID в персистентное хранилище для /kill_bad
    """
    from datetime import datetime

    from src.scheduler.safety import check_campaign_anomaly
    from src.vk_ads.client import VKAdsClient
    from src.vk_ads.models import CampaignStats

    logger.info("[Сторож] Запуск проверки активных кампаний")

    client = VKAdsClient.from_settings()
    if client is None:
        logger.warning("[Сторож] VKAdsClient не настроен, пропускаю")
        return

    try:
        active_campaigns = await client.get_active_ad_plans()
    except Exception as e:
        logger.exception(f"[Сторож] Не смог получить активные кампании: {e}")
        return

    if not active_campaigns:
        logger.info("[Сторож] Активных кампаний нет")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    campaign_ids = [int(c["id"]) for c in active_campaigns]

    logger.info(f"[Сторож] Проверяю {len(campaign_ids)} активных кампаний за {today}")

    try:
        stats_response = await client.get_campaign_stats(
            campaign_ids=campaign_ids,
            date_from=today,
            date_to=today,
        )
    except Exception as e:
        logger.exception(f"[Сторож] Не смог получить метрики: {e}")
        return

    # Парсим метрики и применяем правила
    name_by_id = {int(c["id"]): c.get("name", "?") for c in active_campaigns}
    alerts: list[tuple[int, str, str]] = []  # (id, name, reason)

    # Используем общий хелпер aggregate_stats_item — единственный источник
    # правды о формате ответа VK Ads Statistics API. Раньше тут была
    # дубликатная (и поломанная) копия парсера: искала shows/clicks/spent
    # на уровне `total`, а реально они в `rows[].base`. Сторож из-за
    # этого не выявлял аномалии, так как у всех stats.impressions было 0.
    from src.vk_ads.models import aggregate_stats_item

    items = stats_response.get("items", []) if isinstance(stats_response, dict) else []
    snapshots = []  # для записи истории в БД (Трек B)
    for item in items:
        stats = aggregate_stats_item(item)
        if stats is None:
            continue

        snapshots.append(stats)

        decision = check_campaign_anomaly(stats)
        if decision.action == "alert":
            alerts.append(
                (stats.campaign_id, name_by_id.get(stats.campaign_id, "?"), decision.reason)
            )

    # Память организации: каждый проход Сторожа фиксируем метрики в БД.
    # Это даёт историю динамики для утреннего отчёта и детекта усталости.
    from src.db.repository import save_stats_snapshots
    saved = await save_stats_snapshots(snapshots)
    logger.info(f"[Сторож] Записано снапшотов в БД: {saved}")

    # Сохраняем список «жёлтых» для /kill_bad
    from src.scheduler import bad_campaigns_state
    bad_campaigns_state.set_bad_ids([a[0] for a in alerts])

    if not alerts:
        logger.info("[Сторож] Все кампании в норме")
        return

    # Шлём групповое сообщение Vizit'у — человеческий язык (Phase 5.14)
    lines = [
        f"🐕 *Сторож докладывает: {len(alerts)} объявлени"
        f"{'е' if len(alerts) == 1 else 'й' if 2 <= len(alerts) <= 4 else 'й'} "
        f"работа{'ет' if len(alerts) == 1 else 'ют'} плохо*\n"
    ]
    for cid, name, reason in alerts[:15]:  # максимум 15 в одном сообщении
        lines.append(f"\n`{name}` (`{cid}`)")
        lines.append(reason)
    if len(alerts) > 15:
        lines.append(f"\n_...и ещё {len(alerts) - 15} объявлений с проблемами_")
    lines.append(
        f"\n\n*Что можно сделать:*\n"
        f"• Выключить одно: напиши `/pause <номер>` где <номер> — ID объявления выше\n"
        f"• Выключить сразу все {len(alerts)} проблемных: `/kill_bad`\n"
        f"• Ничего не делать — через час я снова посмотрю и напишу опять если ситуация не улучшится"
    )

    try:
        await bot.send_message(
            chat_id=settings.telegram_owner_id,
            text="".join(lines),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"[Сторож] Не смог отправить алерт: {e}")


async def check_daily_budget(bot: Bot) -> None:
    """Дневной рубильник бюджета — приоритет №1 (Конституция).

    Каждые 10 минут считает фактический расход за сегодня по ВСЕМ кампаниям
    кабинета (а не только активным — кампания могла открутить деньги и быть
    выключенной раньше в течение дня) и сверяет с `MAX_DAILY_SPEND` через
    `check_daily_spend()`. При достижении лимита — **выключает все активные
    кампании** и шлёт алерт владельцу.

    Это единственное место где Сторож действует автоматически, без human
    gate: дневной hard-stop — детерминированный предохранитель из Конституции
    («приоритет №1»), а не оптимизация по CPL (та остаётся за человеком).

    Защита от спама: если лимит уже пробит, но активных кампаний не осталось
    (мы их выключили на прошлом проходе), молча выходим — повторный алерт
    каждые 10 минут не нужен.
    """
    from datetime import datetime

    from src.scheduler.safety import check_daily_spend
    from src.vk_ads.client import VKAdsClient
    from src.vk_ads.models import aggregate_stats_item

    logger.info("[Бюджет] Проверка дневного расхода")

    client = VKAdsClient.from_settings()
    if client is None:
        logger.warning("[Бюджет] VKAdsClient не настроен, пропускаю")
        return

    # Список всех кампаний (любой статус) — для консервативного подсчёта
    # суммарного расхода за день.
    try:
        all_campaigns = await client.get_campaigns()
    except Exception as e:
        logger.exception(f"[Бюджет] Не смог получить список кампаний: {e}")
        return

    campaign_ids = [int(c["id"]) for c in all_campaigns if c.get("id")]
    if not campaign_ids:
        logger.info("[Бюджет] Кампаний в кабинете нет")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    try:
        stats_response = await client.get_campaign_stats(
            campaign_ids=campaign_ids,
            date_from=today,
            date_to=today,
        )
    except Exception as e:
        logger.exception(f"[Бюджет] Не смог получить метрики расхода: {e}")
        return

    items = stats_response.get("items", []) if isinstance(stats_response, dict) else []
    total_spent = 0.0
    for item in items:
        stats = aggregate_stats_item(item)
        if stats is not None:
            total_spent += stats.spent_rub

    decision = check_daily_spend(total_spent)
    logger.info(
        f"[Бюджет] Расход за {today}: {total_spent:.0f}₽ "
        f"из лимита {settings.max_daily_spend_rub}₽ → {decision.action}"
    )

    if decision.action != "stop_all":
        return

    # Лимит пробит. Выключаем все активные кампании.
    try:
        active_campaigns = await client.get_active_ad_plans()
    except Exception as e:
        logger.exception(f"[Бюджет] Лимит пробит, но не смог получить активные: {e}")
        active_campaigns = []

    if not active_campaigns:
        # Уже всё выключено на прошлом проходе — не спамим повторным алертом.
        logger.info("[Бюджет] Лимит пробит, но активных кампаний нет — выходим тихо")
        return

    paused: list[str] = []
    failed: list[str] = []
    for camp in active_campaigns:
        cid = int(camp["id"])
        name = camp.get("name", "?")
        try:
            await client.pause_campaign(cid)
            paused.append(f"`{name}` (`{cid}`)")
            logger.info(f"[Бюджет] Авто-стоп кампании {cid} ({name})")
            # Аудит: авто-действие с деньгами обязательно фиксируем (Трек B).
            from src.db.repository import log_action
            await log_action(
                "pause",
                target_id=cid,
                reason=f"Дневной лимит {settings.max_daily_spend_rub}₽ достигнут (расход {total_spent:.0f}₽)",
                auto=True,
            )
        except Exception as e:
            failed.append(f"`{name}` (`{cid}`)")
            logger.error(f"[Бюджет] Не смог выключить {cid}: {e}")

    lines = [
        f"🛑 *Дневной лимит бюджета достигнут*\n",
        decision.reason,
        f"\n\nВыключено кампаний: *{len(paused)}*",
    ]
    if paused:
        lines.append("\n" + "\n".join(paused))
    if failed:
        lines.append(
            f"\n\n⚠️ Не удалось выключить *{len(failed)}* "
            f"(проверь вручную через `/status`):\n" + "\n".join(failed)
        )

    try:
        await bot.send_message(
            chat_id=settings.telegram_owner_id,
            text="".join(lines),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"[Бюджет] Не смог отправить алерт о стопе: {e}")
