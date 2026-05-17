"""Тимур — таргетолог команды vk-marketer.

Подчиняется Бобе. Отвечает за технику: бюджеты, ставки, ad_groups,
баннеры, package_id, всё что под капотом VK Ads.
"""

from __future__ import annotations

import logging

from telegram.ext import Application

from src.agents.personas import TIMUR_PERSONA
from src.config import settings
from src.telegram_bots.base import AgentBotConfig, build_agent_application

logger = logging.getLogger(__name__)


GREETING = (
    "Тимур, таргетолог. Подчиняюсь Бобе.\n\n"
    "Знаю VK Ads технику изнутри: ad_plans, ad_groups, banners, package_id, "
    "стратегии ставок, targetings, segments — всё это моё.\n\n"
    "Знаю что у нас сейчас:\n"
    "— баланс кабинета\n"
    "— активные кампании и их параметры\n"
    "— подключенные сегменты\n"
    "— что Сторож говорит про текущее состояние\n\n"
    "Спрашивай по технике — отвечу с цифрами, без воды."
)


async def build_project_context() -> str:
    """Тимур видит техническую картину: бюджеты, кампании, баланс."""
    parts = ["[КОНТЕКСТ — техническая картина]"]

    parts.append(f"Кабинет VK Ads: {settings.vk_ads_account_id}")

    # Активные кампании
    try:
        from src.vk_ads.client import VKAdsClient

        client = VKAdsClient.from_settings()
        if client is not None:
            active = await client.get_active_ad_plans()
            parts.append(f"Активных кампаний: {len(active)}")
            if active:
                # Краткая статистика
                names = [c.get("name", "?") for c in active[:5]]
                parts.append(f"Примеры: {', '.join(names)}")
    except Exception as e:
        logger.warning(f"Timur context: VK Ads недоступен: {e}")
        parts.append("VK Ads клиент не отвечает")

    # Сегменты
    segments = settings.vk_audience_segment_ids_parsed
    if segments:
        parts.append(f"Подключённые сегменты: {segments}")
    else:
        parts.append("Сегменты в env не заданы")

    # Сторож
    try:
        from src.scheduler import bad_campaigns_state
        bad_ids, last_update = bad_campaigns_state.get_bad_ids()
        if bad_ids:
            parts.append(f"Сторож: {len(bad_ids)} кампаний с проблемами")
        elif last_update > 0:
            from datetime import datetime
            age_min = int((datetime.now().timestamp() - last_update) / 60)
            parts.append(f"Сторож проверял {age_min} мин назад — норма")
    except Exception:
        pass

    return "\n".join(parts)


def build_timur_application() -> Application | None:
    config = AgentBotConfig(
        agent_id="timur",
        token=settings.tg_bot_timur_token,
        persona=TIMUR_PERSONA,
        greeting=GREETING,
        build_project_context=build_project_context,
    )
    return build_agent_application(config)
