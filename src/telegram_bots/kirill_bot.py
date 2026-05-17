"""Кирилл — креативщик команды vk-marketer.

Подчиняется Бобе (Layer 4 в иерархии Рекламной Организации Vizit).
Отвечает за тексты, заголовки, идеи креативов.
"""

from __future__ import annotations

import logging

from telegram.ext import Application

from src.agents.personas import KIRILL_PERSONA
from src.config import settings
from src.telegram_bots.base import AgentBotConfig, build_agent_application

logger = logging.getLogger(__name__)


GREETING = (
    "Привет, я Кирилл. Креативщик в команде, подчиняюсь Бобе.\n\n"
    "Что делаю:\n"
    "— пишу тексты объявлений (заголовки, основной текст, CTA)\n"
    "— придумываю идеи под конкретные боли ЦА\n"
    "— разбираю чужие креативы и говорю что работает, что нет\n"
    "— знаю что цепляет в православной нише\n\n"
    "Не делаю красивые слова ради красивых слов. Каждый текст — "
    "под конкретную ситуацию и страх человека.\n\n"
    "Давай задачи или обсудим идеи."
)


async def build_project_context() -> str:
    """Кирилл видит креативный контекст: какие тексты в работе, что зашло."""
    parts = ["[КОНТЕКСТ — креативная картина проекта]"]

    # Активные тексты из текущих кампаний
    try:
        from src.vk_ads.client import VKAdsClient

        client = VKAdsClient.from_settings()
        if client is not None:
            active = await client.get_active_ad_plans()
            if active:
                parts.append(f"Активных кампаний в работе: {len(active)}")
                # Имена кампаний намекают на тематику текстов
                names = [c.get("name", "?") for c in active[:8]]
                parts.append("Темы которые сейчас тестим:")
                for n in names:
                    parts.append(f"• {n}")
    except Exception as e:
        logger.warning(f"Kirill context: VK Ads недоступен: {e}")

    # TODO Phase 5.17+: Алина передаёт Кириллу данные о победителях
    # ('тексты A,B зашли с CTR 1.2%', 'C,D провалились')
    parts.append("\nМетрик пока нет — кампании на модерации или только запустились.")

    return "\n".join(parts)


def build_kirill_application() -> Application | None:
    config = AgentBotConfig(
        agent_id="kirill",
        token=settings.tg_bot_kirill_token,
        persona=KIRILL_PERSONA,
        greeting=GREETING,
        build_project_context=build_project_context,
    )
    return build_agent_application(config)
