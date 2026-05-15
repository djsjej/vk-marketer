"""Общий диалоговый агент — параметризован персоной.

Базовый класс используется как Бобой (CMO), так и существующим
Таргетологом, и любыми будущими агентами команды (Рита, Кирилл,
Тимур, Алина — когда дойдут до реализации).

Каждый агент = `DialogAgent(persona=ХХХ_PERSONA)`. Это даёт единообразную
архитектуру и убирает дублирование.

Использование:
    from src.agents.dialog import DialogAgent
    from src.agents import BOBA_PERSONA

    agent = DialogAgent(persona=BOBA_PERSONA)
    response = await agent.chat(
        user_message="Какую стратегию выберешь под пост?",
        history=context.user_data.get("boba_history", []),
    )
    # response.text — ответ агента
    # response.updated_history — обновлённая история (положить в user_data)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from anthropic import AsyncAnthropic

from src.config import settings

logger = logging.getLogger(__name__)


@dataclass
class AgentResponse:
    """Результат одного turn'а разговора с агентом."""

    text: str
    updated_history: list[dict]


class DialogAgent:
    """Параметризованный персоной разговорный агент.

    Лёгкий — без БД, без долговременной памяти. История диалога живёт
    в context.user_data на стороне Telegram bot framework, передаётся
    в каждый chat() как аргумент.

    Будущие расширения (Phase 7+):
    - tools: agent сам вызывает наши функции через function calling
      (например Боба сам запускает vk_audience когда видит что нужно)
    - персистентная память через PostgreSQL — агент помнит диалоги
      предыдущих дней
    - vision input — агент видит фото и комментирует креативы
    """

    # Сколько последних сообщений храним в истории (10 user + 10 assistant).
    # Старые забываются — это норм, контекст текущей задачи важнее истории
    # недельной давности.
    MAX_HISTORY_MESSAGES = 20

    # Максимум токенов в ответе. 2048 хватает на длинные ответы со
    # списком стратегий и обоснованием. Если будет мало — поднимем.
    MAX_TOKENS = 2048

    def __init__(
        self,
        persona: str,
        api_key: str | None = None,
        model: str | None = None,
    ):
        """
        Args:
            persona: системный промпт описывающий характер агента.
                Например BOBA_PERSONA из src.agents.personas.
            api_key: Anthropic API ключ. По умолчанию из settings.
            model: модель Claude. По умолчанию из settings (Sonnet 4).
        """
        if not persona or len(persona) < 100:
            raise ValueError(
                "Persona слишком короткая или пустая — это похоже на ошибку. "
                "Проверь что передал правильную константу из src.agents.personas."
            )
        self.persona = persona
        self.client = AsyncAnthropic(api_key=api_key or settings.anthropic_api_key)
        self.model = model or settings.anthropic_model

    async def chat(
        self,
        user_message: str,
        history: list[dict] | None = None,
    ) -> AgentResponse:
        """Один turn разговора с агентом.

        Args:
            user_message: что написал собеседник (Vizit или Claude).
            history: список предыдущих сообщений в формате Anthropic API
                ([{"role": "user", "content": "..."}, ...]). None или []
                означает новый диалог.

        Returns:
            AgentResponse с текстом ответа и обновлённой историей.

        Raises:
            anthropic.* exceptions — пробрасываем наверх, в Telegram
            handler ловим и показываем дружелюбное сообщение об ошибке.
        """
        history = history or []

        # Добавляем новое сообщение собеседника в историю
        messages = history + [{"role": "user", "content": user_message}]

        # Обрезаем историю если она слишком длинная
        if len(messages) > self.MAX_HISTORY_MESSAGES:
            messages = messages[-self.MAX_HISTORY_MESSAGES:]

        logger.info(
            "DialogAgent.chat: history_len=%d, user_message_chars=%d",
            len(messages),
            len(user_message),
        )

        message = await self.client.messages.create(
            model=self.model,
            max_tokens=self.MAX_TOKENS,
            system=self.persona,
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
