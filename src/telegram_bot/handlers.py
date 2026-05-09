"""Обработчики команд и сообщений от пользователя."""

import logging

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка /start."""
    await update.message.reply_text(
        "🤖 Я — твой VK-маркетолог.\n\n"
        "Что я умею (когда настрою VK Ads клиент):\n"
        "• Принимать картинки + темы для рекламы\n"
        "• Запускать A/B тесты\n"
        "• Мониторить и отключать слабые\n"
        "• Масштабировать удачные\n"
        "• Каждое утро присылать отчёт\n\n"
        "Сейчас MVP в стадии настройки. Напиши /help чтобы увидеть текущий список команд."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка /help."""
    await update.message.reply_text(
        "📋 Команды:\n\n"
        "/start — приветствие\n"
        "/help — это сообщение\n"
        "/status — статус кампаний\n\n"
        "Также можешь:\n"
        "• Прислать фото с подписью — создам тестовые объявления\n"
        "• Написать текстом тему — добавлю в очередь\n\n"
        "⚠️ Пока в разработке. См. CLAUDE.md в репо для прогресса."
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка /status — показ статуса кампаний."""
    # TODO: Подключить VK Ads клиент и получить реальный статус
    await update.message.reply_text(
        "📊 Статус кампаний\n\n"
        "VK Ads клиент ещё не подключён. Когда подключу:\n"
        "• Активных кампаний: N\n"
        "• Расход за сегодня: N₽\n"
        "• Заявок: N\n"
        "• Лучшее объявление: <название>\n\n"
        "Пока заглушка."
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка присланной фотографии (для будущей рекламы)."""
    photo = update.message.photo[-1]  # самая большая версия
    caption = update.message.caption or ""

    logger.info(
        f"Получено фото от owner: file_id={photo.file_id}, "
        f"size={photo.width}x{photo.height}, caption='{caption[:50]}...'"
    )

    # TODO: Сохранить фото локально, передать в VK Ads upload
    await update.message.reply_text(
        f"✅ Получил фото ({photo.width}×{photo.height}).\n"
        f"Подпись: «{caption[:100]}»\n\n"
        "TODO: загрузить в VK и создать тестовые объявления."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка обычного текстового сообщения."""
    text = update.message.text
    logger.info(f"Получен текст: '{text[:100]}...'")

    # TODO: Парсить как команду на естественном языке через Claude
    await update.message.reply_text(
        f"📝 Принял: «{text[:200]}»\n\n"
        "TODO: распознать команду через Claude и выполнить."
    )
