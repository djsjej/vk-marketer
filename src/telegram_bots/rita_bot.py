"""Рита — аналитик ЦА команды vk-marketer.

Подчиняется Бобе. Отвечает за психологические портреты ЦА, боли,
паттерны поведения. Сейчас работает с базой 619k горячих
православных из TargetHunter.
"""

from __future__ import annotations

import logging

from telegram.ext import Application

from src.agents.personas import RITA_PERSONA
from src.config import settings
from src.telegram_bots.base import AgentBotConfig, build_agent_application

logger = logging.getLogger(__name__)


GREETING = (
    "Я Рита, аналитик ЦА. Подчиняюсь Бобе.\n\n"
    "Изучаю кто наша аудитория — что у них болит, какие триггеры срабатывают, "
    "что они ищут когда заходят в социальные сети.\n\n"
    "Сейчас знаю:\n"
    "— у нас 619k 'горячих' православных собранных через TargetHunter\n"
    "— возрастной диапазон 41-58, преимущественно женщины\n"
    "— подключены к рекламе как сегмент в VK Ads\n"
    "— тестируем 3 возрастных среза: 41-46, 47-52, 53-58\n\n"
    "Спрашивай про портрет, психологию, инсайты — отвечу содержательно."
)


async def build_project_context() -> str:
    """Рита видит контекст ЦА: сегменты, парсинг, гипотезы."""
    parts = ["[КОНТЕКСТ — наша целевая аудитория]"]

    # Сегменты
    segments = settings.vk_audience_segment_ids_parsed
    if segments:
        parts.append(f"Активные сегменты в кампаниях: {segments}")
        parts.append(
            "Источник: парсинг 4 православных сообществ через TargetHunter "
            "(pravoslavnie_hristiane, p_trapeza, orthodoxpsiholog, ortodoxia). "
            "Берём пользователей которые подписаны минимум на 2 из 4 — это "
            "'горячая' выборка с двойным подтверждением интереса."
        )

    parts.append(
        "Возрастные группы в тестах: 41-46 (молодые мамы), "
        "47-52 (зрелые), 53-58 (старшие). Пол: female. География: РФ."
    )

    # TODO Phase 5.17+: Алина передаёт Рите данные кто реально кликает
    parts.append(
        "\nДанных о реальных кликах пока нет — кампании только запустились. "
        "После первой волны Алина передаст метрики, и я смогу уточнить портрет."
    )

    return "\n".join(parts)


def build_rita_application() -> Application | None:
    config = AgentBotConfig(
        agent_id="rita",
        token=settings.tg_bot_rita_token,
        persona=RITA_PERSONA,
        greeting=GREETING,
        build_project_context=build_project_context,
    )
    return build_agent_application(config)
