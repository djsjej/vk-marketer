"""Базовая фабрика для агент-ботов. Каждый агент команды = свой
Telegram-бот, и весь их код одинаковый кроме персоны, приветствия
и project_context. Этот модуль — общая фабрика.

Phase 5.16 (17.05.2026).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.agents.dialog import DialogAgent
from src.config import settings

logger = logging.getLogger(__name__)


@dataclass
class AgentBotConfig:
    """Конфигурация одного агент-бота.

    Используется в build_agent_application() для генерации Application
    без дублирования кода для 5 разных агентов.
    """

    # Идентификатор агента (для логов и истории в user_data)
    agent_id: str  # "boba", "kirill", "timur", "rita", "alina"

    # Токен Telegram-бота — None означает что бот не запускается
    token: str | None

    # Системный промпт (персона) из src.agents.personas
    persona: str

    # Стартовое приветствие при /start. В стиле агента, без воды.
    greeting: str

    # Функция которая собирает контекст текущего проекта.
    # Каждый агент видит проект под своим углом:
    # - Боба: общий состояние, кампании, алерты
    # - Кирилл: какие тексты тестим, что работало
    # - Тимур: бюджеты, технические детали, баланс
    # - Рита: какие сегменты, портреты ЦА
    # - Алина: метрики (когда появятся)
    build_project_context: Callable[[], Awaitable[str]]


# Стандартные хендлеры — общие для всех агентов


async def _agent_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start у любого агент-бота — представление + сброс истории."""
    agent_id = context.bot_data.get("agent_id", "?")
    history_key = f"{agent_id}_history"
    context.user_data[history_key] = []
    greeting = context.bot_data.get("greeting", "Привет.")
    await update.message.reply_text(greeting)


async def _agent_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /reset — очистка истории."""
    agent_id = context.bot_data.get("agent_id", "?")
    history_key = f"{agent_id}_history"
    context.user_data[history_key] = []
    await update.message.reply_text(
        "История разговора очищена. Начинаем с чистого листа."
    )


async def _agent_handle_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Любое текстовое сообщение → агент отвечает через DialogAgent.

    История каждого агента хранится отдельно в user_data:
    ['boba_history'], ['kirill_history'], ['timur_history'] и т.д.
    """
    user_message = update.message.text
    if not user_message:
        return

    # Игнорируем сообщения не от владельца
    if update.effective_user.id != settings.telegram_owner_id:
        agent_id = context.bot_data.get("agent_id", "?")
        logger.warning(
            f"{agent_id}: чужой пользователь {update.effective_user.id} пишет, игнорируем"
        )
        return

    agent_id = context.bot_data.get("agent_id", "agent")
    persona = context.bot_data.get("persona", "")
    build_context = context.bot_data.get("build_project_context")
    history_key = f"{agent_id}_history"

    history = context.user_data.get(history_key, [])

    # Контекст проекта для конкретного агента
    project_context = await build_context() if build_context else ""

    enriched_message = (
        f"{project_context}\n\n---\n\nСообщение от Vizit'a:\n{user_message}"
        if project_context
        else user_message
    )

    # «Печатает...» индикатор
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    try:
        agent = DialogAgent(persona=persona)
        response = await agent.chat(
            user_message=enriched_message,
            history=history,
        )
    except Exception as e:
        logger.exception(f"{agent_id} dialog failed")
        await update.message.reply_text(
            f"Anthropic API подвис: {type(e).__name__}: {e}\n\n"
            f"Попробуй ещё раз через минуту."
        )
        return

    # Сохраняем историю (только реальное сообщение, без project_context)
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": response.final_text})
    context.user_data[history_key] = history[-20:]

    await update.message.reply_text(response.final_text)


async def _track_group_membership(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Когда бота добавляют в группу — запоминаем chat_id группы.

    Phase 5.16: подготовка к межагентным диалогам в Phase 5.17.
    После того как Vizit добавит бота в группу, мы сохраним её chat_id
    в group_state. В будущем боты смогут писать туда сообщения сами
    (не только в ответ на личку).
    """
    from src.telegram_bots import group_state

    agent_id = context.bot_data.get("agent_id", "?")

    chat = update.effective_chat
    new_member = update.my_chat_member.new_chat_member if update.my_chat_member else None

    if chat and chat.type in ("group", "supergroup") and new_member:
        if new_member.status in ("member", "administrator"):
            group_state.register_group(agent_id, chat.id, chat.title or "?")
            logger.info(
                f"[{agent_id}] Добавлен в группу '{chat.title}' (chat_id={chat.id})"
            )


def build_agent_application(config: AgentBotConfig) -> Application | None:
    """Создаёт Application для агент-бота из конфига.

    Args:
        config: AgentBotConfig — токен, персона, приветствие, project_context.

    Returns:
        Application или None если токен не задан.
    """
    if not config.token:
        logger.info(f"Токен для {config.agent_id} не задан — бот не запускается")
        return None

    application = (
        Application.builder()
        .token(config.token)
        .build()
    )

    # Сохраняем конфиг в bot_data чтобы хендлеры могли его читать
    application.bot_data["agent_id"] = config.agent_id
    application.bot_data["persona"] = config.persona
    application.bot_data["greeting"] = config.greeting
    application.bot_data["build_project_context"] = config.build_project_context

    # Фильтр — только владелец может общаться с агентом в личке
    owner_filter = filters.User(user_id=settings.telegram_owner_id)

    application.add_handler(
        CommandHandler("start", _agent_start, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("reset", _agent_reset, filters=owner_filter)
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & owner_filter,
            _agent_handle_message,
        )
    )

    # Phase 5.16: отслеживание добавления в группу
    from telegram.ext import ChatMemberHandler
    application.add_handler(
        ChatMemberHandler(_track_group_membership, ChatMemberHandler.MY_CHAT_MEMBER)
    )

    logger.info(f"Агент-бот '{config.agent_id}' сконфигурирован")
    return application
