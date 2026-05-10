"""Обработчики команд и сообщений от пользователя.

Phase 3 — полноценный flow создания тестовой кампании по фото:
1. /start, /help — служебные
2. /status — реальные данные кабинета через VK Ads API
3. handle_photo — приёмка фото с подписью + inline-подтверждение
4. on_callback — обработка кнопок [Создать] / [Отмена]
5. handle_text — заглушка под Phase 4 (текстовые команды через Claude)
"""

import logging
from io import BytesIO

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

from src.config import settings
from src.services.ad_creator import (
    DEFAULT_AGE_SPLITS_ORTHODOX,
    AdCreator,
    AdCreatorError,
)
from src.claude_brain.copywriter import fallback_copy_from_caption
from src.vk_ads.auth import VKAdsAuthError
from src.vk_ads.client import VKAdsAPIError, VKAdsClient

logger = logging.getLogger(__name__)


# Ключи для context.user_data — храним фото между фото-сообщением и нажатием кнопки
_PENDING_KEY = "pending_campaign"

# Префикс callback_data для inline-кнопок
_CB_CONFIRM = "campaign:confirm"
_CB_CANCEL = "campaign:cancel"


# ============================================================================
# Простые команды
# ============================================================================


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 Я — твой VK-маркетолог.\n\n"
        "Что умею:\n"
        "• Принимать фото с подписью → создать тестовую кампанию с возрастным A/B сплитом\n"
        "• Мониторить и автоотключать слабые объявления\n"
        "• Масштабировать удачные\n"
        "• Каждое утро присылать отчёт\n\n"
        "Команды: /help, /status"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📋 Команды:\n\n"
        "/start — приветствие\n"
        "/help — это сообщение\n"
        "/status — реальный статус кабинета VK\n\n"
        "📸 Прислать фото с подписью — бот предложит создать тестовую кампанию:\n"
        "5 возрастных групп (41-42, 43-44, 45-46, 47-48, 49-50)\n"
        "бюджет 200 ₽/день на группу = 1000 ₽/день общий\n"
        "длительность 7 дней\n\n"
        "Перед созданием бот спросит подтверждение."
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Реальный статус кабинета через VK Ads API."""
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
            lines.append(
                "💰 Баланс: смотри в кабинете VK\n"
                "_(API нового кабинета не отдаёт баланс)_"
            )

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
        logger.exception("Неожиданная ошибка в /status")
        await placeholder.edit_text(f"❌ Неожиданная ошибка: {e}")


# ============================================================================
# Photo flow: фото с подписью → подтверждение → создание кампании
# ============================================================================


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Получили фото — скачиваем, парсим подпись, спрашиваем подтверждение."""
    if not settings.vk_configured:
        await update.message.reply_text(
            "⚠️ Сначала настрой VK Ads OAuth в Railway env vars."
        )
        return

    if settings.vk_community_url_id is None:
        await update.message.reply_text(
            "⚠️ Не задан `VK_COMMUNITY_URL_ID` в env vars Railway.\n\n"
            "Это VK ID сообщества, на которое будет вестись реклама. "
            "Без него я не знаю, куда направлять трафик. Задай и попробуй ещё раз.",
            parse_mode="Markdown",
        )
        return

    photo = update.message.photo[-1]  # самая большая версия
    caption = (update.message.caption or "").strip()

    if not caption:
        await update.message.reply_text(
            "📝 К фото нужна подпись с темой рекламы.\n\n"
            "Первая строка станет заголовком (макс 40 символов), "
            "остальное — основным текстом объявления."
        )
        return

    # Скачиваем фото в память
    logger.info(
        f"Получено фото: file_id={photo.file_id}, "
        f"size={photo.width}x{photo.height}, caption='{caption[:60]}'"
    )

    bot_file = await context.bot.get_file(photo.file_id)
    image_buf = BytesIO()
    await bot_file.download_to_memory(image_buf)
    image_bytes = image_buf.getvalue()

    # Сохраняем для callback'а
    context.user_data[_PENDING_KEY] = {
        "image_bytes": image_bytes,
        "caption": caption,
        "filename": "photo.jpg",
        "size_wh": (photo.width, photo.height),
    }

    # Прикидываем что бот сделает
    copy = fallback_copy_from_caption(caption)
    splits = DEFAULT_AGE_SPLITS_ORTHODOX
    daily_per_group = settings.test_campaign_budget_rub
    daily_total = daily_per_group * len(splits)

    preview = (
        f"📸 Получил фото {photo.width}×{photo.height} "
        f"({len(image_bytes) // 1024} КБ)\n\n"
        f"*Что будет в объявлении:*\n"
        f"Заголовок: {copy.title}\n"
        f"Текст: {copy.text[:200]}{'…' if len(copy.text) > 200 else ''}\n\n"
        f"*A/B сплит по возрасту:*\n"
        + "\n".join(f"  • {a}-{b}" for a, b in splits)
        + f"\n\n*Бюджет:* {daily_per_group} ₽/день × {len(splits)} групп = "
        f"*{daily_total} ₽/день*\n"
        f"*Длительность:* 7 дней\n"
        f"*Куда:* VK сообщество `{settings.vk_community_url_id}`\n\n"
        "Создавать?"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Создать", callback_data=_CB_CONFIRM),
            InlineKeyboardButton("❌ Отмена", callback_data=_CB_CANCEL),
        ]
    ])

    await update.message.reply_text(
        preview, reply_markup=keyboard, parse_mode="Markdown"
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка нажатий inline-кнопок."""
    query = update.callback_query
    if not query:
        return
    await query.answer()  # убираем «часики» в Telegram

    data = query.data or ""

    if data == _CB_CANCEL:
        context.user_data.pop(_PENDING_KEY, None)
        await query.edit_message_text("❌ Отменено.")
        return

    if data == _CB_CONFIRM:
        await _create_campaign_from_pending(update, context)
        return

    logger.warning(f"Неизвестный callback_data: {data}")


async def _create_campaign_from_pending(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Достаёт pending фото и создаёт кампанию."""
    query = update.callback_query
    pending = context.user_data.get(_PENDING_KEY)

    if not pending:
        await query.edit_message_text(
            "⚠️ Данные о фото потерялись (вероятно, бот перезапускался). "
            "Пришли фото ещё раз."
        )
        return

    await query.edit_message_text("⏳ Создаю кампанию в VK Рекламе...")

    client = VKAdsClient.from_settings()
    if client is None:
        await query.edit_message_text("⚠️ VK Ads клиент не настроен.")
        return

    creator = AdCreator(client)
    copy = fallback_copy_from_caption(pending["caption"])

    try:
        summary = await creator.create_age_split_campaign(
            image_bytes=pending["image_bytes"],
            theme=copy.title,
            copy=copy,
            community_url_id=settings.vk_community_url_id,  # type: ignore[arg-type]
            age_splits=DEFAULT_AGE_SPLITS_ORTHODOX,
            daily_budget_rub_per_group=settings.test_campaign_budget_rub,
            campaign_name_prefix="bot-test",
            image_filename=pending["filename"],
        )
    except AdCreatorError as e:
        logger.error(f"AdCreator упал: {e}")
        await query.edit_message_text(
            f"❌ Не удалось создать кампанию:\n\n`{str(e)[:500]}`",
            parse_mode="Markdown",
        )
        return
    except VKAdsAuthError as e:
        await query.edit_message_text(
            f"❌ Авторизация VK слетела:\n\n`{str(e)[:300]}`",
            parse_mode="Markdown",
        )
        return
    except Exception as e:
        logger.exception("Неожиданная ошибка при создании кампании")
        await query.edit_message_text(f"❌ Неожиданная ошибка: {e}")
        return
    finally:
        context.user_data.pop(_PENDING_KEY, None)

    # Успех
    msg_lines = [
        "✅ *Кампания создана!*",
        "",
        f"Кампания: `{summary.ad_plan_id}`",
        f"Групп: {len(summary.ad_group_ids)}",
        f"Объявлений: {len(summary.banner_ids)}",
        "",
        "Через 1-2 часа после модерации VK начнут идти показы. "
        "Утром пришлю первый отчёт.",
    ]
    await query.edit_message_text("\n".join(msg_lines), parse_mode="Markdown")


# ============================================================================
# Текст — заглушка
# ============================================================================


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Текстовое сообщение (без команды)."""
    text = (update.message.text or "").strip()
    logger.info(f"Получен текст: '{text[:100]}'")
    await update.message.reply_text(
        "📝 Принято. Текстовые команды через Claude — Phase 4.\n\n"
        "Сейчас работают:\n"
        "• /status — статус кабинета\n"
        "• Фото с подписью — создать тестовую кампанию"
    )
