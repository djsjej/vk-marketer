"""Боба — CMO команды vk-marketer (стратег)."""

from __future__ import annotations

import logging

from telegram.ext import Application

from src.agents import BOBA_PERSONA
from src.config import settings
from src.telegram_bots.base import AgentBotConfig, build_agent_application

logger = logging.getLogger(__name__)


GREETING = (
    "Слушай, я Боба. CMO команды по VK-маркетингу твоего проекта.\n\n"
    "За 13 лет в digital видел и хорошее, и провальное. Сейчас разбираюсь "
    "в твоей теме с молитвенным сообществом — нетривиальная ниша.\n\n"
    "Что могу делать:\n"
    "— обсуждать стратегию, креативы, аудитории\n"
    "— разбирать метрики кампаний после запуска\n"
    "— ставить задачи Кириллу/Тимуру/Рите/Алине\n"
    "— называть вещи своими именами без воды\n\n"
    "Пиши что у тебя сейчас на уме. Я в курсе текущих кампаний."
)


async def build_project_context() -> str:
    """Боба видит общий контекст: кампании, алерты Сторожа, баланс."""
    parts = ["[КОНТЕКСТ — для тебя, не цитируй дословно]"]

    try:
        from src.vk_ads.client import VKAdsClient

        client = VKAdsClient.from_settings()
        if client is not None:
            active = await client.get_active_ad_plans()
            parts.append(f"Активных кампаний: {len(active)}")
            if active:
                names = [c.get("name", "?") for c in active[:5]]
                parts.append(f"Примеры: {', '.join(names)}")
    except Exception as e:
        logger.warning(f"Boba context: VK Ads недоступен: {e}")
        parts.append("VK Ads клиент не отвечает")

    try:
        from src.scheduler import bad_campaigns_state
        bad_ids, last_update = bad_campaigns_state.get_bad_ids()
        if bad_ids:
            parts.append(
                f"Сторож пометил {len(bad_ids)} кампаний с проблемами: "
                f"{', '.join(str(i) for i in bad_ids[:10])}"
            )
        elif last_update > 0:
            from datetime import datetime
            age_min = int((datetime.now().timestamp() - last_update) / 60)
            parts.append(f"Сторож проверял {age_min} мин назад — проблем нет")
    except Exception:
        pass

    return "\n".join(parts)


def build_boba_application() -> Application | None:
    config = AgentBotConfig(
        agent_id="boba",
        token=settings.tg_bot_boba_token,
        persona=BOBA_PERSONA,
        greeting=GREETING,
        build_project_context=build_project_context,
    )
    return build_agent_application(config)
