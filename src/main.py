"""Точка входа приложения. Поднимает Telegram-бота(ов) и планировщик задач."""

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

    # Главный Telegram бот (Claude — диспетчер команд)
    application = build_bot()
    await application.initialize()
    await application.start()
    if application.updater:
        await application.updater.start_polling()
    logger.info("Главный Telegram-бот запущен")

    # Phase 5.15/5.16: Опциональные агент-боты (если заданы соответствующие токены)
    from src.telegram_bots.boba_bot import build_boba_application
    from src.telegram_bots.kirill_bot import build_kirill_application
    from src.telegram_bots.timur_bot import build_timur_application
    from src.telegram_bots.rita_bot import build_rita_application
    from src.telegram_bots.alina_bot import build_alina_application

    agent_apps = []
    for name, builder in [
        ("Боба", build_boba_application),
        ("Кирилл", build_kirill_application),
        ("Тимур", build_timur_application),
        ("Рита", build_rita_application),
        ("Алина", build_alina_application),
    ]:
        try:
            app = builder()
            if app is not None:
                await app.initialize()
                await app.start()
                if app.updater:
                    await app.updater.start_polling()
                agent_apps.append((name, app))
                logger.info(f"Агент-бот '{name}' запущен")
        except Exception as e:
            logger.exception(f"Не смог запустить агент-бота {name}: {e}")

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
        if agent_apps:
            names = ", ".join(name for name, _ in agent_apps)
            startup_msg += f"\n\n👥 Команда подключена: {names}"
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
        for name, app in agent_apps:
            try:
                if app.updater:
                    await app.updater.stop()
                await app.stop()
                await app.shutdown()
            except Exception as e:
                logger.warning(f"Ошибка при остановке {name}: {e}")
        logger.info("Приложение остановлено")


if __name__ == "__main__":
    asyncio.run(main())
