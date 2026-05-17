"""Точка входа приложения. Поднимает Telegram-бота(ов) и планировщик задач."""

import asyncio
import logging
import sys

from src.config import settings
from src.db.session import init_db
from src.scheduler.jobs import setup_scheduler
from src.telegram_bot.bot import build_bot
from src.telegram_bots.boba_bot import build_boba_application

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    """Настройка JSON-логирования для Railway."""
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # Уменьшаем шум от httpx и telegram
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)


async def main() -> None:
    """Главная асинхронная точка входа."""
    setup_logging()
    logger.info("=== Запуск vk-marketer v0.1.0 ===")

    # БД
    await init_db()
    logger.info("База данных инициализирована")

    # Главный Telegram бот (Claude — диспетчер команд)
    application = build_bot()
    await application.initialize()
    await application.start()
    if application.updater:
        await application.updater.start_polling()
    logger.info("Главный Telegram-бот запущен")

    # Phase 5.15: Опционально — отдельный бот Бобы (если задан TG_BOT_BOBA_TOKEN)
    boba_app = build_boba_application()
    if boba_app:
        await boba_app.initialize()
        await boba_app.start()
        if boba_app.updater:
            await boba_app.updater.start_polling()
        logger.info("Боба-бот запущен параллельно с главным")

    # Планировщик задач (использует главный бот для отправки уведомлений)
    scheduler = setup_scheduler(application.bot)
    scheduler.start()
    logger.info("Планировщик задач запущен")

    # Уведомление владельцу о старте
    try:
        startup_msg = (
            "🚀 vk-marketer запущен и готов к работе.\n\n"
            "Команды:\n"
            "/start — начало работы\n"
            "/help — справка\n"
            "/status — статус кампаний"
        )
        if boba_app:
            startup_msg += (
                "\n\n👔 Боба-бот тоже запущен. Открой чат с ним и пиши /start."
            )
        await application.bot.send_message(
            chat_id=settings.telegram_owner_id,
            text=startup_msg,
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить стартовое сообщение: {e}")

    # Бесконечный цикл — ждём сигнала остановки
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Получен сигнал остановки")
    finally:
        scheduler.shutdown()
        if application.updater:
            await application.updater.stop()
        await application.stop()
        await application.shutdown()
        if boba_app:
            if boba_app.updater:
                await boba_app.updater.stop()
            await boba_app.stop()
            await boba_app.shutdown()
        logger.info("Приложение остановлено")


if __name__ == "__main__":
    asyncio.run(main())
