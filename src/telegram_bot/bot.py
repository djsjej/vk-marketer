"""Конструктор Telegram-бота: настройка хендлеров, фильтров, middleware."""

import logging

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from src.config import settings
from src.telegram_bot.handlers import (
    agent_command,
    audience_from_list_command,
    biba_command,
    boba_command,
    clean_tokens_command,
    handle_audience_document,
    handle_photo,
    handle_text,
    inspect_command,
    kill_bad_command,
    launch_batch_command,
    launch_test_command,
    launch_20_command,
    menu_callback,
    menu_command,
    on_callback,
    pause_command,
    pause_non_batch_command,
    set_batch_budget_command,
    start_command,
    help_command,
    status_command,
    upload_audience_command,
    vk_audience_command,
    vk_check_command,
    vk_orthodox_command,
    vk_parse_command,
    vk_search_command,
    watchdog_command,
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
    application.add_handler(
        CommandHandler("clean_tokens", clean_tokens_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("inspect", inspect_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("biba", biba_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("vk_check", vk_check_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("vk_search", vk_search_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("vk_parse", vk_parse_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("vk_orthodox", vk_orthodox_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("agent", agent_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("boba", boba_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("vk_audience", vk_audience_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("upload_audience", upload_audience_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("launch_test", launch_test_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("audience_from_list", audience_from_list_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("launch_batch", launch_batch_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("launch_20", launch_20_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("pause", pause_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("kill_bad", kill_bad_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("watchdog", watchdog_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("set_batch_budget", set_batch_budget_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("pause_non_batch", pause_non_batch_command, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("menu", menu_command, filters=owner_filter)
    )

    # Сообщения
    application.add_handler(
        MessageHandler(filters.PHOTO & owner_filter, handle_photo)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & owner_filter, handle_text)
    )
    # Документы — для /upload_audience (txt-файлы с VK ID из TargetHunter)
    application.add_handler(
        MessageHandler(filters.Document.ALL & owner_filter, handle_audience_document)
    )

    # Inline-кнопки. ВАЖЕН ПОРЯДОК: pattern-обработчик menu_callback должен
    # быть зарегистрирован раньше общего on_callback (без pattern), иначе
    # тот перехватит все события включая наши menu_*.
    application.add_handler(
        CallbackQueryHandler(menu_callback, pattern=r"^menu_")
    )
    application.add_handler(CallbackQueryHandler(on_callback))

    logger.info(f"Бот настроен, owner_id={settings.telegram_owner_id}")
    return application
