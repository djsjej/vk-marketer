"""Общий диалоговый агент — параметризован персоной.

Базовый класс используется как Бобой (CMO), так и существующим
Таргетологом, и любыми будущими агентами команды (Рита, Кирилл,
Тимур, Алина — когда дойдут до реализации).

Каждый агент = `DialogAgent(persona=ХХХ_PERSONA)`. Это даёт единообразную
архитектуру и убирает дублирование.

Использование БЕЗ tools (старый режим):
    from src.agents.dialog import DialogAgent
    from src.agents import BOBA_PERSONA

    agent = DialogAgent(persona=BOBA_PERSONA)
    response = await agent.chat(
        user_message="Какую стратегию выберешь под пост?",
        history=context.user_data.get("boba_history", []),
    )

Использование С tools (Phase 7+):
    from src.agents.boba_tools import BOBA_TOOLS_SCHEMA, execute_tool

    agent = DialogAgent(persona=BOBA_PERSONA)
    response = await agent.chat(
        user_message="Какой статус кабинета?",
        history=...,
        tools=BOBA_TOOLS_SCHEMA,
        tool_executor=execute_tool,
    )
    # Боба сам вызовет get_account_status() через tool use loop
    # и вернёт текстовый ответ Vizit'у
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
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
        tools: list[dict] | None = None,
        tool_executor: Callable[[str, dict], Awaitable[str]] | None = None,
        max_tool_iterations: int = 5,
    ) -> AgentResponse:
        """Один turn разговора с агентом.

        Args:
            user_message: что написал собеседник (Vizit или Claude).
            history: список предыдущих сообщений в формате Anthropic API
                ([{"role": "user", "content": "..."}, ...]). None или []
                означает новый диалог.
            tools: список tool definitions для передачи в Anthropic API
                (формат BOBA_TOOLS_SCHEMA). Если None — режим без tools.
            tool_executor: async функция (name, args) -> result_str для
                выполнения tool calls. Обязательна если tools передан.
            max_tool_iterations: максимум итераций tool use loop. Защита
                от бесконечных циклов если агент зациклился на tools.

        Returns:
            AgentResponse с финальным текстом ответа и обновлённой
            историей (включая tool_use и tool_result блоки если были).

        Raises:
            anthropic.* exceptions — пробрасываем наверх, в Telegram
            handler ловим и показываем дружелюбное сообщение об ошибке.
            ValueError — если tools передан без tool_executor.
        """
        if tools and not tool_executor:
            raise ValueError(
                "tool_executor обязателен когда передаются tools — иначе "
                "вызовы tools некому будет обрабатывать"
            )

        history = history or []

        # Добавляем новое сообщение собеседника в историю
        messages = history + [{"role": "user", "content": user_message}]

        # Обрезаем историю если она слишком длинная
        if len(messages) > self.MAX_HISTORY_MESSAGES:
            messages = messages[-self.MAX_HISTORY_MESSAGES:]

        logger.info(
            "DialogAgent.chat: history_len=%d, user_message_chars=%d, "
            "tools=%s",
            len(messages),
            len(user_message),
            len(tools) if tools else 0,
        )

        # Tool use loop — продолжаем пока агент не выдаст финальный текст
        # без tool_use блоков (или пока не превысим max_tool_iterations).
        for iteration in range(max_tool_iterations):
            api_kwargs = {
                "model": self.model,
                "max_tokens": self.MAX_TOKENS,
                "system": self.persona,
                # Передаём копию — последующие мутации messages не должны
                # влиять на отправленный payload (важно для тестируемости
                # и для будущих параллельных вызовов).
                "messages": list(messages),
            }
            if tools:
                api_kwargs["tools"] = tools

            message = await self.client.messages.create(**api_kwargs)

            # Собираем все блоки контента
            tool_uses = [b for b in message.content if b.type == "tool_use"]
            text_blocks = [b for b in message.content if b.type == "text"]

            # Добавляем ответ ассистента в историю (с tool_use блоками если есть).
            # ВАЖНО: пропускаем ПУСТЫЕ text-блоки. Anthropic API отвергает
            # сообщения, где есть text-блок с пустой строкой ("") — это
            # классическая причина BadRequestError (400). Claude иногда
            # возвращает пустой text рядом с tool_use; если положить его в
            # историю, следующий запрос падает с 400, и весь диалог Бобы
            # ломается. Фильтруем здесь.
            assistant_content: list[dict] = []
            for block in message.content:
                if block.type == "text":
                    if block.text and block.text.strip():
                        assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )

            # Пустой ассистентский ход тоже отвергается API — добавляем в
            # историю только если есть что добавить.
            if assistant_content:
                messages.append({"role": "assistant", "content": assistant_content})

            # Если нет tool_use блоков — это финальный ответ, выходим
            if not tool_uses:
                response_text = "".join(b.text for b in text_blocks).strip()
                if not response_text:
                    response_text = (
                        "Боба задумался, но не ответил словами. "
                        "Переформулируй вопрос чуть иначе."
                    )
                return AgentResponse(text=response_text, updated_history=messages)

            # Иначе — выполняем все tool calls параллельно (по факту последовательно)
            # и возвращаем результаты как tool_result блоки.
            tool_results: list[dict] = []
            for tool_use in tool_uses:
                logger.info(
                    f"Tool call iteration {iteration + 1}: "
                    f"{tool_use.name}({tool_use.input})"
                )
                try:
                    result_text = await tool_executor(tool_use.name, tool_use.input)
                except Exception as e:
                    logger.exception(f"Tool {tool_use.name} crashed in executor")
                    result_text = f"❌ Внутренняя ошибка: {type(e).__name__}: {e}"

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": result_text,
                    }
                )

            # Tool results идут как user message
            messages.append({"role": "user", "content": tool_results})

            # Продолжаем loop — Боба обработает результаты и либо выдаст
            # финальный ответ, либо вызовет ещё tools

        # Превысили max_tool_iterations — это аномалия
        logger.warning(
            f"Tool use loop достиг лимита {max_tool_iterations} итераций. "
            f"Возможно агент зациклился на tools."
        )
        return AgentResponse(
            text=(
                "⚠️ Не смог завершить размышление — слишком много вызовов "
                "инструментов подряд. Попробуй переформулировать вопрос проще."
            ),
            updated_history=messages,
        )
