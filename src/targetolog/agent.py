"""Разговорный агент-таргетолог (Phase 5.2).

По требованию Vizit'а (15.05.2026): «мне еще знаешь что надо чтобы там
агент был с кем можно было разговаривать и говорить что надо. У него
должны быть мозги».

Это первая итерация — агент **говорит**, но пока **не действует**
автономно. Он понимает контекст бизнеса (через PERSONA_SYSTEM_PROMPT),
помнит диалог, отвечает с экспертизой, **подсказывает** какие команды
нужно нажать в боте. Действовать сам — будет следующая итерация
(function calling — агент сам вызывает /vk_orthodox, /vk_parse, и т.д.).

Использование:
    agent = TargetologAgent()
    response = await agent.chat(
        user_message="Найди мне женскую православную аудиторию",
        history=context.user_data.get("agent_history", []),
    )
    # response.text — ответ агента
    # response.updated_history — обновлённая история для следующего turn
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from anthropic import AsyncAnthropic

from src.config import settings
from src.targetolog.persona import PERSONA_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


@dataclass
class AgentResponse:
    """Результат одного turn'а разговора с агентом."""

    text: str
    updated_history: list[dict]


class TargetologAgent:
    """Обёртка над Anthropic API для разговорного режима таргетолога.

    Лёгкий — без БД, без долговременной памяти. История диалога живёт
    в context.user_data на стороне Telegram bot framework, передаётся
    в каждый chat() как аргумент.

    В следующей итерации (Phase 5.2.5) добавим:
    - tools: agent сам вызывает наши команды (/vk_orthodox, /vk_parse, etc.)
    - персистентная память (через PostgreSQL) — агент помнит вчерашние диалоги
    """

    MAX_HISTORY_MESSAGES = 20  # храним N последних сообщений (10 user + 10 assistant)

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.client = AsyncAnthropic(api_key=api_key or settings.anthropic_api_key)
        self.model = model or settings.anthropic_model

    async def chat(
        self, user_message: str, history: list[dict] | None = None
    ) -> AgentResponse:
        """Один turn разговора.

        Args:
            user_message: что написал Vizit
            history: список предыдущих сообщений в формате Anthropic API
                ([{"role": "user", "content": "..."}, ...]). Передавать
                None или [] для нового диалога.

        Returns:
            AgentResponse с текстом ответа и обновлённой историей.

        Raises:
            anthropic.* exceptions — пробрасываем наверх, в Telegram
            handler уже ловим и показываем Vizit'у дружелюбное сообщение.
        """
        history = history or []

        # Добавляем новое сообщение Vizit'а в историю
        messages = history + [{"role": "user", "content": user_message}]

        # Обрезаем историю если она слишком длинная (экономим токены).
        # Сохраняем последние N — старые забываются. Для нашего случая
        # это норм: контекст текущей задачи важнее истории недельной давности.
        if len(messages) > self.MAX_HISTORY_MESSAGES:
            messages = messages[-self.MAX_HISTORY_MESSAGES:]

        logger.info(
            "TargetologAgent chat: history_len=%d, user_message_chars=%d",
            len(messages),
            len(user_message),
        )

        message = await self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=PERSONA_SYSTEM_PROMPT,
            messages=messages,  # type: ignore[arg-type]
        )

        # Anthropic SDK возвращает content как список блоков.
        # Для текстового ответа — один TextBlock, берём .text из него.
        response_text = "".join(
            block.text for block in message.content if hasattr(block, "text")
        )

        # Обновлённая история: + ответ ассистента
        updated_history = messages + [
            {"role": "assistant", "content": response_text}
        ]

        return AgentResponse(text=response_text, updated_history=updated_history)
