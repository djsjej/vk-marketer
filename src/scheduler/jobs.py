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
    """Утренний отчёт владельцу: вчерашние метрики + анализ Claude (Трек C).

    До этого был заглушкой ('TODO: подключить VK Ads'). Теперь:
    1. Берём метрики за вчера по всем кампаниям кабинета.
    2. Прогоняем через ClaudeAnalyzer (мозг-аналитик, до сих пор не был
       подключён к реальным данным).
    3. Шлём владельцу: живые цифры + вывод/проблемы/рекомендации.

    Рекомендации НЕ исполняются автоматически — отчёт информирует, решает
    человек (human gate из Конституции). Деградация: если Claude недоступен
    или VK не настроен — всё равно шлём то, что есть (цифры без анализа).
    """
    from datetime import datetime, timedelta

    from src.vk_ads.client import VKAdsClient
    from src.vk_ads.models import aggregate_stats_item

    logger.info("Запуск утреннего отчёта")

    client = VKAdsClient.from_settings()
    if client is None:
        await _safe_send(bot, "🌅 *Утренний отчёт*\n\nVK Ads не настроен — нет данных за вчера.")
        return

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        campaigns = await client.get_campaigns()
        campaign_ids = [int(c["id"]) for c in campaigns if c.get("id")]
        name_by_id = {int(c["id"]): c.get("name", "?") for c in campaigns if c.get("id")}
    except Exception as e:
        logger.exception(f"[Отчёт] Не смог получить кампании: {e}")
        await _safe_send(bot, f"🌅 *Утренний отчёт*\n\nНе смог получить кампании из VK: {e}")
        return

    stats_list = []
    if campaign_ids:
        try:
            resp = await client.get_campaign_stats(
                campaign_ids=campaign_ids, date_from=yesterday, date_to=yesterday
            )
            items = resp.get("items", []) if isinstance(resp, dict) else []
            for item in items:
                s = aggregate_stats_item(item)
                # В отчёт берём только кампании что реально крутились вчера.
                if s is not None and s.impressions > 0:
                    stats_list.append(s)
        except Exception as e:
            logger.exception(f"[Отчёт] Не смог получить метрики за вчера: {e}")

    if not stats_list:
        await _safe_send(
            bot,
            f"🌅 *Утренний отчёт за {yesterday}*\n\n"
            f"Вчера активных показов не было — кампании не крутились.",
        )
        return

    # Цифры — детерминированно, своими руками (не доверяем числа Claude).
    total_spent = sum(s.spent_rub for s in stats_list)
    total_leads = sum(s.leads for s in stats_list)
    total_clicks = sum(s.clicks for s in stats_list)
    avg_cpl = total_spent / total_leads if total_leads else 0.0

    lines = [
        f"🌅 *Утренний отчёт за {yesterday}*\n",
        f"Кампаний крутилось: *{len(stats_list)}*",
        f"Потрачено: *{total_spent:.0f}₽*",
        f"Кликов: *{total_clicks}*, написавших: *{total_leads}*",
    ]
    if total_leads:
        lines.append(f"Средний CPL: *{avg_cpl:.0f}₽* (норма ≤50₽)")
    else:
        lines.append("Средний CPL: _нет написавших_")

    # Шаг C: итоги накопленного опыта + предложение следующего раунда.
    try:
        from src.knowledge.round_analyzer import summarize_knowledge

        summary = summarize_knowledge()
        lines.append(f"\n*Обучение:* {summary['recommendation']}")
    except Exception as e:
        logger.warning(f"[Отчёт] round_analyzer недоступен: {e}")

    # Анализ Claude — best effort. Если упал, цифры уже собраны выше.
    analysis = await _analyze_morning(stats_list)
    if analysis:
        if analysis.get("summary"):
            lines.append(f"\n*Вывод:* {analysis['summary']}")
        for p in analysis.get("problems", [])[:5]:
            lines.append(f"⚠️ {p}")
        for w in analysis.get("winners", [])[:5]:
            lines.append(f"✅ {w}")
        recs = analysis.get("recommendations", [])
        if recs:
            lines.append("\n*Рекомендации* (решаешь ты):")
            for r in recs[:5]:
                cid = r.get("campaign_id") or r.get("based_on")
                cname = name_by_id.get(cid, cid)
                lines.append(f"• {r.get('action', '?')} `{cname}` — {r.get('reason', '')}")

    await _safe_send(bot, "\n".join(lines))


async def _analyze_morning(stats_list: list) -> dict | None:
    """Прогоняет метрики через ClaudeAnalyzer. None при любой ошибке."""
    try:
        from src.claude_brain.analyzer import ClaudeAnalyzer

        return await ClaudeAnalyzer().analyze_campaigns(stats_list)
    except Exception as e:
        logger.warning(f"[Отчёт] ClaudeAnalyzer недоступен, шлю отчёт без анализа: {e}")
        return None


async def _safe_send(bot: Bot, text: str) -> None:
    """Отправка в Telegram с Markdown, не падает при ошибке."""
    try:
        await bot.send_message(
            chat_id=settings.telegram_owner_id, text=text, parse_mode="Markdown"
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

    from src.db.repository import log_action
    from src.knowledge.recorder import record_campaign_result

    items = stats_response.get("items", []) if isinstance(stats_response, dict) else []
    snapshots = []  # для записи истории в БД (Трек B)
    auto_stopped: list[tuple[int, str, str]] = []  # (id, name, reason) — выключены сами
    for item in items:
        stats = aggregate_stats_item(item)
        if stats is None:
            continue

        snapshots.append(stats)

        cid = stats.campaign_id
        name = name_by_id.get(cid, "?")

        decision = check_campaign_anomaly(stats)
        if decision.action != "alert":
            # Шаг B: дешёвую кампанию с реальными конверсиями фиксируем как
            # рабочую связку (CPL ≤ норма «хорошо» 30₽, есть написавшие).
            if stats.leads >= 3 and stats.cpl_rub and stats.cpl_rub <= 30:
                record_campaign_result(
                    campaign_id=cid, name=name, impressions=stats.impressions,
                    clicks=stats.clicks, spent_rub=stats.spent_rub, leads=stats.leads,
                    cpl_rub=stats.cpl_rub, won=True,
                )
            continue

        # Шаг A автономии: Сторож САМ выключает дорогую кампанию. Пороги
        # уже консервативны (CPL судим только после 100₽ расхода), пауза
        # безопасна (только останавливает трату). Если авто-стоп выключен
        # или пауза не удалась — оставляем как алерт, решает человек.
        if settings.auto_stop_enabled:
            try:
                await client.pause_campaign(cid)
                await log_action(
                    "pause", target_id=cid,
                    reason=f"Авто-стоп Сторожа: {decision.reason[:200]}", auto=True,
                )
                auto_stopped.append((cid, name, decision.reason))
                logger.info(f"[Сторож] Авто-стоп кампании {cid} ({name})")
                # Шаг B: провал фиксируем в базу знаний (учимся на ошибках).
                record_campaign_result(
                    campaign_id=cid, name=name, impressions=stats.impressions,
                    clicks=stats.clicks, spent_rub=stats.spent_rub, leads=stats.leads,
                    cpl_rub=stats.cpl_rub, won=False, reason=decision.reason[:200],
                )
                continue
            except Exception as e:
                logger.error(f"[Сторож] Не смог авто-выключить {cid}: {e}")

        alerts.append((cid, name, decision.reason))

    # Память организации: каждый проход Сторожа фиксируем метрики в БД.
    # Это даёт историю динамики для утреннего отчёта и детекта усталости.
    from src.db.repository import save_stats_snapshots
    saved = await save_stats_snapshots(snapshots)
    logger.info(f"[Сторож] Записано снапшотов в БД: {saved}")

    # Сохраняем список «жёлтых» для /kill_bad (только те что не выключили сами)
    from src.scheduler import bad_campaigns_state
    bad_campaigns_state.set_bad_ids([a[0] for a in alerts])

    if not auto_stopped and not alerts:
        logger.info("[Сторож] Все кампании в норме")
        return

    lines: list[str] = []

    # Что Сторож выключил САМ (Шаг A автономии)
    if auto_stopped:
        lines.append(
            f"🛑 *Сторож сам выключил {len(auto_stopped)} объявлени"
            f"{'е' if len(auto_stopped) == 1 else 'я' if 2 <= len(auto_stopped) <= 4 else 'й'}* "
            f"(дорого по CPL / не работают):\n"
        )
        for cid, name, reason in auto_stopped[:15]:
            lines.append(f"\n`{name}` (`{cid}`)")
            lines.append(reason)
        if len(auto_stopped) > 15:
            lines.append(f"\n_...и ещё {len(auto_stopped) - 15}_")

    # Что не смог выключить сам — оставляем человеку
    if alerts:
        lines.append(
            f"\n\n⚠️ *Не смог выключить сам {len(alerts)} — выключи вручную* "
            f"(`/kill_bad` или `/pause <id>`):\n"
        )
        for cid, name, reason in alerts[:15]:
            lines.append(f"\n`{name}` (`{cid}`)")
            lines.append(reason)

    try:
        await bot.send_message(
            chat_id=settings.telegram_owner_id,
            text="".join(lines),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"[Сторож] Не смог отправить отчёт: {e}")


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
