"""Обработчики команд и сообщений от пользователя.

Phase 3.5 — генерация креативов через Claude:
1. Фото с подписью (тема) → Claude генерирует 4 варианта
2. Бот показывает все 4 в одном сообщении + кнопки [1][2][3][4][🔄][❌]
3. Пользователь тапает номер → превью с выбранным + [✅ Создать][❌ Отмена]
4. Создать → AdCreator делает кампанию с возрастным A/B сплитом
"""

import logging
from io import BytesIO

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

from src.claude_brain.copywriter import (
    ClaudeCopywriter,
    CopywriterError,
    fallback_copy_from_caption,
)
from src.config import settings
from src.services.ad_creator import (
    DEFAULT_AGE_SPLITS_ORTHODOX,
    AdCopy,
    AdCreator,
    AdCreatorError,
)
from src.vk_ads.auth import VKAdsAuthError
from src.vk_ads.client import VKAdsAPIError, VKAdsClient

logger = logging.getLogger(__name__)


# Ключи в context.user_data:
# pending_photo: bytes картинки + caption (theme), пока не выбран вариант
# pending_variants: список AdCopy после генерации Claude
# pending_campaign: финальный выбор {image_bytes, caption, copy} перед созданием
_PHOTO_KEY = "pending_photo"
_VARIANTS_KEY = "pending_variants"
_CAMPAIGN_KEY = "pending_campaign"

# Префиксы callback_data
_CB_VARIANT = "variant:"  # variant:0, variant:1, ...
_CB_REGEN = "variant:regen"
_CB_CONFIRM = "campaign:confirm"
_CB_CANCEL = "campaign:cancel"


# ============================================================================
# Простые команды
# ============================================================================


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 Я — твой VK-маркетолог.\n\n"
        "Что умею:\n"
        "• Принимать фото + тему → Claude генерит 4 варианта текста, выбираешь лучший\n"
        "• Запускать тестовую кампанию с возрастным A/B сплитом\n"
        "• Мониторить и автоотключать слабые объявления (Phase 4)\n\n"
        "Команды: /help, /status"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📋 Команды:\n\n"
        "/start — приветствие\n"
        "/help — это сообщение\n"
        "/status — реальный статус кабинета VK\n\n"
        "📸 Прислать фото с подписью:\n"
        "1. Подпись = тема рекламы (например: «молитвы за здравие в монастыре»)\n"
        "2. Я сгенерирую через Claude 4 разных варианта заголовка+текста\n"
        "3. Ты выбираешь лучший\n"
        "4. Создаю тестовую кампанию: 5 возрастных групп × 200 ₽/день = 1000 ₽/день, 7 дней"
    )


async def clean_tokens_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Удаляет ВСЕ активные токены VK для нашего client_id.

    Использовать только в одной ситуации: упёрлись в лимит токенов
    (token_limit_exceeded) и нужно почистить старые накопившиеся.
    После очистки бот при следующем /status автоматически запросит
    свежий permanent токен и закэширует в БД.
    """
    if not settings.vk_configured:
        await update.message.reply_text("⚠️ VK Ads OAuth не настроен.")
        return

    placeholder = await update.message.reply_text(
        "⏳ Удаляю все активные токены VK для этого client_id..."
    )

    from src.vk_ads.auth import VKAdsAuthenticator

    auth = VKAdsAuthenticator(
        client_id=settings.vk_ads_oauth_client_id,  # type: ignore[arg-type]
        client_secret=settings.vk_ads_oauth_client_secret,  # type: ignore[arg-type]
    )

    try:
        result = await auth.delete_all_remote_tokens()
    except VKAdsAuthError as e:
        await placeholder.edit_text(
            f"❌ Не удалось удалить токены:\n\n`{str(e)[:500]}`",
            parse_mode="Markdown",
        )
        return
    except Exception as e:
        logger.exception("Неожиданная ошибка при удалении токенов")
        await placeholder.edit_text(f"❌ Неожиданная ошибка: {e}")
        return

    await placeholder.edit_text(
        "✅ *Все токены удалены.*\n\n"
        f"Ответ VK: `{str(result)[:200]}`\n\n"
        "Теперь напиши /status — бот автоматически запросит свежий "
        "permanent токен и сохранит его в БД навсегда. "
        "Больше в лимит токенов мы не упрёмся.",
        parse_mode="Markdown",
    )


async def inspect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Диагностика: сырая структура кампании из VK Ads API.

    /inspect            — список последних кампаний (id + name + status)
    /inspect <ad_plan_id> — полная структура: кампания + её группы + баннеры

    Используется чтобы сравнить кампанию, созданную вручную через UI кабинета,
    с тем что собирает наш AdCreator. Помогает понять реальную структуру
    payload, которую VK ожидает.
    """
    import json

    if not settings.vk_configured:
        await update.message.reply_text(
            "⚠️ VK Ads клиент не настроен.\n\n"
            "Нужно задать в env vars: VK_ADS_OAUTH_CLIENT_ID и VK_ADS_OAUTH_CLIENT_SECRET"
        )
        return

    client = VKAdsClient.from_settings()
    if client is None:
        await update.message.reply_text("⚠️ Не удалось создать VK Ads клиент")
        return

    placeholder = await update.message.reply_text("⏳ Запрашиваю данные из VK Ads API...")

    try:
        args = context.args or []
        result: dict = {}

        if args:
            ad_plan_id = args[0].strip()
            # 1) Сама кампания с расширенным набором полей
            plan = await client.get_ad_plan_raw(ad_plan_id)
            result["ad_plan"] = plan

            # 2) Группы этой кампании (со всеми полями включая patterns/package_id)
            try:
                groups = await client.get_ad_groups(ad_plan_id=int(ad_plan_id))
            except ValueError:
                groups = []
            result["ad_groups"] = groups

            # 3) Баннеры для каждой группы — отдельным запросом с patterns
            banners_per_group = []
            for g in groups:
                gid = g.get("id")
                if gid is None:
                    continue
                bs = await client.get_banners_raw(ad_group_id=int(gid))
                banners_per_group.append({"ad_group_id": gid, "banners": bs})
            result["banners"] = banners_per_group
        else:
            # Без аргумента — список последних кампаний для выбора
            campaigns = await client.get_campaigns(limit=20)
            result["recent_campaigns"] = [
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "status": c.get("status"),
                    "objective": c.get("objective"),
                    "date_start": c.get("date_start"),
                }
                for c in campaigns
            ]
            result["_hint"] = (
                "Вызови /inspect <id> с ID нужной кампании для полной структуры"
            )

        payload = json.dumps(result, ensure_ascii=False, indent=2, default=str)

        await placeholder.delete()
        filename = f"inspect_{args[0] if args else 'list'}.json"
        await update.message.reply_document(
            document=BytesIO(payload.encode("utf-8")),
            filename=filename,
            caption=(
                f"🔍 Сырая структура VK Ads API\n"
                f"Размер: {len(payload)} символов"
            ),
        )
    except Exception as e:
        logger.exception("inspect_command failed")
        await placeholder.edit_text(f"❌ Ошибка: {type(e).__name__}: {e}")
    finally:
        await client.close()


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Реальный статус кабинета через VK Ads API."""
    if not settings.vk_configured:
        await update.message.reply_text(
            "⚠️ VK Ads клиент не настроен.\n\n"
            "Нужно задать в env vars: VK_ADS_OAUTH_CLIENT_ID и VK_ADS_OAUTH_CLIENT_SECRET"
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
# Photo flow: фото с темой → Claude генерит варианты → выбор → подтверждение → создание
# ============================================================================


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Получили фото — скачиваем, генерим варианты текста, показываем для выбора."""
    if not settings.vk_configured:
        await update.message.reply_text(
            "⚠️ Сначала настрой VK Ads OAuth в Railway env vars."
        )
        return

    if settings.vk_community_url is None and settings.vk_community_url_id is None:
        await update.message.reply_text(
            "⚠️ Не задано сообщество для рекламы.\n\n"
            "В Railway env vars нужно задать ОДНО из:\n"
            "• `VK_COMMUNITY_URL` = `https://vk.com/pomolimsy` (приоритет)\n"
            "• `VK_COMMUNITY_URL_ID` = `216409501` (fallback)",
            parse_mode="Markdown",
        )
        return

    photo = update.message.photo[-1]
    caption = (update.message.caption or "").strip()

    if not caption:
        await update.message.reply_text(
            "📝 К фото нужна подпись с темой рекламы.\n\n"
            "По теме Claude сгенерирует 4 разных варианта заголовка+текста, "
            "ты выберешь лучший."
        )
        return

    logger.info(
        f"Фото от owner: file_id={photo.file_id}, "
        f"size={photo.width}x{photo.height}, caption='{caption[:60]}'"
    )

    # Скачиваем фото в память
    bot_file = await context.bot.get_file(photo.file_id)
    image_buf = BytesIO()
    await bot_file.download_to_memory(image_buf)
    image_bytes = image_buf.getvalue()

    context.user_data[_PHOTO_KEY] = {
        "image_bytes": image_bytes,
        "caption": caption,
        "filename": "photo.jpg",
    }
    # Старые pending очищаем
    context.user_data.pop(_VARIANTS_KEY, None)
    context.user_data.pop(_CAMPAIGN_KEY, None)

    placeholder = await update.message.reply_text(
        "🤖 Получил фото, генерирую 4 варианта текста через Claude..."
    )

    await _generate_and_show_variants(placeholder, context, caption)


async def _generate_and_show_variants(
    message_to_edit, context: ContextTypes.DEFAULT_TYPE, theme: str
) -> None:
    """Дёргаем Claude → сохраняем варианты → показываем кнопки выбора."""
    try:
        copywriter = ClaudeCopywriter()
        variants = await copywriter.generate_copy_variants(theme=theme, n=4)
    except CopywriterError as e:
        logger.error(f"Claude не сгенерил варианты: {e}")
        # Fallback: используем caption напрямую как один вариант
        variants = [fallback_copy_from_caption(theme)]
        await message_to_edit.edit_text(
            f"⚠️ Claude недоступен ({str(e)[:100]}). Использую подпись как текст напрямую."
        )
        # Сразу переходим к подтверждению с этим единственным вариантом
        context.user_data[_CAMPAIGN_KEY] = {
            **context.user_data[_PHOTO_KEY],
            "copy": variants[0],
        }
        await _show_campaign_preview(message_to_edit, context, variants[0], len(variants))
        return

    # Сохраняем варианты для callback'а
    context.user_data[_VARIANTS_KEY] = variants

    # Собираем сообщение со всеми вариантами
    lines = [f"🎨 *Сгенерировал {len(variants)} вариантов:*\n"]
    for i, v in enumerate(variants, 1):
        lines.append(f"*━━━ Вариант {i} ━━━*")
        lines.append(f"*{v.title}*")
        # Текст обрезаем чтобы влезло в один Telegram-message (4096 chars total)
        text_preview = v.text[:300] + ("…" if len(v.text) > 300 else "")
        lines.append(text_preview)
        lines.append(f"_{v.about}_")
        lines.append("")

    # Кнопки выбора
    n = len(variants)
    rows: list[list[InlineKeyboardButton]] = []
    # Первый ряд — номера вариантов
    rows.append([
        InlineKeyboardButton(str(i + 1), callback_data=f"{_CB_VARIANT}{i}")
        for i in range(n)
    ])
    rows.append([
        InlineKeyboardButton("🔄 Перегенерить", callback_data=_CB_REGEN),
        InlineKeyboardButton("❌ Отмена", callback_data=_CB_CANCEL),
    ])

    full_text = "\n".join(lines)
    # Ограничение Telegram — 4096 символов на сообщение
    if len(full_text) > 4000:
        full_text = full_text[:3990] + "\n\n_(обрезано)_"

    await message_to_edit.edit_text(
        full_text,
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown",
    )


async def _show_campaign_preview(
    message_to_edit,
    context: ContextTypes.DEFAULT_TYPE,
    chosen: AdCopy,
    variant_num: int,
) -> None:
    """Показывает финальное превью с выбранным вариантом + кнопки [Создать] [Отмена]."""
    photo = context.user_data.get(_PHOTO_KEY)
    if not photo:
        await message_to_edit.edit_text("⚠️ Данные о фото потерялись, пришли заново.")
        return

    splits = DEFAULT_AGE_SPLITS_ORTHODOX
    daily_per_group = settings.test_campaign_budget_rub
    daily_total = daily_per_group * len(splits)

    text_preview = chosen.text[:400] + ("…" if len(chosen.text) > 400 else "")

    # Куда вести (URL приоритет над ID)
    target_display = (
        settings.vk_community_url
        or f"vk.com/club{settings.vk_community_url_id}"
    )

    preview = (
        f"📋 *Выбран вариант:*\n\n"
        f"*Заголовок:* {chosen.title}\n"
        f"*Текст:* {text_preview}\n"
        f"*О проекте:* {chosen.about}\n"
        f"*CTA:* {chosen.cta}\n\n"
        f"*A/B сплит по возрасту:*\n"
        + "\n".join(f"  • {a}-{b}" for a, b in splits)
        + f"\n\n*Бюджет:* {daily_per_group} ₽/день × {len(splits)} групп = "
        f"*{daily_total} ₽/день*\n"
        f"*Длительность:* 7 дней\n"
        f"*Куда:* `{target_display}`\n\n"
        "Создавать?"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Создать", callback_data=_CB_CONFIRM),
            InlineKeyboardButton("❌ Отмена", callback_data=_CB_CANCEL),
        ]
    ])

    await message_to_edit.edit_text(
        preview, reply_markup=keyboard, parse_mode="Markdown"
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Роутинг inline-кнопок."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data or ""

    if data == _CB_CANCEL:
        context.user_data.pop(_PHOTO_KEY, None)
        context.user_data.pop(_VARIANTS_KEY, None)
        context.user_data.pop(_CAMPAIGN_KEY, None)
        await query.edit_message_text("❌ Отменено.")
        return

    if data == _CB_REGEN:
        photo = context.user_data.get(_PHOTO_KEY)
        if not photo:
            await query.edit_message_text("⚠️ Данные о фото потерялись, пришли заново.")
            return
        await query.edit_message_text("🔄 Перегенерирую варианты...")
        await _generate_and_show_variants(query.message, context, photo["caption"])
        return

    if data.startswith(_CB_VARIANT):
        # variant:N — выбор варианта по номеру
        try:
            idx = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            logger.warning(f"Невалидный variant callback: {data}")
            return

        variants = context.user_data.get(_VARIANTS_KEY, [])
        if idx < 0 or idx >= len(variants):
            await query.edit_message_text("⚠️ Этот вариант больше недоступен.")
            return

        chosen: AdCopy = variants[idx]
        photo = context.user_data.get(_PHOTO_KEY)
        if not photo:
            await query.edit_message_text("⚠️ Данные о фото потерялись.")
            return

        context.user_data[_CAMPAIGN_KEY] = {**photo, "copy": chosen}
        await _show_campaign_preview(query.message, context, chosen, idx + 1)
        return

    if data == _CB_CONFIRM:
        await _create_campaign_from_pending(update, context)
        return

    logger.warning(f"Неизвестный callback_data: {data}")


async def _create_campaign_from_pending(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    pending = context.user_data.get(_CAMPAIGN_KEY)

    if not pending:
        await query.edit_message_text(
            "⚠️ Данные потерялись (бот мог перезапуститься). Пришли фото заново."
        )
        return

    await query.edit_message_text("⏳ Создаю кампанию в VK Рекламе...")

    client = VKAdsClient.from_settings()
    if client is None:
        await query.edit_message_text("⚠️ VK Ads клиент не настроен.")
        return

    creator = AdCreator(client)
    copy: AdCopy = pending["copy"]

    # Используем явный URL если он задан, иначе строим из ID
    community_url = (
        settings.vk_community_url
        or f"https://vk.com/club{settings.vk_community_url_id}"
    )

    try:
        # Если настроена template-кампания — workaround-флоу.
        # Бот создаёт новые ad_groups (с banner внутри) в существующую
        # template-кампанию через POST /ad_groups.json. Старая кампания
        # уже инициализировала patterns settings в package через UI-flow.
        # Поддержка VK (May 2026) подтвердила: banner создаётся только как
        # nested внутри ad_group, отдельный POST /banners.json не работает.
        if settings.has_template_campaign:
            summary = await creator.create_age_split_groups_in_template_plan(
                image_bytes=pending["image_bytes"],
                copy=copy,
                community_url=community_url,
                template_ad_plan_id=settings.vk_template_ad_plan_id,
                age_splits=DEFAULT_AGE_SPLITS_ORTHODOX,
                daily_budget_rub_per_group=settings.test_campaign_budget_rub,
                banner_name_prefix="bot-test",
                image_filename=pending["filename"],
            )
        else:
            summary = await creator.create_age_split_campaign(
                image_bytes=pending["image_bytes"],
                theme=copy.title,
                copy=copy,
                community_url=community_url,
                age_splits=DEFAULT_AGE_SPLITS_ORTHODOX,
                daily_budget_rub_per_group=settings.test_campaign_budget_rub,
                campaign_name_prefix="bot-test",
                image_filename=pending["filename"],
            )
    except AdCreatorError as e:
        logger.error(f"AdCreator упал: {e}")
        # Если ошибка пришла из VK API — добавляем diag-данные для тикета
        # в поддержку VK (x-request-id + точное время).
        msg = f"❌ Не удалось создать кампанию:\n\n`{str(e)[:400]}`"
        if e.vk_error is not None:
            msg += f"\n\n🔧 *Для тикета в VK:*\n`{e.vk_error.diag_summary()}`"
        await query.edit_message_text(msg, parse_mode="Markdown")
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
        context.user_data.pop(_PHOTO_KEY, None)
        context.user_data.pop(_VARIANTS_KEY, None)
        context.user_data.pop(_CAMPAIGN_KEY, None)

    msg_lines = [
        "✅ *Кампания создана!*",
        "",
        f"Кампания: `{summary.ad_plan_id}`",
        f"Групп: {len(summary.ad_group_ids)}",
        f"Объявлений: {len(summary.banner_ids)}",
        "",
        "Через 1-2 часа после модерации VK начнут идти показы.",
    ]
    await query.edit_message_text("\n".join(msg_lines), parse_mode="Markdown")


# ============================================================================
# Текст — заглушка
# ============================================================================


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    logger.info(f"Получен текст: '{text[:100]}'")
    await update.message.reply_text(
        "📝 Принято. Сейчас работают:\n"
        "• /status — статус кабинета\n"
        "• Фото с подписью → 4 варианта от Claude → выбор → создание кампании"
    )
