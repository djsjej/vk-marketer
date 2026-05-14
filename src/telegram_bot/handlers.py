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
    """Приветствие + сразу показываем меню с кнопками.

    На iPhone это первое что видит Vizit — поэтому удобнее всего сразу
    отдать ему меню, а не текстовое описание.
    """
    from telegram import KeyboardButton, ReplyKeyboardMarkup

    keyboard = [
        [
            KeyboardButton("📊 Статус кабинета"),
            KeyboardButton("🔧 Проверить VK API"),
        ],
        [
            KeyboardButton("☦️ Православные сообщества"),
            KeyboardButton("👥 Парсить pomolimsy"),
        ],
        [
            KeyboardButton("🔍 Поиск: православие"),
            KeyboardButton("🔍 Поиск: монастырь"),
        ],
        [
            KeyboardButton("👁 Биба-разведчик"),
            KeyboardButton("📋 Все команды"),
        ],
    ]
    markup = ReplyKeyboardMarkup(
        keyboard, resize_keyboard=True, is_persistent=True
    )

    await update.message.reply_text(
        "🤖 *Я — твой VK-маркетолог.*\n\n"
        "Жми кнопки внизу — они сразу запускают действие. "
        "Если нужен другой ключ поиска или сообщество — вводи команду "
        "вручную через `/vk_search` или `/vk_parse`.",
        parse_mode="Markdown",
        reply_markup=markup,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Список команд и подсказка как пользоваться photo-flow.

    Восстановлено из коммита 6248da3. В коммите 32d534a (`feat(biba)`) я
    случайно затёр `async def help_command(...):` строку — само тело
    функции оказалось приклеено в конец `biba_command`, а импорт
    `help_command` в `bot.py` остался — worker крашился на старте с
    ImportError. Регрессионный тест см. в `tests/test_handlers_imports.py`.
    """
    await update.message.reply_text(
        "📋 Команды:\n\n"
        "/menu — главное меню с кнопками 🎛\n"
        "/start — приветствие\n"
        "/help — это сообщение\n"
        "/status — реальный статус кабинета VK\n"
        "/biba — карта функционала кабинета VK Ads (REPORT.md + raw JSON в zip)\n"
        "/vk_check [screen_name] — проверка VK API service token\n"
        "/vk_search <ключ> — поиск VK-сообществ по теме (Phase 5)\n"
        "/vk_parse <screen_name> [N] — парсинг подписчиков сообщества\n\n"
        "📸 Прислать фото с подписью:\n"
        "1. Подпись = тема рекламы (например: «молитвы за здравие в монастыре»)\n"
        "2. Я сгенерирую через Claude 4 разных варианта заголовка+текста\n"
        "3. Ты выбираешь лучший\n"
        "4. Создаю тестовую кампанию: 5 возрастных групп × 200 ₽/день = 1000 ₽/день, 7 дней"
    )


async def biba_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запуск Бибы — разведчика VK Ads API.

    Биба пройдёт по 36 справочным endpoints VK Ads и соберёт inventory:
    интересы, регионы, существующие сегменты, пиксели, и т.д.
    Результат — два markdown файла:
    - REPORT.md — что откликнулось и что в нём
    - PROPOSALS.md — гипотезы Бибы что ещё стоит проверить

    Прогон занимает ~1-2 минуты. Прогресс шлётся в чат, файлы в конце.
    """
    from src.biba.explorer import explore
    from src.vk_ads.auth import VKAdsAuthenticator

    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "👁 *БИБА выходит на разведку*\n\n"
        "Опрашиваю 36 справочных endpoints VK Ads. "
        "Сразу не лезу в твои кампании — только справочники.\n\n"
        "Это займёт 1-2 минуты. Жди.",
        parse_mode="Markdown",
    )

    try:
        authenticator = VKAdsAuthenticator(
            client_id=settings.vk_ads_oauth_client_id,
            client_secret=settings.vk_ads_oauth_client_secret,
        )
        client = VKAdsClient(authenticator=authenticator)
        report = await explore(client)

        # Шлём REPORT.md как файл
        from pathlib import Path
        report_path = Path("docs/biba_findings/REPORT.md")
        proposals_path = Path("docs/biba_findings/PROPOSALS.md")

        # Краткая сводка в чат
        await update.message.reply_text(
            f"✅ *Биба вернулся из экспедиции*\n\n"
            f"Endpoints проверено: {len(report.findings)}\n"
            f"Откликнулись (200): {report.ok_count}\n"
            f"Не найдены (404): {report.not_found_count}\n\n"
            f"Шлю детальный рапорт файлом.",
            parse_mode="Markdown",
        )

        # Файл рапорта
        if report_path.exists():
            with open(report_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename="biba_REPORT.md",
                    caption="📄 Полный рапорт Бибы",
                )

        # Файл предложений
        if proposals_path.exists():
            with open(proposals_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename="biba_PROPOSALS.md",
                    caption=(
                        "💡 Что ещё Биба предлагает проверить.\n"
                        "Скажи Claude в чате какие пункты одобряешь — "
                        "он добавит их в следующий прогон."
                    ),
                )

        # ZIP со ВСЕМИ raw JSON-файлами — для разбора Claude'ом в чате.
        # REPORT.md обрезает sample каждого endpoint до 1500 символов
        # (иначе Markdown становится нечитаемым на телефоне), но Claude'у
        # нужны полные данные — особенно targetings_tree.json для интересов.
        try:
            from src.biba.explorer import create_findings_archive
            archive_path = create_findings_archive(Path("docs/biba_findings"))
            with open(archive_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename="biba_findings.zip",
                    caption=(
                        "📦 Полные raw JSON всех endpoints.\n"
                        "Перешли Claude'у в чате — он распакует и достанет "
                        "из targetings_tree.json ID интересов для православной "
                        "ниши (Религия, Православие, Благотворительность)."
                    ),
                )
        except Exception as zip_err:
            logger.exception("Не смог собрать архив biba_findings")
            await update.message.reply_text(
                f"⚠️ Не смог собрать ZIP с raw JSON: `{zip_err}`\n\n"
                f"REPORT.md и PROPOSALS.md ушли — для базовых задач хватит.",
                parse_mode="Markdown",
            )

    except Exception as e:
        logger.exception("Биба упал")
        await update.message.reply_text(
            f"❌ Биба упал в обморок:\n\n`{e}`\n\n"
            f"Скажи Claude в чате — разберёмся.",
            parse_mode="Markdown",
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
                days_duration=settings.test_campaign_days,
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

    # URL на просмотр кампании в кабинете VK — чтобы Vizit мог быстро
    # посмотреть как кампания выглядит и сразу дать фидбэк / отключить.
    cabinet_url = f"https://ads.vk.com/hq/edit/ad_plan/{summary.ad_plan_id}"

    msg_lines = [
        "✅ *Кампания создана!*",
        "",
        f"Кампания: `{summary.ad_plan_id}`",
        f"Групп: {len(summary.ad_group_ids)}",
        f"Объявлений: {len(summary.banner_ids)}",
        "",
        f"🔗 [Посмотреть в кабинете]({cabinet_url})",
        "",
        "Через 1-2 часа после модерации VK начнут идти показы.",
    ]
    await query.edit_message_text(
        "\n".join(msg_lines),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


# ============================================================================
# Текст — заглушка
# ============================================================================


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик текстовых сообщений.

    Сначала проверяет — не нажатие ли это на ReplyKeyboard-кнопку меню.
    Если да — запускает соответствующий handler с правильными аргументами.
    Иначе — обычный ответ-плейсхолдер.
    """
    text = (update.message.text or "").strip()
    logger.info(f"Получен текст: '{text[:100]}'")

    # 1. Проверяем — это кнопка меню?
    if text in MENU_BUTTON_ROUTES:
        action_type, action_value = MENU_BUTTON_ROUTES[text]

        if action_type == "command":
            # Команды без аргументов — вызываем handler напрямую
            command_map = {
                "status": status_command,
                "vk_check": vk_check_command,
                "vk_orthodox": vk_orthodox_command,
                "biba": biba_command,
                "help": help_command,
            }
            handler = command_map.get(action_value)
            if handler:
                context.args = []  # команда без аргументов
                await handler(update, context)
                return
        elif action_type == "vk_search":
            # Кнопка поиска с предустановленным ключом
            context.args = action_value.split()
            await vk_search_command(update, context)
            return
        elif action_type == "vk_parse":
            # Кнопка парсинга с предустановленным аргументами (screen_name + N)
            context.args = action_value.split()
            await vk_parse_command(update, context)
            return

    # 2. Не кнопка — обычное текстовое сообщение
    await update.message.reply_text(
        "📝 Принято. Что можно делать:\n"
        "• `/menu` — меню с кнопками\n"
        "• `/help` — все команды\n"
        "• Фото с подписью → 4 варианта от Claude → создание кампании",
        parse_mode="Markdown",
    )


async def vk_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверка VK API service token — Phase 5 первая итерация.

    Использование:
        /vk_check                — запрашивает инфу о vk.com/pomolimsy
        /vk_check pomolimsy      — то же явно
        /vk_check 216409501      — по числовому ID
        /vk_check orthodox_woman — любое публичное сообщество

    Проверяет что:
    - Service token из VK_API_SERVICE_TOKEN в Railway env работает
    - VK API клиент в src/targetolog/ корректно общается с api.vk.com
    - У токена достаточно прав на метод groups.getById

    Если работает — Phase 5 готов к расширению (groups.getMembers,
    парсинг подписчиков, пересечения, etc.).
    """
    from src.targetolog import VKAPIClient, VKAPIError

    if not settings.vk_api_service_token:
        await update.message.reply_text(
            "⚠️ VK API service token не настроен.\n\n"
            "Положи `VK_API_SERVICE_TOKEN` в Railway → Variables и перезапусти.",
            parse_mode="Markdown",
        )
        return

    # Парсим аргумент — это может быть screen_name, числовой ID, или URL.
    # URL вида https://vk.com/pomolimsy чистим до последнего сегмента.
    args = context.args or []
    raw_target = args[0] if args else "pomolimsy"
    target = raw_target.rstrip("/").split("/")[-1]

    await update.message.reply_text(
        f"🔍 Проверяю VK API — запрашиваю инфо о сообществе `{target}`...",
        parse_mode="Markdown",
    )

    try:
        async with VKAPIClient(service_token=settings.vk_api_service_token) as client:
            group = await client.groups_get_by_id(target)
    except VKAPIError as e:
        await update.message.reply_text(
            f"❌ VK API ошибка {e.code}: {e.message}\n\n"
            f"Возможные причины:\n"
            f"• Сообщество не существует (`{target}`)\n"
            f"• Сообщество закрытое (нужен user-token, не service)\n"
            f"• Токен невалидный или истёк",
        )
        return
    except Exception as e:
        logger.exception("VK API check failed")
        await update.message.reply_text(
            f"❌ Неожиданная ошибка: `{type(e).__name__}: {e}`",
            parse_mode="Markdown",
        )
        return

    # Успех — собираем красивый ответ
    is_closed_map = {0: "открытое", 1: "закрытое", 2: "частное"}
    is_closed_text = is_closed_map.get(group.get("is_closed", 0), "?")

    description = group.get("description") or ""
    # Обрезаем длинное описание для Telegram
    if len(description) > 200:
        description = description[:200] + "..."

    reply = (
        f"✅ *VK API работает*\n\n"
        f"*Сообщество:* {group.get('name', '?')}\n"
        f"*ID:* `{group.get('id', '?')}`\n"
        f"*Screen name:* `{group.get('screen_name', '?')}`\n"
        f"*Подписчиков:* {group.get('members_count', '?'):,}\n".replace(",", " ")
        + f"*Тип:* {is_closed_text}\n"
    )
    if description:
        reply += f"\n_{description}_"

    await update.message.reply_text(reply, parse_mode="Markdown")


async def vk_search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Поиск VK-сообществ по ключевому слову (Phase 5).

    Использование:
        /vk_search православие
        /vk_search молитва за здравие

    Возвращает топ-20 сообществ по релевантности от VK (релевантность
    обычно совпадает с размером — крупные первыми). Для каждого:
    название, screen_name, число подписчиков, краткое описание.
    """
    from src.targetolog import VKAPIClient, VKAPIError

    if not settings.vk_api_service_token:
        await update.message.reply_text(
            "⚠️ VK_API_SERVICE_TOKEN не настроен в Railway.",
        )
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Введи ключевую фразу:\n"
            "`/vk_search православие`\n"
            "`/vk_search молитва за здравие`\n"
            "`/vk_search монастырь`",
            parse_mode="Markdown",
        )
        return

    query = " ".join(args)
    await update.message.reply_text(
        f"🔍 Ищу сообщества по запросу «{query}»...",
    )

    try:
        async with VKAPIClient(service_token=settings.vk_api_service_token) as client:
            groups = await client.groups_search(query, count=30, country=1)
    except VKAPIError as e:
        await update.message.reply_text(
            f"❌ VK API ошибка {e.code}: {e.message}",
        )
        return
    except Exception as e:
        logger.exception("vk_search failed")
        await update.message.reply_text(
            f"❌ Ошибка: `{type(e).__name__}: {e}`",
            parse_mode="Markdown",
        )
        return

    if not groups:
        await update.message.reply_text(
            f"Ничего не нашлось по запросу «{query}». Попробуй другие ключи.",
        )
        return

    found_total = len(groups)

    # 1. Эвристическая фильтрация — отсеиваем явный треш (сатанинское,
    # юмор, чужие конфессии, оккультное). Быстро.
    from src.targetolog.orthodox_filter import (
        claude_filter_orthodox,
        quick_filter_obviously_bad,
    )

    after_quick = [g for g in groups if quick_filter_obviously_bad(g)]

    await update.message.reply_text(
        f"📊 Нашёл {found_total} сообществ. После быстрого фильтра "
        f"осталось {len(after_quick)}.\n\n"
        f"Теперь Claude проверит каждое детально по описанию (несколько секунд)...",
    )

    # 2. Claude-фильтр — каждое сообщество проверяется по названию и описанию.
    # Это исключает околорелигиозный треш, секты, чужие конфессии.
    try:
        orthodox_only = await claude_filter_orthodox(after_quick)
    except Exception as e:
        logger.exception("claude_filter_orthodox failed")
        # Fail-safe — если фильтр сломался, показываем что есть после quick
        orthodox_only = after_quick

    if not orthodox_only:
        await update.message.reply_text(
            f"После фильтрации ничего не осталось — VK по запросу «{query}» "
            f"не вернул реально православных сообществ. Попробуй другой ключ "
            f"(например `молитва`, `иконы`, `святые`, конкретный святой).",
            parse_mode="Markdown",
        )
        return

    # Сортируем итог по числу подписчиков убывание — крупные первые
    orthodox_sorted = sorted(
        orthodox_only, key=lambda g: g.get("members_count", 0), reverse=True
    )

    lines = [
        f"☦️ *Православные сообщества по запросу «{query}»*",
        f"_({found_total} найдено → {len(after_quick)} после quick → "
        f"{len(orthodox_sorted)} после Claude)_\n",
    ]
    for i, g in enumerate(orthodox_sorted, 1):
        name = g.get("name", "?")
        screen = g.get("screen_name", "?")
        members = g.get("members_count", 0)
        members_fmt = f"{members:,}".replace(",", " ")
        reason = g.get("_orthodox_reason", "")
        line = f"{i}. *{name}*\n   `{screen}` — {members_fmt} подп."
        if reason:
            line += f"\n   _{reason}_"
        lines.append(line)

    text = "\n".join(lines)
    # Telegram лимит 4096 символов на сообщение — обрезаем если надо
    if len(text) > 4000:
        text = text[:4000] + "\n\n_(обрезано)_"

    text += (
        "\n\nЧтобы распарсить подписчиков:\n"
        "`/vk_parse <screen_name>` (например `/vk_parse pomolimsy`)"
    )

    await update.message.reply_text(text, parse_mode="Markdown")


async def vk_parse_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Парсинг подписчиков VK-сообщества (Phase 5).

    Использование:
        /vk_parse pomolimsy           — парсит всех подписчиков
        /vk_parse pomolimsy 100       — парсит первые 100 (для теста)

    На большие группы (>10k) уходит ~3-5 секунд каждые 1000 человек
    из-за rate limit VK. Группа в 60k — около 15 секунд.
    """
    from src.targetolog import VKAPIClient, VKAPIError

    if not settings.vk_api_service_token:
        await update.message.reply_text(
            "⚠️ VK_API_SERVICE_TOKEN не настроен в Railway.",
        )
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Использование:\n"
            "`/vk_parse <screen_name или ID>` — все подписчики\n"
            "`/vk_parse <screen_name> <число>` — только первые N\n\n"
            "Примеры:\n"
            "`/vk_parse pomolimsy 100` — первые 100 для теста\n"
            "`/vk_parse pravoslavnie_hristiane 1000` — первая тысяча",
            parse_mode="Markdown",
        )
        return

    target = args[0].rstrip("/").split("/")[-1]
    max_count = None
    if len(args) >= 2:
        try:
            max_count = int(args[1])
        except ValueError:
            await update.message.reply_text(
                f"Второй аргумент должен быть числом, не «{args[1]}»",
            )
            return

    limit_text = f" (первые {max_count})" if max_count else " (все)"
    await update.message.reply_text(
        f"👥 Парсю подписчиков `{target}`{limit_text}... может занять до минуты на крупных группах.",
        parse_mode="Markdown",
    )

    try:
        async with VKAPIClient(service_token=settings.vk_api_service_token) as client:
            # Сначала получим инфу о группе чтобы знать сколько всего
            try:
                group_info = await client.groups_get_by_id(target)
                total_in_group = group_info.get("members_count", 0)
                group_name = group_info.get("name", target)
            except VKAPIError:
                total_in_group = None
                group_name = target

            members = await client.groups_get_members(target, max_count=max_count)
    except VKAPIError as e:
        await update.message.reply_text(
            f"❌ VK API ошибка {e.code}: {e.message}\n\n"
            f"Если код 15 — сообщество закрытое, подписчики скрыты.",
        )
        return
    except Exception as e:
        logger.exception("vk_parse failed")
        await update.message.reply_text(
            f"❌ Ошибка: `{type(e).__name__}: {e}`",
            parse_mode="Markdown",
        )
        return

    # Сводка результата
    n = len(members)
    reply = f"✅ *Парсинг завершён*\n\n"
    reply += f"Сообщество: {group_name}\n"
    if total_in_group:
        total_fmt = f"{total_in_group:,}".replace(",", " ")
        reply += f"Всего подписчиков: {total_fmt}\n"
    reply += f"Собрано ID: {n:,}".replace(",", " ") + "\n\n"
    reply += f"Первые 5 ID: {members[:5]}\n"
    if n > 5:
        reply += f"Последние 5 ID: {members[-5:]}\n\n"
    reply += "_Готово к пересечениям и выгрузке в VK Ads (следующая итерация)._"

    await update.message.reply_text(reply, parse_mode="Markdown")


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Главное меню бота — ReplyKeyboard под клавиатурой Telegram.

    Появилось когда Vizit попросил «кнопки команд». На iPhone это самое
    удобное — постоянное меню которое не теряется в истории.

    Кнопки сразу запускают действия (без ввода команд):
    - Команды без аргументов (status, biba, vk_check) — выполняются сразу
    - Команды с аргументами — у нас есть кнопки для частых случаев
      (поиск «православие», парсинг pomolimsy)
    - Для произвольных аргументов — Vizit вводит /vk_search или /vk_parse вручную
    """
    from telegram import KeyboardButton, ReplyKeyboardMarkup

    keyboard = [
        [
            KeyboardButton("📊 Статус кабинета"),
            KeyboardButton("🔧 Проверить VK API"),
        ],
        [
            KeyboardButton("☦️ Православные сообщества"),
            KeyboardButton("👥 Парсить pomolimsy"),
        ],
        [
            KeyboardButton("🔍 Поиск: православие"),
            KeyboardButton("🔍 Поиск: монастырь"),
        ],
        [
            KeyboardButton("👁 Биба-разведчик"),
            KeyboardButton("📋 Все команды"),
        ],
    ]
    markup = ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,  # подгоняет размер кнопок под экран
        is_persistent=True,    # меню остаётся между сообщениями
    )

    await update.message.reply_text(
        "🎛 *Меню готово*\n\n"
        "Кнопки внизу — нажимай. Каждая сразу запускает действие.\n\n"
        "Если нужно другое ключевое слово или сообщество — введи команду вручную:\n"
        "`/vk_search <твоё ключевое слово>`\n"
        "`/vk_parse <screen_name> [число]`",
        parse_mode="Markdown",
        reply_markup=markup,
    )


# Маппинг текста ReplyKeyboard-кнопок → название команды/действия.
# Используется в handle_text для роутинга нажатий на кнопки.
MENU_BUTTON_ROUTES = {
    "📊 Статус кабинета": ("command", "status"),
    "🔧 Проверить VK API": ("command", "vk_check"),
    "☦️ Православные сообщества": ("command", "vk_orthodox"),
    "🔍 Поиск: православие": ("vk_search", "православие"),
    "🔍 Поиск: монастырь": ("vk_search", "монастырь"),
    "🔍 Поиск: молитва": ("vk_search", "молитва"),
    "👥 Парсить pomolimsy": ("vk_parse", "pomolimsy 100"),
    "👁 Биба-разведчик": ("command", "biba"),
    "📋 Все команды": ("command", "help"),
}


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Старый inline-callback (не используется в новом ReplyKeyboard-меню).

    Оставлен на случай если где-то в будущих фичах появятся inline-кнопки
    с pattern `menu_`. Сейчас просто молча отвечает на callback.
    """
    query = update.callback_query
    if query:
        await query.answer()


async def vk_orthodox_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Расширенный поиск ПРАВОСЛАВНЫХ сообществ с фильтрацией мусора.

    Phase 5.1 — Vizit указал что обычный поиск по «монастырь» возвращает
    приколы, сатанистов, католиков. Нужна предфильтрация.

    Параллельно ищет по 6 ключам («православная церковь», «православный
    монастырь», «православный собор», «православная часовня»,
    «православная икона», «православные молитвы»), дедупит, выкидывает
    группы со стоп-словами и мелкие.
    """
    from src.targetolog import VKAPIClient, VKAPIError

    if not settings.vk_api_service_token:
        await update.message.reply_text(
            "⚠️ VK_API_SERVICE_TOKEN не настроен в Railway.",
        )
        return

    await update.message.reply_text(
        "☦️ Ищу православные сообщества...\n\n"
        "Запускаю 6 параллельных поисков (церкви, монастыри, соборы, "
        "часовни, иконы, молитвы), отсекаю приколы/сатанистов/другие "
        "конфессии. Займёт секунд 5-10.",
    )

    try:
        async with VKAPIClient(service_token=settings.vk_api_service_token) as client:
            groups = await client.find_orthodox_communities(min_members=500)
    except VKAPIError as e:
        await update.message.reply_text(
            f"❌ VK API ошибка {e.code}: {e.message}",
        )
        return
    except Exception as e:
        logger.exception("vk_orthodox failed")
        await update.message.reply_text(
            f"❌ Ошибка: `{type(e).__name__}: {e}`",
            parse_mode="Markdown",
        )
        return

    if not groups:
        await update.message.reply_text(
            "Ничего не нашлось. Это странно — обычно VK что-то возвращает. "
            "Возможно проблема с токеном или сетью.",
        )
        return

    # Берём топ-25 чтобы не разорвать Telegram-лимит на сообщение (4096 символов)
    top = groups[:25]

    lines = [f"☦️ *Найдено {len(groups)} православных сообществ. Топ-{len(top)}:*\n"]
    for i, g in enumerate(top, 1):
        name = g.get("name", "?")
        screen = g.get("screen_name", "?")
        members = g.get("members_count", 0)
        members_fmt = f"{members:,}".replace(",", " ")
        lines.append(f"{i}. *{name}*\n   `{screen}` — {members_fmt} подписчиков")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n_(обрезано)_"

    text += (
        "\n\n⚠️ *Глазами проверь* — фильтр чистит явный мусор (приколы, "
        "сатанистов, другие конфессии), но всё равно проскользнуть может. "
        "Когда отметишь нужные — пиши `/vk_parse <screen_name> 1000` "
        "по каждому, я соберу подписчиков."
    )

    await update.message.reply_text(text, parse_mode="Markdown")
