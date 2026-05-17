"""Алина — аналитик результатов команды vk-marketer.

Подчиняется Бобе. Отвечает за анализ метрик после запуска: что работает,
что нет, какие связки победили. Делает первые срезы через 24 часа после
старта кампаний.
"""

from __future__ import annotations

import logging
from datetime import datetime

from telegram.ext import Application

from src.agents.personas import ALINA_PERSONA
from src.config import settings
from src.telegram_bots.base import AgentBotConfig, build_agent_application

logger = logging.getLogger(__name__)


GREETING = (
    "Алина, аналитик результатов. Подчиняюсь Бобе.\n\n"
    "Моя работа — смотреть что получилось у наших кампаний и говорить "
    "конкретными цифрами: что зашло, что нет, какие связки победили.\n\n"
    "Без воды. Если данных нет — скажу 'данных нет, рано выводы делать'. "
    "Если есть — будет конкретно: 'связка X+Y дала 18 сообщений за 1500₽, "
    "это в 3 раза лучше чем средняя'.\n\n"
    "Сейчас 12 кампаний только запустились. Через 24 часа после старта "
    "будут первые цифры — приходи, расскажу."
)


async def build_project_context() -> str:
    """Алина видит контекст метрик: активные кампании, что Сторож говорит."""
    parts = ["[КОНТЕКСТ — что у нас по метрикам]"]

    # Активные кампании
    try:
        from src.vk_ads.client import VKAdsClient

        client = VKAdsClient.from_settings()
        if client is not None:
            active = await client.get_active_ad_plans()
            parts.append(f"Активных кампаний: {len(active)}")

            # Метрики за сегодня
            if active:
                today = datetime.now().strftime("%Y-%m-%d")
                campaign_ids = [int(c["id"]) for c in active]
                try:
                    stats_response = await client.get_campaign_stats(
                        campaign_ids=campaign_ids,
                        date_from=today,
                        date_to=today,
                    )
                    items = stats_response.get("items", []) if isinstance(
                        stats_response, dict
                    ) else []

                    total_shows = 0
                    total_clicks = 0
                    total_spent = 0
                    total_leads = 0
                    for item in items:
                        total = item.get("total") or {}
                        if not total and isinstance(item.get("rows"), list) and item["rows"]:
                            total = item["rows"][0]
                        total_shows += int(total.get("shows", 0) or 0)
                        total_clicks += int(total.get("clicks", 0) or 0)
                        total_spent += float(total.get("spent", 0) or 0)
                        total_leads += int(total.get("goals", 0) or 0)

                    parts.append(
                        f"Суммарно за сегодня: показов {total_shows}, "
                        f"кликов {total_clicks}, потрачено {total_spent:.0f}₽, "
                        f"сообщений {total_leads}"
                    )
                    if total_shows == 0:
                        parts.append(
                            "(всё на модерации или только запустились — данных пока нет)"
                        )
                except Exception as e:
                    logger.warning(f"Alina context: не смог получить метрики: {e}")
    except Exception as e:
        logger.warning(f"Alina context: VK Ads недоступен: {e}")
        parts.append("VK Ads клиент не отвечает")

    # Сторож — он мой коллега, ловит проблемы быстро
    try:
        from src.scheduler import bad_campaigns_state
        bad_ids, last_update = bad_campaigns_state.get_bad_ids()
        if bad_ids:
            parts.append(
                f"Сторож нашёл {len(bad_ids)} проблемных кампаний — "
                f"им нужен мой анализ почему упали"
            )
    except Exception:
        pass

    return "\n".join(parts)


def build_alina_application() -> Application | None:
    config = AgentBotConfig(
        agent_id="alina",
        token=settings.tg_bot_alina_token,
        persona=ALINA_PERSONA,
        greeting=GREETING,
        build_project_context=build_project_context,
    )
    return build_agent_application(config)
