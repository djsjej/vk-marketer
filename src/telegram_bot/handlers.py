"""Обработчики команд и сообщений от пользователя."""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from src.config import settings
from src.vk_ads.auth import VKAdsAuthError
from src.vk_ads.client import VKAdsAPIError, VKAdsClient

logger = logging.getLogger(__name__)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка /start."""
    await update.message.reply_text(
        "🤖 Я — твой VK-маркетолог.\n\n"
        "Что умею:\n"
        "• Принимать картинки + темы для рекламы\n"
        "• Запускать A/B тесты\n"
        "• Мониторить и отключать слабые\n"
        "• Масштабировать удачные\n"
        "• Каждое утро присылать отчёт\n\n"
        "Команды: /help, /status"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка /help."""
    await update.message.reply_text(
        "📋 Команды:\n\n"
        "/start — приветствие\n"
        "/help — это сообщение\n"
        "/status — реальный статус кабинета VK\n\n"
        "Также можешь:\n"
        "• Прислать фото с подписью — создам тестовые объявления (Phase 3)\n"
        "• Написать текстом тему — добавлю в очередь (Phase 3)"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка /status — реальный статус кабинета через VK Ads API."""
    if not settings.vk_configured:
        await update.message.reply_text(
            "⚠️ VK Ads клиент не настроен.\n\n"
            "Нужно задать в env vars: VK_ADS_OAUTH_CLIENT_ID и VK_ADS_OAUTH_CLIENT_SECRET\n"
            "(или VK_ADS_TOKEN если есть готовый access_token)"
        )
        return

    client = VKAdsClient.from_settings()
    if client is None:
        await update.message.reply_text("⚠️ Не удалось создать VK Ads клиент")
        return

    # Сначала покажем «думаю», чтобы пользователь видел отклик
    placeholder = await update.message.reply_text("⏳ Запрашиваю статус кабинета...")

    try:
        balance = await client.get_balance()
        campaigns = await client.get_campaigns(limit=20)

        active = [c for c in campaigns if c.get("status") == "active"]
        blocked = [c for c in campaigns if c.get("status") == "blocked"]

        lines = ["📊 *Статус кабинета VK Рекламы*\n"]

        if balance is not None:
            lines.append(f"💰 Баланс: *{balance:.0f} ₽*")
        else:
            lines.append("💰 Баланс: не удалось получить")

        lines.append(f"🆔 Кабинет: `{settings.vk_ads_account_id}`")
        lines.append("")

        if not campaigns:
            lines.append("Кампаний пока нет.")
        else:
            lines.append(f"📢 Кампаний всего: *{len(campaigns)}*")
            lines.append(f"   • Активных: {len(active)}")
            lines.append(f"   • Заблокированных: {len(blocked)}")

            if active:
                lines.append("\nАктивные:")
                for c in active[:5]:
                    name = c.get("name", "?")[:40]
                    daily = c.get("budget_limit_day", "—")
                    lines.append(f"  • {name} (дневной: {daily})")
                if len(active) > 5:
                    lines.append(f"  ... и ещё {len(active) - 5}")

        await placeholder.edit_text("\n".join(lines), parse_mode="Markdown")

    except VKAdsAuthError as e:
        logger.error(f"OAuth error в /status: {e}")
        await placeholder.edit_text(
            f"❌ Ошибка авторизации в VK:\n\n`{str(e)[:300]}`\n\n"
            "Проверь VK_ADS_OAUTH_CLIENT_ID и VK_ADS_OAUTH_CLIENT_SECRET в Railway.",
            parse_mode="Markdown",
        )
    except VKAdsAPIError as e:
        logger.error(f"API error в /status: {e}")
        await placeholder.edit_text(
            f"❌ Ошибка VK Ads API:\n\n`{str(e)[:400]}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception(f"Неожиданная ошибка в /status")
        await placeholder.edit_text(f"❌ Неожиданная ошибка: {e}")


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
