"""Точка входа приложения. Поднимает Telegram-бота и планировщик задач."""

import asyncio
import logging
import sys

from src.config import settings
from src.db.session import init_db
from src.scheduler.jobs import setup_scheduler
from src.telegram_bot.bot import build_bot

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

    # Telegram бот
    application = build_bot()
    await application.initialize()
    await application.start()
    if application.updater:
        await application.updater.start_polling()
    logger.info("Telegram-бот запущен")

    # Планировщик задач
    scheduler = setup_scheduler(application.bot)
    scheduler.start()
    logger.info("Планировщик задач запущен")

    # Уведомление владельцу о старте
    try:
        await application.bot.send_message(
            chat_id=settings.telegram_owner_id,
            text="🚀 vk-marketer запущен и готов к работе.\n\n"
            "Команды:\n"
            "/start — начало работы\n"
            "/help — справка\n"
            "/status — статус кампаний",
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
        logger.info("Приложение остановлено")


if __name__ == "__main__":
    asyncio.run(main())
