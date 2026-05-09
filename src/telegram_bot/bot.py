"""Конструктор Telegram-бота: настройка хендлеров, фильтров, middleware."""

import logging

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from src.config import settings
from src.telegram_bot.handlers import (
    handle_photo,
    handle_text,
    start_command,
    help_command,
    status_command,
)

logger = logging.getLogger(__name__)


def build_bot() -> Application:
    """Создаёт и настраивает Telegram Application с хендлерами."""
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .build()
    )

    # Фильтр: принимаем сообщения только от владельца
    owner_filter = filters.User(user_id=settings.telegram_owner_id)

    # Команды
    application.add_handler(
        CommandHandler("start", start_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("help", help_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("status", status_command, filters=owner_filter)
    )

    # Сообщения
    application.add_handler(
        MessageHandler(filters.PHOTO & owner_filter, handle_photo)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & owner_filter, handle_text)
    )

    logger.info(f"Бот настроен, owner_id={settings.telegram_owner_id}")
    return application
