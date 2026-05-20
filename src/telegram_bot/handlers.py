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
            KeyboardButton("💼 Поговорить с Бобой (CMO)"),
            KeyboardButton("🔥 Горячая аудитория"),
        ],
        [
            KeyboardButton("🧠 Поговорить с агентом"),
            KeyboardButton("📊 Статус кабинета"),
        ],
        [
            KeyboardButton("🔧 Проверить VK API"),
            KeyboardButton("☦️ Православные сообщества"),
        ],
        [
            KeyboardButton("👥 Парсить Верую"),
            KeyboardButton("👁 Биба-разведчик"),
        ],
        [
            KeyboardButton("📋 Все команды"),
        ],
    ]
    markup = ReplyKeyboardMarkup(
        keyboard, resize_keyboard=True, is_persistent=True
    )

    await update.message.reply_text(
        "🤖 *Я — твой VK-маркетолог.*\n\n"
        "Главное: *💼 Поговорить с Бобой* — CMO нашей рекламной "
        "организации, жёсткий стратег, даёт 3 варианта стратегии с прогнозом. "
        "Или *🔥 Горячая аудитория* — бот соберёт подписчиков 8 крупных правосл. "
        "сообществ и найдёт тех кто в 2+ группах одновременно.",
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
        "/boba — поговорить с Бобой (CMO Рекламной Организации) 💼\n"
        "/agent — поговорить с агентом-таргетологом 🧠\n"
        "/vk_audience [N|full] — собрать горячую аудиторию 🔥\n"
        "/start — приветствие\n"
        "/help — это сообщение\n"
        "/status — реальный статус кабинета VK\n"
        "/biba — карта функционала кабинета VK Ads (REPORT.md + raw JSON в zip)\n"
        "/vk_check [screen_name] — проверка VK API service token\n"
        "/vk_search <ключ> — поиск VK-сообществ по теме\n"
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
                for c in active[:20]:
                    name = c.get("name", "?")[:40]
                    daily = c.get("budget_limit_day", "—")
                    # Backticks вокруг имени — внутри них Markdown игнорируется.
                    # Имена могут содержать _ и * (smoke_test, batch ж41-46 и т.д.),
                    # без backticks Telegram падает на parse_entities.
                    lines.append(f"  • `{name}` (дневной: {daily})")
                if len(active) > 20:
                    lines.append(f"  ... и ещё {len(active) - 20}")

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
    """Получили фото — скачиваем, генерим варианты текста, показываем для выбора.

    Phase 5.11: batch режим имеет приоритет над smoke test и обычным фото-flow.
    Phase 5.9: smoke test проверяется вторым.
    Обычный фото-flow — третьим (когда нет специальных режимов).
    """
    # Phase 5.17: launch_20 режим (20 кампаний за 4 фото)
    if await handle_launch_20_photo(update, context):
        return

    # Phase 5.11: batch режим (12 кампаний за 3 фото)
    if await handle_batch_photo(update, context):
        return

    # Phase 5.9: smoke test (одно фото → одна кампания)
    if await handle_smoke_test_photo(update, context):
        return

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

    Порядок проверок:
    1. Кнопка меню — выход из режима агента + запуск действия.
    2. Режим агента активен — отправляем в Claude через TargetologAgent.
       Слово «выйти» — выход из режима.
    3. Обычное текстовое сообщение — плейсхолдер с подсказкой.
    """
    text = (update.message.text or "").strip()
    logger.info(f"Получен текст: '{text[:100]}'")

    # 1. Проверяем — это кнопка меню?
    if text in MENU_BUTTON_ROUTES:
        # При нажатии кнопки меню — автоматический выход из режимов агентов
        if context.user_data.get("in_agent_mode"):
            context.user_data["in_agent_mode"] = False
            context.user_data.pop("agent_history", None)
        if context.user_data.get("in_boba_mode"):
            context.user_data["in_boba_mode"] = False
            context.user_data.pop("boba_history", None)

        action_type, action_value = MENU_BUTTON_ROUTES[text]

        if action_type == "command":
            # Команды без аргументов — вызываем handler напрямую
            command_map = {
                "status": status_command,
                "vk_check": vk_check_command,
                "vk_orthodox": vk_orthodox_command,
                "vk_audience": vk_audience_command,
                "biba": biba_command,
                "help": help_command,
                "agent": agent_command,
                "boba": boba_command,
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

    # 2. Режим Бобы (CMO) активен — все текстовые сообщения идут в Бобу
    if context.user_data.get("in_boba_mode"):
        if text.lower() in ("выйти", "выход", "exit", "стоп", "quit"):
            context.user_data["in_boba_mode"] = False
            context.user_data.pop("boba_history", None)
            await update.message.reply_text(
                "💼 Вышел из диалога с Бобой. Меню снова работает как обычно."
            )
            return

        await _boba_chat_turn(update, context, text)
        return

    # 3. Режим агента-таргетолога активен
    if context.user_data.get("in_agent_mode"):
        # Команды выхода из режима
        if text.lower() in ("выйти", "выход", "exit", "стоп", "quit"):
            context.user_data["in_agent_mode"] = False
            context.user_data.pop("agent_history", None)
            await update.message.reply_text(
                "🧠 Вышел из режима агента. Меню снова работает как обычно."
            )
            return

        await _agent_chat_turn(update, context, text)
        return

    # 4. Обычное текстовое сообщение
    await update.message.reply_text(
        "📝 Принято. Что можно делать:\n"
        "• `/menu` — меню с кнопками\n"
        "• `/boba` — поговорить с Бобой (CMO) 💼\n"
        "• `/agent` — разговор с агентом-таргетологом 🧠\n"
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
            KeyboardButton("💼 Поговорить с Бобой (CMO)"),
            KeyboardButton("🔥 Горячая аудитория"),
        ],
        [
            KeyboardButton("🧠 Поговорить с агентом"),
            KeyboardButton("📊 Статус кабинета"),
        ],
        [
            KeyboardButton("🔧 Проверить VK API"),
            KeyboardButton("☦️ Православные сообщества"),
        ],
        [
            KeyboardButton("👥 Парсить Верую"),
            KeyboardButton("👁 Биба-разведчик"),
        ],
        [
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
        "💼 Боба — CMO рекламной организации, жёсткий стратег. "
        "🧠 Агент-таргетолог — мягкий советник. "
        "🔥 Горячая аудитория — сбор автоматом за пару минут.",
        parse_mode="Markdown",
        reply_markup=markup,
    )


# Маппинг текста ReplyKeyboard-кнопок → название команды/действия.
# Используется в handle_text для роутинга нажатий на кнопки.
MENU_BUTTON_ROUTES = {
    "📊 Статус кабинета": ("command", "status"),
    "🔧 Проверить VK API": ("command", "vk_check"),
    "🔥 Горячая аудитория": ("command", "vk_audience"),
    "👥 Парсить Верую": ("vk_parse", "pravoslavnie_hristiane 1000"),
    "💼 Поговорить с Бобой (CMO)": ("command", "boba"),
    "🧠 Поговорить с агентом": ("command", "agent"),
    "☦️ Православные сообщества": ("command", "vk_orthodox"),
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


async def vk_audience_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сбор горячей православной аудитории — Phase 5.2.

    Запускает пайплайн:
    1. Парсит подписчиков 8 крупных открытых правосл. сообществ из
       захардкоженного списка (orthodox_seed.py)
    2. Считает пересечения — кто состоит в 2+ группах одновременно
    3. Показывает Vizit'у статистику и распределение

    Опциональный аргумент:
        /vk_audience           — парсит первые 5000 с каждой (тест-режим)
        /vk_audience full      — парсит ВСЕХ (может занять 15-30 минут)
        /vk_audience 1000      — парсит первые 1000 с каждой

    На следующем шаге (Phase 5.3) — выгрузка hot_users как CSV-сегмент
    в VK Ads через API кабинета. Сейчас только показ цифр.
    """
    from src.targetolog import VKAPIClient, VKAPIError
    from src.targetolog.intersections import find_intersections, parse_groups_parallel
    from src.targetolog.orthodox_seed import (
        ORTHODOX_COMMUNITIES_SEED,
        get_seed_screen_names,
    )

    if not settings.vk_api_service_token:
        await update.message.reply_text(
            "⚠️ VK_API_SERVICE_TOKEN не настроен в Railway.",
        )
        return

    # Парсинг аргументов
    args = context.args or []
    max_per_group: int | None = 5000  # дефолт — тест-режим
    if args:
        if args[0].lower() == "full":
            max_per_group = None
        else:
            try:
                max_per_group = int(args[0])
            except ValueError:
                await update.message.reply_text(
                    f"Аргумент должен быть числом или 'full', не «{args[0]}»",
                )
                return

    seed_count = len(ORTHODOX_COMMUNITIES_SEED)
    mode_text = "все подписчики" if max_per_group is None else f"первые {max_per_group}"

    # Грубая оценка времени
    if max_per_group is None:
        est_seconds = seed_count * 60  # ~60 сек/группа в среднем при rate limit
        time_text = f"~{est_seconds // 60} минут"
    else:
        # 4 req/sec, по 1000 в запросе, + 1 req на getById
        reqs_per_group = (max_per_group + 999) // 1000 + 1
        est_seconds = (seed_count * reqs_per_group) / 4
        time_text = f"~{int(est_seconds)} секунд"

    await update.message.reply_text(
        f"☦️ *Сбор горячей аудитории*\n\n"
        f"Парсю {seed_count} крупных православных сообществ ({mode_text}).\n"
        f"Оценка времени: {time_text}.\n\n"
        f"После — найду пересечения и покажу сколько человек подписано "
        f"на 2+ групп одновременно. Это и есть горячая аудитория.",
        parse_mode="Markdown",
    )

    try:
        async with VKAPIClient(service_token=settings.vk_api_service_token) as client:
            results = await parse_groups_parallel(
                client, get_seed_screen_names(), max_per_group=max_per_group
            )

            # Считаем пересечения с min=2 (минимум 2 группы)
            intersection = find_intersections(results, min_intersections=2)

            # Phase 5.7 (15.05.2026, после Vizit вопроса про чистоту базы):
            # Фильтруем горячих по активности и deactivated.
            # Боба сказал: 14 дней — компромисс между чистотой и размером
            # базы для православной ниши (45-58 не каждый день в VK).
            # Также убираем banned/deleted аккаунты (мёртвый мусор).
            from src.targetolog.audience_filter import AudienceFilter, filter_audience

            hot_ids_raw = list(intersection.hot_users)
            audience_filter_obj = AudienceFilter(
                sex=None,           # пол не фильтруем (VK Ads сам сужает)
                min_age=None,       # возраст не фильтруем (VK Ads сам)
                max_age=None,
                min_last_seen_days=14,  # 14 дней — фильтр активности
            )

            await update.message.reply_text(
                f"🧹 Чищу базу: убираю мёртвые и неактивные аккаунты "
                f"(не заходили в VK 14+ дней)... "
                f"Запрашиваю профили для {len(hot_ids_raw)} ID..."
            )

            # users.get с полями: deactivated + last_seen
            profiles = await client.users_get_batch(
                user_ids=hot_ids_raw,
                fields="deactivated,last_seen",
            )

            filtered_ids, filter_stats = filter_audience(profiles, audience_filter_obj)
    except Exception as e:
        logger.exception("vk_audience parse failed")
        await update.message.reply_text(
            f"❌ Ошибка при парсинге: `{type(e).__name__}: {e}`",
            parse_mode="Markdown",
        )
        return

    # Сводка по группам
    lines = ["📊 *Результаты парсинга:*\n"]
    for r in results:
        if r.error:
            lines.append(
                f"❌ `{r.screen_name}` ({r.group_name}): {r.error}"
            )
        else:
            collected = len(r.parsed_members)
            total = r.total_members
            collected_fmt = f"{collected:,}".replace(",", " ")
            total_fmt = f"{total:,}".replace(",", " ")
            lines.append(
                f"✅ `{r.screen_name}` ({r.group_name}): "
                f"собрано {collected_fmt} из {total_fmt}"
            )

    # Сводка пересечений
    total = intersection.total_unique_users
    hot = intersection.hot_users_count
    total_fmt = f"{total:,}".replace(",", " ")
    hot_fmt = f"{hot:,}".replace(",", " ")
    pct = (hot / total * 100) if total else 0

    lines.append("")
    lines.append("🔥 *Пересечения (до фильтрации):*")
    lines.append(f"Всего уникальных людей: {total_fmt}")
    lines.append(f"Из них в 2+ группах одновременно: *{hot_fmt}* ({pct:.1f}%)")
    lines.append("")
    lines.append("*Распределение:*")
    dist = intersection.get_distribution()
    for n_groups in sorted(dist.keys()):
        count = dist[n_groups]
        count_fmt = f"{count:,}".replace(",", " ")
        lines.append(f"  в {n_groups} группах: {count_fmt} чел.")

    # Phase 5.7 — статистика фильтрации по активности
    lines.append("")
    lines.append("🧹 *После чистки базы (фильтр 14 дней + deactivated):*")
    matched_fmt = f"{filter_stats.matched:,}".replace(",", " ")
    deact_fmt = f"{filter_stats.skipped_deactivated:,}".replace(",", " ")
    inact_fmt = f"{filter_stats.skipped_inactive:,}".replace(",", " ")
    lines.append(f"✅ Чистых активных: *{matched_fmt}* ({filter_stats.matched_pct:.1f}%)")
    lines.append(f"  убрано banned/deleted: {deact_fmt}")
    lines.append(f"  убрано неактивных 14+ дней: {inact_fmt}")

    lines.append("")
    lines.append(
        "_Следующий шаг — автовыгрузка чистой базы в VK Ads как сегмент._"
    )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n_(обрезано)_"

    # Defensive: иногда Markdown ломается на спец. символах из VK ошибок
    # (backticks, звёздочки, подчёркивания внутри названий групп или
    # error messages). Тогда Telegram возвращает 400 'can't parse entities'.
    # Если упало — отправляем plain text без форматирования.
    try:
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Markdown render failed, fallback to plain: {e}")
        # Убираем markdown спецсимволы для plain text
        plain_text = text.replace("*", "").replace("_", "").replace("`", "")
        await update.message.reply_text(plain_text)

    # Логируем критичные числа в Railway logs для диагностики
    logger.info(
        f"vk_audience результаты: groups={len(results)}, "
        f"errors={sum(1 for r in results if r.error)}, "
        f"intersection_total={intersection.total_unique_users}, "
        f"hot_raw={len(hot_ids_raw)}, "
        f"after_filter={filter_stats.matched}"
    )

    # Phase 5.7 — используем отфильтрованный список (а не сырой intersection.hot_users)
    target_audience_ids: list[int] = sorted(filtered_ids)

    # Phase 5.4 — формируем CSV для ручной загрузки в VK Ads.
    # VK Ads принимает обычный текстовый файл со списком user_id, по одному
    # на строку. Поэтому формат тривиальный.
    if target_audience_ids:
        import io
        from datetime import datetime

        # Сохраняем в BytesIO чтобы не плодить файлы на диске Railway
        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        filename = f"hot_audience_{timestamp}.csv"
        buf = io.BytesIO()
        sorted_audience_ids = sorted(target_audience_ids)
        for uid in sorted_audience_ids:
            buf.write(f"{uid}\n".encode("utf-8"))
        buf.seek(0)

        target_count = len(sorted_audience_ids)
        hot_count_fmt = f"{target_count:,}".replace(",", " ")
        await update.message.reply_document(
            document=buf,
            filename=filename,
            caption=(
                f"📤 *Горячая аудитория для VK Ads* — {hot_count_fmt} человек\n\n"
                f"Подписаны на 2+ правосл. сообщества одновременно. "
                f"Узкое сужение до женщин 41-58 произойдёт на стороне VK Ads "
                f"через `targetings.age` + `targetings.sex` в самой кампании — "
                f"у них больше данных для определения возраста.\n\n"
                f"Если 2000+ — бот сейчас попробует автозагрузить, иначе "
                f"можно загрузить файл руками (VK Ads → Аудитории → Создать → Из файла)."
            ),
            parse_mode="Markdown",
        )

        # Phase 5.4 — автозагрузка в VK Ads через API
        from src.vk_ads.client import VKAdsAPIError, VKAdsClient

        if target_count < 2000:
            await update.message.reply_text(
                f"⚠️ В ЦА всего {hot_count_fmt}, а VK Ads API требует "
                f"*минимум 2000* для создания сегмента. Автозагрузка "
                f"пропущена — можно загрузить файл руками (он выше). "
                f"Либо запусти `/vk_audience full` чтобы собрать всю "
                f"аудиторию каждой группы — после фильтра горячих "
                f"будет в разы больше.",
                parse_mode="Markdown",
            )
            return

        # Достаточно ID — пробуем автозагрузку
        ads_client = VKAdsClient.from_settings()
        if ads_client is None:
            await update.message.reply_text(
                "⚠️ VK Ads клиент не настроен (нет OAuth credentials). "
                "Загрузи файл руками.",
            )
            return

        list_name = f"Pomolimsy ЦА {timestamp}"
        await update.message.reply_text(
            f"⏳ Автозагружаю в VK Ads как `{list_name}`...",
            parse_mode="Markdown",
        )

        try:
            result = await ads_client.create_remarketing_users_list(
                name=list_name,
                user_ids=sorted_audience_ids,
            )
        except VKAdsAPIError as e:
            await update.message.reply_text(
                f"❌ VK Ads API отверг загрузку: `{e}`\n\n"
                f"Можно загрузить файл руками (он выше).",
                parse_mode="Markdown",
            )
            return
        except Exception as e:
            logger.exception("Auto-upload to VK Ads failed")
            await update.message.reply_text(
                f"❌ Не получилось автозагрузить: `{type(e).__name__}: {e}`\n\n"
                f"Можно загрузить файл руками (он выше).",
                parse_mode="Markdown",
            )
            return

        list_id = result.get("id")
        status = result.get("status", "?")
        entries = result.get("entries_count", "?")

        await update.message.reply_text(
            f"✅ *Список создан в VK Ads*\n\n"
            f"ID: `{list_id}`\n"
            f"Загружено: {entries} ID\n"
            f"Статус: `{status}` (обработка займёт несколько минут, "
            f"потом станет `ready`)\n\n"
            f"Что дальше — положи этот ID в Railway env "
            f"`VK_AUDIENCE_SEGMENT_IDS` (через запятую если их уже "
            f"несколько). После следующего деплоя бот будет рекламировать "
            f"новые кампании именно на эту аудиторию.",
            parse_mode="Markdown",
        )


async def agent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вход в режим разговора с агентом-таргетологом (Phase 5.2).

    После /agent — все текстовые сообщения уходят напрямую к Claude
    через src/targetolog/agent.py. Агент понимает контекст бизнеса
    (pomolimsy, ЦА женщины 41-58, православная ниша), помнит диалог,
    подсказывает что делать.

    Выход из режима — команда /menu (или /start), либо текст «выйти».
    """
    context.user_data["in_agent_mode"] = True
    context.user_data["agent_history"] = []  # начинаем диалог с чистого листа

    await update.message.reply_text(
        "🧠 *Включил режим разговора с таргетологом.*\n\n"
        "Теперь пиши что хочешь — я отвечаю как эксперт, помню контекст "
        "беседы, объясняю что могу и не могу. Действовать сам пока не "
        "могу (Phase 5.2.5) — пока подсказываю какие кнопки/команды нажать тебе.\n\n"
        "Чтобы выйти из режима — напиши «выйти» или нажми любую кнопку меню.",
        parse_mode="Markdown",
    )


async def _agent_chat_turn(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Один turn разговора с агентом. Внутренний хелпер."""
    from src.targetolog.agent import TargetologAgent

    # Покажем «печатает...» в Telegram чтобы Vizit не думал что зависло
    await update.message.chat.send_action(action="typing")

    history = context.user_data.get("agent_history", [])

    try:
        agent = TargetologAgent()
        response = await agent.chat(user_message=text, history=history)
    except Exception as e:
        logger.exception("Agent chat failed")
        await update.message.reply_text(
            f"❌ Агент не смог ответить: `{type(e).__name__}`. "
            f"Попробуй ещё раз или выйди через «выйти».",
            parse_mode="Markdown",
        )
        return

    # Сохраняем обновлённую историю для следующего turn
    context.user_data["agent_history"] = response.updated_history

    # Telegram-лимит 4096 символов — если ответ длиннее, разбиваем
    text_to_send = response.text
    if len(text_to_send) > 4000:
        # Простое разбиение по абзацам, не идеальное но рабочее
        chunks = []
        current = ""
        for para in text_to_send.split("\n\n"):
            if len(current) + len(para) + 2 < 4000:
                current += ("\n\n" if current else "") + para
            else:
                if current:
                    chunks.append(current)
                current = para
        if current:
            chunks.append(current)

        for chunk in chunks:
            await update.message.reply_text(chunk, parse_mode="Markdown")
    else:
        # Markdown может ломаться на нестандартных символах от Claude
        # (звёздочки в неожиданных местах). Если падает — отправим как plain text.
        try:
            await update.message.reply_text(text_to_send, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(text_to_send)




async def boba_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вход в режим разговора с Бобой — CMO Рекламной Организации Vizit.

    Phase 6 — первая итерация диалога с Бобой как CMO для Vizit'а.
    Раньше Боба был только моим (Claude) внутренним консультантом, теперь
    он становится также консультантом Vizit'а через Telegram.

    Боба — жёсткий критик, VK-гуру 43 лет, специализация на стратегии.
    Не подменяет собой существующего агента-таргетолога (/agent) — это
    другая роль и другой характер.

    Выход из режима — текст «выйти» или нажатие любой кнопки меню.
    """
    context.user_data["in_boba_mode"] = True
    context.user_data["boba_history"] = []  # начинаем диалог с чистого листа

    # Также выключаем режим таргетолога если он был включён — нельзя
    # одновременно говорить с двумя агентами.
    context.user_data["in_agent_mode"] = False

    await update.message.reply_text(
        "💼 *Боба на связи.* CMO Рекламной Организации Vizit.\n\n"
        "Тебе 43, я VK-гуру с 13-летним стажем, прошёл все версии "
        "рекламного кабинета. Специализируюсь на стратегии — общая картина, "
        "что делать когда. Жёсткий, без воды.\n\n"
        "Пиши что у тебя — задачу, идею, проблему с кампанией. Я разложу "
        "по полочкам, дам 3 варианта стратегии с прогнозом CPL.\n\n"
        "Выйти — напиши «выйти» или нажми любую кнопку меню.",
        parse_mode="Markdown",
    )


async def _boba_chat_turn(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Один turn разговора с Бобой. Внутренний хелпер.

    Phase 7 (15.05.2026): подключены tools — Боба может сам вызывать
    функции (статус кабинета, парсинг, статистика, чтение/запись базы знаний).
    """
    from src.agents import BOBA_PERSONA
    from src.agents.boba_tools import BOBA_TOOLS_SCHEMA, execute_tool
    from src.agents.dialog import DialogAgent

    await update.message.chat.send_action(action="typing")

    history = context.user_data.get("boba_history", [])

    try:
        agent = DialogAgent(persona=BOBA_PERSONA)
        response = await agent.chat(
            user_message=text,
            history=history,
            tools=BOBA_TOOLS_SCHEMA,
            tool_executor=execute_tool,
        )
    except Exception as e:
        logger.exception("Boba chat failed")
        await update.message.reply_text(
            f"❌ Боба не смог ответить: `{type(e).__name__}`. "
            f"Попробуй ещё раз или выйди через «выйти».",
            parse_mode="Markdown",
        )
        return

    # Сохраняем обновлённую историю для следующего turn
    context.user_data["boba_history"] = response.updated_history

    # Разбиение длинных ответов по 4000 символов (Telegram лимит 4096)
    text_to_send = response.text
    if len(text_to_send) > 4000:
        chunks = []
        current = ""
        for para in text_to_send.split("\n\n"):
            if len(current) + len(para) + 2 < 4000:
                current += ("\n\n" if current else "") + para
            else:
                if current:
                    chunks.append(current)
                current = para
        if current:
            chunks.append(current)

        for chunk in chunks:
            try:
                await update.message.reply_text(chunk, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(chunk)
    else:
        try:
            await update.message.reply_text(text_to_send, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(text_to_send)


# ============================================================
# Phase 5.8 (16.05.2026) — загрузка готового списка ID из TargetHunter
# ============================================================
#
# Vizit использует TargetHunter (профессиональный инструмент парсинга VK)
# для сбора горячей аудитории — наш собственный парсер уперлся в flood
# control при больших объёмах. TargetHunter решает это через пул токенов.
#
# Workflow:
# 1. Vizit в TargetHunter парсит подписчиков нужных правосл. сообществ
#    с фильтрами (активные за 14 дней, состоят в 2+ групп, минус
#    подписчики pomolimsy) — получает txt с user_id, по одному на строку
# 2. В Telegram-боте вызывает /upload_audience
# 3. Бот ожидает документ
# 4. Vizit присылает .txt файл
# 5. Бот парсит, валидирует (минимум 2000 ID для VK Ads),
#    создаёт remarketing-сегмент через VKAdsClient.create_remarketing_users_list()
# 6. Возвращает segment_id который можно использовать в VK_AUDIENCE_SEGMENT_IDS


async def upload_audience_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запускает режим ожидания txt-файла с VK ID для загрузки в VK Ads.

    Показывает инструкцию и ставит флаг ожидания файла в user_data.
    Следующий .txt документ от Vizit'а будет обработан handle_audience_document.
    """
    args = context.args or []

    # Опциональный аргумент — название будущего сегмента
    segment_name = " ".join(args) if args else None

    context.user_data["awaiting_audience_upload"] = True
    context.user_data["audience_segment_name"] = segment_name

    instruction = (
        "📤 *Загрузка аудитории из TargetHunter*\n\n"
        "Жду от тебя `.txt` файл со списком VK ID:\n"
        "— один ID на строку\n"
        "— минимум 2 000 ID (требование VK Ads)\n"
        "— максимум 5 000 000 ID\n"
        "— только числа, без пробелов и лишних символов\n\n"
        "Это стандартный формат экспорта из TargetHunter "
        "(выбор «Сохранить → Текстовый файл → ID»)."
    )
    if segment_name:
        instruction += f"\n\nНазвание сегмента в VK Ads: *{segment_name}*"
    else:
        instruction += (
            "\n\nНазвание сегмента будет автоматическое "
            "(дата + размер). Если хочешь своё — отмени и запусти "
            "`/upload_audience Моё название`."
        )
    instruction += "\n\nПросто отправь файл в этот чат. Или напиши «отмена» чтобы выйти."

    await update.message.reply_text(instruction, parse_mode="Markdown")


async def handle_audience_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Обрабатывает приходящий .txt файл когда стоит флаг ожидания.

    Скачивает файл, парсит VK ID, создаёт remarketing-сегмент через
    VKAdsClient.
    """
    from datetime import datetime

    if not context.user_data.get("awaiting_audience_upload"):
        # Не наш режим — игнорируем (документ мог прийти случайно)
        return

    document = update.message.document
    if not document:
        return

    # Проверка имени файла — txt
    file_name = document.file_name or ""
    if not file_name.lower().endswith((".txt", ".csv")):
        await update.message.reply_text(
            f"❌ Ожидаю `.txt` или `.csv` файл, ты прислал `{file_name}`. "
            f"Перезапусти через /upload_audience и пришли правильный формат.",
            parse_mode="Markdown",
        )
        context.user_data["awaiting_audience_upload"] = False
        return

    # Размер файла — sanity check
    if document.file_size and document.file_size > 100 * 1024 * 1024:
        await update.message.reply_text(
            f"❌ Файл слишком большой ({document.file_size / 1024 / 1024:.1f} MB). "
            f"Максимум 100 MB. Если у тебя реально 5 миллионов ID — режь "
            f"на несколько файлов."
        )
        context.user_data["awaiting_audience_upload"] = False
        return

    await update.message.reply_text(
        f"📥 Получил `{file_name}` ({document.file_size or '?'} байт). Читаю...",
        parse_mode="Markdown",
    )

    # Скачиваем содержимое файла
    try:
        tg_file = await document.get_file()
        file_bytes = await tg_file.download_as_bytearray()
        raw_text = bytes(file_bytes).decode("utf-8", errors="ignore")
    except Exception as e:
        logger.exception("Failed to download audience file")
        await update.message.reply_text(
            f"❌ Не смог скачать файл: `{type(e).__name__}: {e}`",
            parse_mode="Markdown",
        )
        context.user_data["awaiting_audience_upload"] = False
        return

    # Парсим строки в VK ID
    user_ids, parse_stats = _parse_audience_file(raw_text)

    if not user_ids:
        await update.message.reply_text(
            f"❌ В файле не нашёл ни одного валидного VK ID.\n"
            f"Всего строк прочитано: {parse_stats['total_lines']}\n"
            f"Невалидных строк: {parse_stats['invalid_lines']}\n\n"
            f"Проверь что формат правильный (число на строку)."
        )
        context.user_data["awaiting_audience_upload"] = False
        return

    # Валидация количества под требования VK Ads
    if len(user_ids) < 2000:
        await update.message.reply_text(
            f"⚠️ В файле {len(user_ids)} ID — это меньше минимума VK Ads "
            f"(2000 для создания remarketing-сегмента).\n\n"
            f"Решения:\n"
            f"1) Спарси больше в TargetHunter (ослабь фильтр активности "
            f"или добавь сообществ)\n"
            f"2) Или дождись пока соберётся минимум 2000 и загрузи снова\n\n"
            f"Загрузка отменена."
        )
        context.user_data["awaiting_audience_upload"] = False
        return

    if len(user_ids) > 5_000_000:
        await update.message.reply_text(
            f"⚠️ В файле {len(user_ids):,} ID — это больше максимума VK Ads "
            f"(5 000 000). Раздели файл и загружай частями.".replace(",", " ")
        )
        context.user_data["awaiting_audience_upload"] = False
        return

    # Формируем название сегмента
    custom_name = context.user_data.get("audience_segment_name")
    if custom_name:
        segment_name = custom_name
    else:
        today = datetime.now().strftime("%Y-%m-%d")
        size_k = len(user_ids) // 1000
        segment_name = f"TargetHunter {today} ({size_k}k IDs)"

    # Прогресс-сообщение
    await update.message.reply_text(
        f"✅ Спарсил {len(user_ids):,} валидных VK ID.\n".replace(",", " ")
        + f"Невалидных строк отброшено: {parse_stats['invalid_lines']}\n\n"
        f"Создаю в VK Ads сегмент «{segment_name}»... "
        f"(может занять до минуты на больших объёмах)",
        parse_mode="Markdown" if "*" not in segment_name else None,
    )

    # Создание сегмента в VK Ads
    try:
        ads_client = VKAdsClient.from_settings()
        if ads_client is None:
            await update.message.reply_text(
                "❌ VK Ads клиент не настроен. Проверь env vars в Railway."
            )
            context.user_data["awaiting_audience_upload"] = False
            return

        result = await ads_client.create_remarketing_users_list(
            name=segment_name,
            user_ids=user_ids,
            list_type="vk",
        )
    except Exception as e:
        logger.exception("VK Ads remarketing upload failed")
        await update.message.reply_text(
            f"❌ VK Ads вернул ошибку: `{type(e).__name__}: {e}`\n\n"
            f"Возможные причины:\n"
            f"— Истёк OAuth токен (проверь Railway env)\n"
            f"— Лимит ремаркетинг-списков на кабинет\n"
            f"— Сетевой сбой (попробуй ещё раз)",
            parse_mode="Markdown",
        )
        context.user_data["awaiting_audience_upload"] = False
        return

    users_list_id = result.get("id")
    status = result.get("status", "unknown")
    entries_count = result.get("entries_count", "?")

    # Phase 5.10: после создания users_list — создаём Segment (Аудиторию)
    # для использования в targetings кампании. Это два разных объекта в
    # VK Ads (RemarketingUsersList vs Segment), что не было очевидно.
    # Подробности — см. create_audience_segment_from_users_list в client.py.
    await update.message.reply_text(
        f"✅ Список пользователей создан (id={users_list_id}).\n"
        f"🔄 Создаю Аудиторию (Segment) на его основе..."
    )

    try:
        segment_result = await ads_client.create_audience_segment_from_users_list(
            name=segment_name,
            users_list_id=int(users_list_id),
        )
    except Exception as e:
        logger.exception("Segment creation failed after users_list")
        await update.message.reply_text(
            f"⚠️ Список создан (`{users_list_id}`), но Аудитория не создалась: "
            f"`{type(e).__name__}: {e}`\n\n"
            f"Создай Аудиторию вручную:\n"
            f"`/audience_from_list {users_list_id} {segment_name}`\n\n"
            f"Или в UI VK Ads → вкладка «Аудитории» → «Создать» → "
            f"выбери Список пользователей.",
            parse_mode="Markdown",
        )
        context.user_data["awaiting_audience_upload"] = False
        return

    segment_id = segment_result.get("id")

    await update.message.reply_text(
        f"✅ *Готово!*\n\n"
        f"📋 Список пользователей: `{users_list_id}` ({entries_count} ID)\n"
        f"🎯 *Аудитория (Segment): `{segment_id}`* ← это для кампаний\n"
        f"⏳ Status списка: `{status}` (через несколько минут станет `ready`)\n\n"
        f"*Что дальше:*\n"
        f"1) Добавь в Railway env: `VK_AUDIENCE_SEGMENT_IDS={segment_id}` "
        f"(если там несколько — через запятую)\n"
        f"2) Передеплой подхватит автоматически\n"
        f"3) Используй в кампаниях через `/launch_test` или передай Бобе через `/boba`\n\n"
        f"_Примечание: для targetings в кампании нужен ID Аудитории "
        f"({segment_id}), не ID Списка ({users_list_id}). Это разные сущности_",
        parse_mode="Markdown",
    )

    # Снимаем флаг ожидания
    context.user_data["awaiting_audience_upload"] = False
    context.user_data.pop("audience_segment_name", None)

    # Логируем
    logger.info(
        f"upload_audience: users_list={users_list_id} ({entries_count} IDs), "
        f"segment={segment_id}, parse_stats={parse_stats}"
    )


def _parse_audience_file(raw_text: str) -> tuple[list[int], dict]:
    """Парсит txt с VK ID, по одному на строку.

    Принимает строки которые могут содержать:
    - Чистый ID: "123456"
    - С префиксом: "id123456" или "user_123456"
    - С пробелами по краям
    Игнорирует:
    - Пустые строки
    - Строки начинающиеся с '#' (комментарии)
    - Невалидные строки (буквы, спецсимволы)

    Returns:
        (list of int ids, stats dict с total_lines/valid/invalid)
    """
    user_ids: list[int] = []
    total_lines = 0
    invalid_lines = 0

    seen: set[int] = set()  # дедупликация

    for line in raw_text.splitlines():
        total_lines += 1
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Убираем популярные префиксы
        normalized = stripped
        for prefix in ("id", "user_", "vk.com/id", "https://vk.com/id"):
            if normalized.lower().startswith(prefix):
                normalized = normalized[len(prefix):]
                break

        # Должно остаться чистое число
        if not normalized.isdigit():
            invalid_lines += 1
            continue

        uid = int(normalized)
        if uid <= 0:
            invalid_lines += 1
            continue

        if uid not in seen:
            seen.add(uid)
            user_ids.append(uid)

    return user_ids, {
        "total_lines": total_lines,
        "valid": len(user_ids),
        "invalid_lines": invalid_lines,
    }


# ============================================================
# Phase 5.9 (16.05.2026 утром) — команда /launch_test для smoke test
# ============================================================
#
# Smoke test после ночной сессии 15-16.05.2026:
# - Сегмент 80507749 в VK Ads готов (619 146 ID через TargetHunter)
# - Боба согласовал план: одна кампания, A/B на 2 текстах, package 3127
#   ("Написать"), бюджет 500₽/день × 3 дня = 1500₽
# - Тексты A и B одобрены Vizit'ом
# - Фото от Vizit'а (картинка с монахом и чётками)
#
# Workflow:
# 1. /launch_test → бот ставит флаг ожидания фото
# 2. Vizit присылает фото в чат
# 3. Бот вызывает AdCreator.create_smoke_test_campaign() с захардкоженными
#    текстами A и B
# 4. Бот возвращает ad_plan_id и инструкцию проверить в кабинете VK Ads

# Тексты под smoke test — одобрены Vizit'ом и Бобой
SMOKE_TEST_COPY_A_TITLE = "Помолимся о ваших близких"
SMOKE_TEST_COPY_A_TEXT = (
    "Если в вашем сердце есть имя — родного, болеющего, ушедшего — "
    "напишите нам. Помолимся вместе.\n\n"
    "О здравии или упокоении — просто отправьте имена сообщением."
)
SMOKE_TEST_COPY_A_ABOUT = "Молитвенное сообщество. Напишите имена близких."
SMOKE_TEST_COPY_A_CTA = "write"  # для package 3127

SMOKE_TEST_COPY_B_TITLE = "Имя дорогого человека"
SMOKE_TEST_COPY_B_TEXT = (
    "Имя дорогого вам человека может стать частью молитвы.\n\n"
    "Напишите сюда имена близких — о здравии или упокоении — и они "
    "войдут в молитвенное правило нашей общины."
)
SMOKE_TEST_COPY_B_ABOUT = "Молитвенное сообщество. Имена для молитвы."
SMOKE_TEST_COPY_B_CTA = "write"


async def launch_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запускает smoke test после сбора аудитории через TargetHunter.

    Создаёт одну кампанию в VK Ads с A/B на 2 текстах. Сегмент берётся
    из env VK_AUDIENCE_SEGMENT_IDS автоматически. Бюджет фиксирован
    500₽/день × 3 дня = 1500₽.
    """
    # Проверка что сегмент задан
    from src.config import settings
    segments = settings.vk_audience_segment_ids_parsed
    if not segments:
        await update.message.reply_text(
            "❌ В Railway env не задан `VK_AUDIENCE_SEGMENT_IDS`.\n\n"
            "Сейчас сделай:\n"
            "1. Открой Railway → vk-marketer worker → Variables\n"
            "2. Добавь переменную:\n"
            "   `VK_AUDIENCE_SEGMENT_IDS=80507749`\n"
            "3. Сохрани и дождись передеплоя (~2 мин)\n"
            "4. Вернись и снова `/launch_test`",
            parse_mode="Markdown",
        )
        return

    context.user_data["awaiting_smoke_test_photo"] = True

    await update.message.reply_text(
        f"🧪 *Smoke Test готов к запуску*\n\n"
        f"Параметры:\n"
        f"— Сегмент: `{', '.join(str(s) for s in segments)}` (горячие православные из TargetHunter)\n"
        f"— Аудитория: ж 41-58, Россия\n"
        f"— Бюджет: 500₽/день × 3 дня = 1500₽\n"
        f"— Цель: «Написать сообщение» (приём имён для поминовения)\n"
        f"— Креативы: 2 баннера (A/B на двух текстах)\n\n"
        f"*Жду от тебя ОДНУ фотографию* — лучший кандидат из тех что "
        f"присылал (атмосферный монах с чётками или похожее). Без текста "
        f"на изображении.\n\n"
        f"Просто отправь фото в этот чат. Бот сам создаст кампанию и "
        f"вернёт ID для проверки в VK Ads UI.\n\n"
        f"Если передумаешь — напиши «отмена».",
        parse_mode="Markdown",
    )


async def handle_smoke_test_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Обработчик фото для smoke test. Вызывается из handle_photo если
    выставлен флаг.

    Returns:
        True если обработали (handle_photo не должен дальше обрабатывать),
        False если флага нет (обычный photo handler работает как раньше).
    """
    if not context.user_data.get("awaiting_smoke_test_photo"):
        return False

    context.user_data["awaiting_smoke_test_photo"] = False

    # Скачиваем фото
    photo = update.message.photo[-1]  # самое большое разрешение
    try:
        tg_file = await photo.get_file()
        image_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        logger.exception("Failed to download smoke test photo")
        await update.message.reply_text(
            f"❌ Не смог скачать фото: `{type(e).__name__}: {e}`. "
            f"Попробуй ещё раз `/launch_test`.",
            parse_mode="Markdown",
        )
        return True

    await update.message.reply_text(
        f"📥 Получил фото ({len(image_bytes) // 1024} KB). "
        f"Создаю кампанию в VK Ads... (1-2 минуты)"
    )

    # Создаём кампанию
    try:
        from src.services.ad_creator import AdCopy, AdCreator
        from src.vk_ads.client import VKAdsClient

        ads_client = VKAdsClient.from_settings()
        if ads_client is None:
            await update.message.reply_text(
                "❌ VK Ads клиент не настроен — проверь OAuth env в Railway."
            )
            return True

        ad_creator = AdCreator(ads_client)

        copies = [
            AdCopy(
                title=SMOKE_TEST_COPY_A_TITLE,
                text=SMOKE_TEST_COPY_A_TEXT,
                about=SMOKE_TEST_COPY_A_ABOUT,
                cta=SMOKE_TEST_COPY_A_CTA,
            ),
            AdCopy(
                title=SMOKE_TEST_COPY_B_TITLE,
                text=SMOKE_TEST_COPY_B_TEXT,
                about=SMOKE_TEST_COPY_B_ABOUT,
                cta=SMOKE_TEST_COPY_B_CTA,
            ),
        ]

        result = await ad_creator.create_smoke_test_campaign(
            image_bytes=image_bytes,
            copies=copies,
            community_url="https://vk.com/pomolimsy",
            daily_budget_rub=500,
            days_duration=3,
            age_min=41,
            age_max=58,
        )
    except Exception as e:
        logger.exception("Smoke test campaign creation failed")
        await update.message.reply_text(
            f"❌ Не смог создать кампанию: `{type(e).__name__}: {e}`\n\n"
            f"Скорее всего одно из:\n"
            f"— Истёк OAuth токен VK Ads (проверь Railway env)\n"
            f"— Бюджет (1500₽) превышает баланс кабинета\n"
            f"— Сегмент 80507749 ещё не в статусе ready (подожди 10-15 мин)\n"
            f"— Ошибка валидации payload (нужны логи Railway)\n\n"
            f"Попробуй ещё раз через несколько минут.",
            parse_mode="Markdown",
        )
        return True

    # Успех
    ad_plan_id = result.ad_plan_id
    banner_count = len(result.banner_ids)
    await update.message.reply_text(
        f"✅ *Smoke test кампания создана!*\n\n"
        f"🆔 Кампания: `{ad_plan_id}`\n"
        f"📊 Баннеров создано: {banner_count} (A/B)\n"
        f"💰 Бюджет: 1500₽ (500₽/день × 3 дня)\n"
        f"🎯 Сегмент: 80507749 (619k горячих)\n\n"
        f"*Что дальше:*\n"
        f"1. Открой VK Ads на телефоне → раздел «Кампании»\n"
        f"2. Найди новую кампанию (по имени `smoke_test ...`)\n"
        f"3. Проверь что выглядит корректно (тексты, фото, аудитория)\n"
        f"4. Дождись модерации (~4 часа)\n"
        f"5. Если кампания в статусе «Активна» — она крутится\n"
        f"6. Если отклонена — пришли скрин причины, разберём\n\n"
        f"Через 3 дня — Алина сделает срез метрик через `/boba`.",
        parse_mode="Markdown",
    )

    logger.info(
        f"Smoke test создан: ad_plan_id={ad_plan_id}, "
        f"banners={banner_count}, segment=80507749"
    )
    return True


# ============================================================
# Phase 5.10 (16.05.2026) — команда /audience_from_list
# ============================================================
#
# Найдена архитектурная особенность VK Ads:
# RemarketingUsersList ≠ Segment (Аудитория). В targetings кампании
# нужен ID Segment, не users_list_id. Это была причина unallowed_value
# при первом запуске smoke test.
#
# /audience_from_list <users_list_id> [название] — создаёт Segment
# с привязкой к существующему users_list. Возвращает segment_id для
# использования в VK_AUDIENCE_SEGMENT_IDS.
#
# Для НОВЫХ загрузок (через /upload_audience) — сегмент создаётся
# автоматически после users_list (см. Phase 5.10b).


async def audience_from_list_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Создаёт Segment (Аудиторию) из существующего RemarketingUsersList.

    Использование:
        /audience_from_list <users_list_id> [название]

    Пример:
        /audience_from_list 80507749 TargetHunter православные горячие

    Если название не указано — сгенерируется из ID и даты.
    """
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "📖 *Использование:*\n"
            "`/audience_from_list <users_list_id> [название]`\n\n"
            "Например: `/audience_from_list 80507749 TargetHunter горячие`\n\n"
            "Команда создаёт Аудиторию (Segment) на основе существующего "
            "Списка пользователей. Возвращает `segment_id`, который нужно "
            "прописать в Railway env `VK_AUDIENCE_SEGMENT_IDS` для "
            "использования в кампаниях.",
            parse_mode="Markdown",
        )
        return

    # Парсим users_list_id
    try:
        users_list_id = int(args[0])
    except ValueError:
        await update.message.reply_text(
            f"❌ Первый аргумент должен быть числом (users_list_id), "
            f"передано: `{args[0]}`",
            parse_mode="Markdown",
        )
        return

    # Название из остальных аргументов или auto
    if len(args) >= 2:
        segment_name = " ".join(args[1:])
    else:
        from datetime import datetime
        segment_name = (
            f"Audience from list {users_list_id} "
            f"({datetime.now().strftime('%Y-%m-%d %H:%M')})"
        )

    await update.message.reply_text(
        f"🔄 Создаю Аудиторию из Списка пользователей `{users_list_id}`...\n"
        f"Название: `{segment_name}`",
        parse_mode="Markdown",
    )

    # Создаём segment
    try:
        from src.vk_ads.client import VKAdsClient

        ads_client = VKAdsClient.from_settings()
        if ads_client is None:
            await update.message.reply_text(
                "❌ VK Ads клиент не настроен — проверь OAuth env в Railway."
            )
            return

        result = await ads_client.create_audience_segment_from_users_list(
            name=segment_name,
            users_list_id=users_list_id,
        )
    except Exception as e:
        logger.exception("create_audience_segment_from_users_list failed")
        await update.message.reply_text(
            f"❌ Не смог создать аудиторию: `{type(e).__name__}: {e}`\n\n"
            f"Возможные причины:\n"
            f"— users_list_id `{users_list_id}` не существует в кабинете\n"
            f"— Истёк OAuth токен VK Ads\n"
            f"— Сегмент с таким же именем уже есть (попробуй другое название)\n"
            f"— API v2 не подходит — нужно v3 (пришли ошибку, доработаю)",
            parse_mode="Markdown",
        )
        return

    segment_id = result.get("id")

    await update.message.reply_text(
        f"✅ *Аудитория создана!*\n\n"
        f"🆔 Segment ID: `{segment_id}`\n"
        f"📝 Название: {result.get('name')}\n"
        f"🔗 Источник: users_list `{users_list_id}` (619k ID если это наш TargetHunter)\n\n"
        f"*Что дальше:*\n"
        f"1. Открой Railway → Variables\n"
        f"2. Замени значение на: `VK_AUDIENCE_SEGMENT_IDS={segment_id}` "
        f"(сейчас там users_list_id, нужен segment_id)\n"
        f"3. Сохрани → подожди передеплой (~2 мин)\n"
        f"4. Запусти `/launch_test` снова — должно пройти",
        parse_mode="Markdown",
    )

    logger.info(
        f"audience_from_list: создан Segment id={segment_id}, "
        f"users_list={users_list_id}, name={segment_name}"
    )


# ============================================================
# Phase 5.11 (17.05.2026) — команда /launch_batch
# ============================================================
#
# Параллельный калибровочный запуск множества кампаний.
#
# По плану Vizit'a + Бобы:
# - 3 возраста (41-46, 47-52, 53-58)
# - 4 текста на возраст (разные жизненные ситуации/боли)
# - 3 фото (одно на возраст)
# - 12 кампаний × 500₽/день × 2 дня = 12 000₽ total
# - Через 24 часа: метрики, отключение слабых, удвоение бюджета сильным
#
# Workflow:
# 1. /launch_batch → бот ставит флаг ожидания 3 фото
# 2. Vizit присылает фото 1 (для 41-46)
# 3. Бот: "получил, жду фото для 47-52"
# 4. Vizit присылает фото 3 (для 47-52)
# 5. Бот: "получил, жду фото для 53-58"
# 6. Vizit присылает фото 5 (для 53-58)
# 7. Бот запускает create_batch_campaigns с 12 текстами + 3 фото
# 8. Возвращает список ad_plan_id

# 12 текстов для калибровочного запуска (одобрены Vizit'ом 17.05.2026)
# Разные жизненные ситуации, без шаблонов, "не убеждать что православие
# существует — сразу к боли"
BATCH_COPIES_41_46 = [
    {
        "title": "Молитва за ребёнка",
        "text": "Когда тревожно за ребёнка, нет ничего сильнее молитвы. Напишите имя — добавим в молитвенное правило.",
        "about": "Молитвенное сообщество. Напишите имя ребёнка.",
    },
    {
        "title": "О здравии родителей",
        "text": "Родители стареют, а мы всё заняты. Простое дело — напишите их имена. Помолимся о здравии.",
        "about": "Молитвенное сообщество. О здравии родителей.",
    },
    {
        "title": "Молитва за семью",
        "text": "Сильная женщина держит семью. Молитвенная помощь — не лишняя. Напишите имена близких.",
        "about": "Молитвенное сообщество. Молитва за семью.",
    },
    {
        "title": "Имя. Просьба. Молитва.",
        "text": "Имя. Просьба. Молитва. Напишите имя того, за кого болит сердце.",
        "about": "Молитвенное сообщество. Напишите имена.",
    },
]

BATCH_COPIES_47_52 = [
    {
        "title": "За тех кто на пороге",
        "text": "Дети растут — экзамены, выбор пути, отношения. Молитесь о них спокойно — напишите имя, мы поддержим.",
        "about": "Молитвенное сообщество. Молитва о детях.",
    },
    {
        "title": "Молитва о больном",
        "text": "Когда близкий болеет, нужна молитва. Напишите имя того, за кого болит душа. Помолимся вместе.",
        "about": "Молитвенное сообщество. О здравии болящего.",
    },
    {
        "title": "О помощи в труде",
        "text": "Когда силы на исходе — а решения нужно принимать каждый день. Напишите имя — помолимся о труде и разуме.",
        "about": "Молитвенное сообщество. О помощи в труде.",
    },
    {
        "title": "Молитва общины",
        "text": "В нашей общине пишут имена. Больных. Страдающих. Ушедших. Каждое имя становится молитвой. Присоединяйтесь.",
        "about": "Молитвенное сообщество. Молимся за каждого.",
    },
]

BATCH_COPIES_53_58 = [
    {
        "title": "Помяните дорогих ушедших",
        "text": "Они ушли — но остались в сердце. И в молитве. Напишите имена ушедших близких — помянем.",
        "about": "Молитвенное сообщество. Об упокоении.",
    },
    {
        "title": "О ваших внуках",
        "text": "Внуки — наша радость и забота. Напишите имена — помолимся об их здравии, мире в семьях, разуме.",
        "about": "Молитвенное сообщество. Молитва о внуках.",
    },
    {
        "title": "Когда тяжело",
        "text": "Бывают дни, когда сил нет, а слова не выходят. Просто напишите имя — мы помолимся за вас.",
        "about": "Молитвенное сообщество. Поддержка молитвой.",
    },
    {
        "title": "Имя — это память",
        "text": "Имя — это память. Имя — это молитва. Напишите имена тех, о ком болит душа.",
        "about": "Молитвенное сообщество. Память и молитва.",
    },
]

# Возрастные группы и тексты — соответствие
BATCH_AGE_TO_COPIES = {
    (41, 46): BATCH_COPIES_41_46,
    (47, 52): BATCH_COPIES_47_52,
    (53, 58): BATCH_COPIES_53_58,
}

# Порядок в котором ждём фото
BATCH_AGE_ORDER = [(41, 46), (47, 52), (53, 58)]


async def launch_batch_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Запускает калибровочный батч из 12 кампаний (3 возраста × 4 текста).

    Параметры зашиты в коде (одобрены Vizit'ом 17.05.2026):
    - 3 возраста: 41-46, 47-52, 53-58
    - 4 текста на возраст (разные жизненные ситуации)
    - 1 фото на возраст (Vizit прислает 3 фото последовательно)
    - 500₽/день × 2 дня = 1000₽ на кампанию
    - Total: 12 × 1000 = 12 000₽
    """
    from src.config import settings
    segments = settings.vk_audience_segment_ids_parsed
    if not segments:
        await update.message.reply_text(
            "❌ В Railway env не задан `VK_AUDIENCE_SEGMENT_IDS`.",
            parse_mode="Markdown",
        )
        return

    # Стартуем pipeline: жду первое фото (для 41-46)
    context.user_data["batch_mode"] = True
    context.user_data["batch_photos"] = {}  # {(age_min, age_max): bytes}
    context.user_data["batch_current_age_idx"] = 0  # индекс в BATCH_AGE_ORDER

    first_age = BATCH_AGE_ORDER[0]

    await update.message.reply_text(
        f"🎬 *Калибровочный батч готов к запуску*\n\n"
        f"Параметры (захардкожены):\n"
        f"— **3 возраста:** 41-46, 47-52, 53-58\n"
        f"— **4 текста** на каждый возраст (разные ситуации/боли)\n"
        f"— **12 кампаний** = 3 × 4\n"
        f"— **Бюджет:** 500₽/день × 2 дня = 1000₽ на кампанию\n"
        f"— **Всего:** 12 000₽ за 2 дня\n"
        f"— **Сегмент:** {segments[0]}\n"
        f"— **Цель:** «Написать сообщение» (package 3127)\n\n"
        f"*Принимаю 3 фото последовательно* — по одному на каждый возраст.\n\n"
        f"📷 *Шаг 1/3:* пришли фото для возраста *{first_age[0]}-{first_age[1]}* "
        f"(молодые мамы, рекомендую картинку 1 — монах с чётками).\n\n"
        f"Чтобы отменить — напиши «отмена».",
        parse_mode="Markdown",
    )


async def handle_batch_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Обработчик фото в режиме batch. Принимает фото последовательно,
    после 3-го запускает создание 12 кампаний.

    Returns:
        True если обработали (handle_photo не должен дальше обрабатывать),
        False если не в режиме batch.
    """
    if not context.user_data.get("batch_mode"):
        return False

    current_idx = context.user_data.get("batch_current_age_idx", 0)
    if current_idx >= len(BATCH_AGE_ORDER):
        # Аномалия — выходим из режима
        context.user_data["batch_mode"] = False
        return True

    current_age = BATCH_AGE_ORDER[current_idx]

    # Скачиваем фото
    photo = update.message.photo[-1]
    try:
        tg_file = await photo.get_file()
        image_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        await update.message.reply_text(
            f"❌ Не смог скачать фото: `{type(e).__name__}: {e}`. "
            f"Перезапусти `/launch_batch`.",
            parse_mode="Markdown",
        )
        context.user_data["batch_mode"] = False
        return True

    # Сохраняем фото для текущего возраста
    context.user_data["batch_photos"][current_age] = image_bytes
    next_idx = current_idx + 1
    context.user_data["batch_current_age_idx"] = next_idx

    # Если ещё есть возрасты — ждём следующего фото
    if next_idx < len(BATCH_AGE_ORDER):
        next_age = BATCH_AGE_ORDER[next_idx]
        age_hint = {
            (47, 52): "(зрелые, рекомендую картинку 3 — руки пишут в книге)",
            (53, 58): "(старшие, рекомендую картинку 5 — монах пишет в большой книге)",
        }.get(next_age, "")
        await update.message.reply_text(
            f"✅ Фото для *{current_age[0]}-{current_age[1]}* принято "
            f"({len(image_bytes) // 1024} KB).\n\n"
            f"📷 *Шаг {next_idx + 1}/3:* теперь фото для возраста "
            f"*{next_age[0]}-{next_age[1]}* {age_hint}.",
            parse_mode="Markdown",
        )
        return True

    # Получили все 3 фото — запускаем батч
    await update.message.reply_text(
        f"✅ Все 3 фото получены. Запускаю 12 кампаний... "
        f"(~5-10 минут, по 30-60 сек на каждую кампанию)"
    )

    try:
        from src.services.ad_creator import AdCopy, AdCreator
        from src.vk_ads.client import VKAdsClient

        ads_client = VKAdsClient.from_settings()
        if ads_client is None:
            await update.message.reply_text(
                "❌ VK Ads клиент не настроен."
            )
            context.user_data["batch_mode"] = False
            return True

        ad_creator = AdCreator(ads_client)

        # Готовим copies_by_age
        copies_by_age = {}
        for age_key, copy_dicts in BATCH_AGE_TO_COPIES.items():
            copies_by_age[age_key] = [
                AdCopy(
                    title=d["title"],
                    text=d["text"],
                    about=d["about"],
                    cta="write",  # для package 3127
                )
                for d in copy_dicts
            ]

        results = await ad_creator.create_batch_campaigns(
            images_by_age=context.user_data["batch_photos"],
            copies_by_age=copies_by_age,
            community_url="https://vk.com/pomolimsy",
            daily_budget_rub_per_campaign=500,
            days_duration=2,
        )
    except Exception as e:
        logger.exception("Batch campaigns creation failed")
        await update.message.reply_text(
            f"❌ Не смог создать батч: `{type(e).__name__}: {e}`",
            parse_mode="Markdown",
        )
        context.user_data["batch_mode"] = False
        return True

    # Финальный отчёт
    ids_summary = "\n".join(
        f"   • Кампания #{i+1}: `{r.ad_plan_id}`"
        for i, r in enumerate(results)
    )
    await update.message.reply_text(
        f"✅ *Батч запущен!*\n\n"
        f"Создано: *{len(results)} из 12 кампаний*\n"
        f"Бюджет: 500₽/день × 2 дня на каждую = ~{len(results) * 1000}₽ total\n\n"
        f"*ID кампаний:*\n{ids_summary}\n\n"
        f"*Что дальше:*\n"
        f"1. Все кампании на модерации (4 часа обычно)\n"
        f"2. Через 24 часа после запуска — `/boba стат батча` для метрик\n"
        f"3. Слабые отключаем, на сильные удваиваем бюджет\n\n"
        f"_В кабинете VK Ads → Кампании → фильтр по имени `batch` ._",
        parse_mode="Markdown",
    )

    context.user_data["batch_mode"] = False
    context.user_data.pop("batch_photos", None)
    context.user_data.pop("batch_current_age_idx", None)

    logger.info(
        f"launch_batch: создано {len(results)}/12 кампаний, "
        f"ids={[r.ad_plan_id for r in results]}"
    )
    return True


# ============================================================
# Phase 5.17 (17.05.2026) — команда /launch_20
# ============================================================
#
# Масштабирующий запуск после успеха «Молитвы за семью» (CPL 78₽).
# - 4 женских возраста (36-40, 41-46, 47-52, 53-58) — шаг 5 лет
# - 5 общих текстов на каждый возраст (без триггеров боли/смерти)
# - 4 фото (по одному на возраст)
# - 20 кампаний × 500₽/день × 1 день = 10 000₽ на тест
# - Заголовки не содержат названия группы (Vizit пересмотрел требование 17.05)

LAUNCH_20_COPIES = [
    {
        "title": "Молитва за семью",
        "text": "Сильная женщина держит семью. Молитвенная помощь — не лишняя. Напишите имена близких — добавим в общую молитву.",
        "about": "Молитвенное сообщество. Молитва за семью.",
    },
    {
        "title": "Молитва о вашем ребёнке",
        "text": "Когда тревожно за ребёнка, нет ничего сильнее молитвы. Напишите имя — добавим в молитвенное правило.",
        "about": "Молитвенное сообщество. Молитва о детях.",
    },
    {
        "title": "Молитва матери самая сильная",
        "text": "Материнская молитва со дна моря достанет. Напишите имя того, о ком душа болит — помолимся вместе.",
        "about": "Молитвенное сообщество. Сила материнской молитвы.",
    },
    {
        "title": "Помолитесь о ваших близких",
        "text": "Родители, дети, муж — те кого любим больше всех. Напишите имена близких — добавим в общую молитву.",
        "about": "Молитвенное сообщество. Молитва о близких.",
    },
    {
        "title": "Молитва о вашем доме и семье",
        "text": "Дом крепок молитвой. О мире в семье, о здравии всех живущих. Напишите имена — помолимся.",
        "about": "Молитвенное сообщество. Молитва о доме.",
    },
]

# 4 женских возраста, шаг 5 лет — точно измерим где лучше отзываются
LAUNCH_20_AGE_ORDER = [(36, 40), (41, 46), (47, 52), (53, 58)]

# Подсказки какое фото лучше под каждый возраст (выбраны из 10 присланных
# 17.05.2026, отметили ⭐⭐⭐ как топ для родительской темы)
LAUNCH_20_PHOTO_HINTS = {
    (36, 40): "молодые матери — IMG_5699 или IMG_5716 (мать с младенцем у иконы)",
    (41, 46): "победный сегмент — IMG_5709 (мать с младенцем у Богородицы с младенцем, ⭐⭐⭐)",
    (47, 52): "зрелые — IMG_5704 (Владимирская икона, ⭐⭐⭐) или IMG_5711",
    (53, 58): "бабушки — IMG_5705 (Утоли моя печали) или IMG_5710 (благословение ребёнка)",
}


async def launch_20_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Запускает 20 кампаний (4 возраста × 5 текстов) для масштабирования
    после «Молитвы за семью».

    Параметры (одобрены Vizit'ом 17.05.2026):
    - 4 женских возраста: 36-40, 41-46, 47-52, 53-58
    - 5 текстов на возраст (одни и те же — варьируется возраст, не текст)
    - 4 фото — Vizit присылает последовательно, одно на возраст
    - 500₽/день × 1 день = 500₽ на кампанию
    - Total: 20 × 500 = 10 000₽
    """
    from src.config import settings
    segments = settings.vk_audience_segment_ids_parsed
    if not segments:
        await update.message.reply_text(
            "❌ В Railway env не задан `VK_AUDIENCE_SEGMENT_IDS`.",
            parse_mode="Markdown",
        )
        return

    # Включаем launch_20 режим — handle_launch_20_photo подхватит следующие фото
    context.user_data["launch_20_mode"] = True
    context.user_data["launch_20_photos"] = {}
    context.user_data["launch_20_current_age_idx"] = 0

    first_age = LAUNCH_20_AGE_ORDER[0]
    first_hint = LAUNCH_20_PHOTO_HINTS[first_age]

    await update.message.reply_text(
        f"🚀 *Масштабирующий запуск 20 кампаний*\n\n"
        f"Параметры:\n"
        f"— **4 возраста (Ж):** 36-40, 41-46, 47-52, 53-58\n"
        f"— **5 текстов** на каждый возраст (общие)\n"
        f"— **20 кампаний** = 4 × 5\n"
        f"— **Бюджет:** 500₽/день × 1 день = 500₽ на кампанию\n"
        f"— **Всего:** 10 000₽ за день\n"
        f"— **Сегмент:** {segments[0]}\n"
        f"— **Цель:** «Написать сообщение» (package 3127)\n\n"
        f"*Принимаю 4 фото последовательно* — по одному на каждый возраст.\n\n"
        f"📷 *Шаг 1/4:* пришли фото для возраста *{first_age[0]}-{first_age[1]}*\n"
        f"_{first_hint}_\n\n"
        f"Чтобы отменить — напиши «отмена».",
        parse_mode="Markdown",
    )


async def handle_launch_20_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Обработчик фото в режиме launch_20. Принимает 4 фото последовательно,
    после 4-го запускает создание 20 кампаний.

    Returns:
        True если обработали (handle_photo не должен дальше обрабатывать),
        False если не в режиме launch_20.
    """
    if not context.user_data.get("launch_20_mode"):
        return False

    current_idx = context.user_data.get("launch_20_current_age_idx", 0)
    if current_idx >= len(LAUNCH_20_AGE_ORDER):
        context.user_data["launch_20_mode"] = False
        return True

    current_age = LAUNCH_20_AGE_ORDER[current_idx]

    # Скачиваем фото
    photo = update.message.photo[-1]
    try:
        tg_file = await photo.get_file()
        image_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        await update.message.reply_text(
            f"❌ Не смог скачать фото: `{type(e).__name__}: {e}`. "
            f"Перезапусти `/launch_20`.",
            parse_mode="Markdown",
        )
        context.user_data["launch_20_mode"] = False
        return True

    # Сохраняем фото для текущего возраста
    context.user_data["launch_20_photos"][current_age] = image_bytes
    next_idx = current_idx + 1
    context.user_data["launch_20_current_age_idx"] = next_idx

    # Если ещё есть возрасты — ждём следующего фото
    if next_idx < len(LAUNCH_20_AGE_ORDER):
        next_age = LAUNCH_20_AGE_ORDER[next_idx]
        next_hint = LAUNCH_20_PHOTO_HINTS[next_age]
        await update.message.reply_text(
            f"✅ Фото для {current_age[0]}-{current_age[1]} принято "
            f"({len(image_bytes)} байт).\n\n"
            f"📷 *Шаг {next_idx + 1}/4:* пришли фото для возраста "
            f"*{next_age[0]}-{next_age[1]}*\n"
            f"_{next_hint}_",
            parse_mode="Markdown",
        )
        return True

    # Все 4 фото собраны — запускаем создание
    await update.message.reply_text(
        f"✅ Все 4 фото собраны. Создаю 20 кампаний — это займёт минуту-две.\n"
        f"Не отправляй ничего, пока я не отвечу с результатом.",
        parse_mode="Markdown",
    )

    try:
        from src.vk_ads.client import VKAdsClient

        ads_client = VKAdsClient.from_settings()
        if ads_client is None:
            await update.message.reply_text(
                "❌ VK Ads клиент не настроен."
            )
            context.user_data["launch_20_mode"] = False
            return True

        ad_creator = AdCreator(ads_client)

        # Готовим copies_by_age — одни и те же 5 текстов для всех 4 возрастов
        ad_copies = [
            AdCopy(
                title=d["title"],
                text=d["text"],
                about=d["about"],
                cta="write",
            )
            for d in LAUNCH_20_COPIES
        ]
        copies_by_age = {age: ad_copies for age in LAUNCH_20_AGE_ORDER}

        results = await ad_creator.create_batch_campaigns(
            images_by_age=context.user_data["launch_20_photos"],
            copies_by_age=copies_by_age,
            community_url="https://vk.com/pomolimsy",
            daily_budget_rub_per_campaign=500,
            days_duration=1,
        )
    except Exception as e:
        logger.exception("launch_20 campaigns creation failed")
        await update.message.reply_text(
            f"❌ Не смог создать 20 кампаний: `{type(e).__name__}: {e}`",
            parse_mode="Markdown",
        )
        context.user_data["launch_20_mode"] = False
        return True

    # Финальный отчёт — компактный
    ids_summary = ", ".join(f"`{r.ad_plan_id}`" for r in results[:20])
    await update.message.reply_text(
        f"✅ *20 кампаний запущены!*\n\n"
        f"Создано: *{len(results)} из 20 кампаний*\n"
        f"Бюджет: 500₽/день × 1 день на каждую = ~{len(results) * 500}₽ total\n\n"
        f"*ID кампаний:* {ids_summary}\n\n"
        f"*Что дальше:*\n"
        f"1. Все кампании на модерации (обычно 1-4 часа)\n"
        f"2. К утру `/watchdog` покажет CPL по каждой\n"
        f"3. Слабые `/pause`, на сильные удваиваем бюджет\n\n"
        f"_В кабинете VK Ads → Кампании → фильтр по имени `batch` ._",
        parse_mode="Markdown",
    )

    context.user_data["launch_20_mode"] = False
    context.user_data.pop("launch_20_photos", None)
    context.user_data.pop("launch_20_current_age_idx", None)

    logger.info(
        f"launch_20: создано {len(results)}/20 кампаний, "
        f"ids={[r.ad_plan_id for r in results]}"
    )
    return True


# ============================================================
# Phase 5.12 (17.05.2026) — Сторож: /pause и /kill_bad
# ============================================================
#
# Сторож (src/scheduler/jobs.py::check_metrics_and_anomalies) каждый час
# читает метрики активных кампаний, применяет правила из safety.py,
# и при пробитии порогов шлёт алерт владельцу в Telegram.
#
# Auto-pause в первой версии НЕТ — Vizit сам решает и выключает через:
# - /pause <ad_plan_id>   — одну конкретную
# - /kill_bad             — все «жёлтые» (которые Сторож пометил)


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выключить кампанию по ID. Использование: /pause <ad_plan_id>"""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "📖 Использование: `/pause <ad_plan_id>`\n\nНапример: `/pause 21162137`",
            parse_mode="Markdown",
        )
        return

    try:
        ad_plan_id = int(args[0])
    except ValueError:
        await update.message.reply_text(
            f"❌ ID должен быть числом, передано: `{args[0]}`",
            parse_mode="Markdown",
        )
        return

    try:
        from src.vk_ads.client import VKAdsClient
        client = VKAdsClient.from_settings()
        if client is None:
            await update.message.reply_text("❌ VK Ads клиент не настроен.")
            return

        await client.pause_campaign(ad_plan_id)
    except Exception as e:
        logger.exception("pause_campaign failed")
        await update.message.reply_text(
            f"❌ Не смог выключить `{ad_plan_id}`: `{type(e).__name__}: {e}`",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        f"✅ Кампания `{ad_plan_id}` выключена (status=blocked).",
        parse_mode="Markdown",
    )
    logger.info(f"pause_command: ad_plan_id={ad_plan_id} выключена")


async def kill_bad_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Массовое выключение всех «жёлтых» кампаний которые Сторож пометил."""
    from src.scheduler import bad_campaigns_state

    bad_ids, last_update = bad_campaigns_state.get_bad_ids()

    if not bad_ids:
        if last_update == 0:
            await update.message.reply_text(
                "ℹ️ Сторож ещё не запускался (проверка раз в час). "
                "Подожди до следующего запуска или выключай через `/pause <id>`.",
                parse_mode="Markdown",
            )
        else:
            from datetime import datetime
            age_min = int((datetime.now().timestamp() - last_update) / 60)
            await update.message.reply_text(
                f"✅ В последней проверке Сторожа ({age_min} мин назад) "
                f"проблемных кампаний не было — все в норме."
            )
        return

    await update.message.reply_text(
        f"🔄 Выключаю {len(bad_ids)} проблемных кампаний..."
    )

    try:
        from src.vk_ads.client import VKAdsClient
        client = VKAdsClient.from_settings()
        if client is None:
            await update.message.reply_text("❌ VK Ads клиент не настроен.")
            return

        success: list[int] = []
        failed: list[tuple[int, str]] = []
        for cid in bad_ids:
            try:
                await client.pause_campaign(cid)
                success.append(cid)
            except Exception as e:
                failed.append((cid, f"{type(e).__name__}: {e}"))

        # Очищаем список — выключенные больше не «жёлтые»
        bad_campaigns_state.clear()

    except Exception as e:
        logger.exception("kill_bad failed")
        await update.message.reply_text(
            f"❌ Сбой при массовом выключении: `{type(e).__name__}: {e}`",
            parse_mode="Markdown",
        )
        return

    msg = f"✅ Выключено: *{len(success)}/{len(bad_ids)}*\n"
    if success:
        msg += "\n" + "\n".join(f"• `{cid}`" for cid in success[:10])
        if len(success) > 10:
            msg += f"\n_...и ещё {len(success) - 10}_"
    if failed:
        msg += f"\n\n❌ *Не получилось ({len(failed)}):*"
        for cid, err in failed[:5]:
            msg += f"\n• `{cid}` — {err}"

    await update.message.reply_text(msg, parse_mode="Markdown")
    logger.info(
        f"kill_bad: выключено {len(success)}/{len(bad_ids)}, провалов {len(failed)}"
    )


# ============================================================
# Phase 5.14 (17.05.2026) — команда /watchdog
# ============================================================
#
# Vizit плохо понимает статистику, хочет:
# 1. Простой язык в алертах Сторожа (без CTR/CPC)
# 2. Возможность "ткнуть" Сторожа и получить отчёт сейчас
#
# /watchdog — принудительный запуск проверки. Сторож обычно работает по
# расписанию (раз в час), но эта команда позволяет триггернуть его на
# месте + получить детальный отчёт о состоянии всех активных кампаний
# (не только проблемных).


async def watchdog_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Принудительный запуск Сторожа с детальным отчётом по всем активным
    кампаниям (не только проблемным).

    Используется когда Vizit хочет проверить «как там моя реклама прямо
    сейчас», не дожидаясь автоматического запуска через час.
    """
    from datetime import datetime
    from src.scheduler.safety import check_campaign_anomaly
    from src.scheduler import bad_campaigns_state
    from src.vk_ads.client import VKAdsClient
    from src.vk_ads.models import CampaignStats

    await update.message.reply_text(
        "🐕 Сторож проверяет кампании... (10-20 секунд)"
    )

    client = VKAdsClient.from_settings()
    if client is None:
        await update.message.reply_text(
            "❌ VK Ads клиент не настроен — не могу проверить."
        )
        return

    try:
        active_campaigns = await client.get_active_ad_plans()
    except Exception as e:
        logger.exception("watchdog: failed to get active campaigns")
        await update.message.reply_text(
            f"❌ Не смог получить список кампаний: `{type(e).__name__}: {e}`",
            parse_mode="Markdown",
        )
        return

    if not active_campaigns:
        await update.message.reply_text(
            "🐕 *Сторож докладывает:*\n\n"
            "Сейчас нет активных кампаний которые крутятся.\n\n"
            "Возможные причины:\n"
            "• Все кампании ещё на модерации (через 4 часа после запуска "
            "обычно проходят)\n"
            "• Все кампании уже завершились (бюджет потрачен)\n"
            "• Все кампании выключены вручную\n\n"
            "Как только кампании начнут крутиться — я снова заработаю. "
            "Проверь статус в VK Ads → Кампании.",
            parse_mode="Markdown",
        )
        return

    today = datetime.now().strftime("%Y-%m-%d")
    campaign_ids = [int(c["id"]) for c in active_campaigns]
    name_by_id = {int(c["id"]): c.get("name", "?") for c in active_campaigns}

    try:
        stats_response = await client.get_campaign_stats(
            campaign_ids=campaign_ids,
            date_from=today,
            date_to=today,
        )
    except Exception as e:
        logger.exception("watchdog: failed to get stats")
        await update.message.reply_text(
            f"❌ Не смог получить метрики: `{type(e).__name__}: {e}`",
            parse_mode="Markdown",
        )
        return

    # Парсим метрики и применяем правила
    alerts: list[tuple[int, str, str]] = []
    ok_campaigns: list[tuple[int, str, CampaignStats]] = []
    no_data_campaigns: list[tuple[int, str]] = []

    # ВАЖНО: используем общий хелпер из vk_ads.models, чтобы парсинг
    # ответа VK Ads Statistics API жил в одном месте. До 17.05.2026 у нас
    # тут был свой парсер который искал shows/clicks/spent на верхнем
    # уровне `total`, а реально они лежат в `rows[].base`. Из-за этого
    # сторож писал «🕐 Ещё нет данных» по всем кампаниям несмотря на то
    # что они показывались. Источник правды — docs/VK_STATISTICS_API.md.
    from src.vk_ads.models import aggregate_stats_item

    items = stats_response.get("items", []) if isinstance(stats_response, dict) else []
    stats_by_id: dict[int, CampaignStats] = {}
    for item in items:
        stats = aggregate_stats_item(item)
        if stats is not None:
            stats_by_id[stats.campaign_id] = stats

    for cid in campaign_ids:
        name = name_by_id.get(cid, "?")
        stats = stats_by_id.get(cid)
        if stats is None or stats.impressions == 0:
            no_data_campaigns.append((cid, name))
            continue

        decision = check_campaign_anomaly(stats)
        if decision.action == "alert":
            alerts.append((cid, name, decision.reason))
        else:
            ok_campaigns.append((cid, name, stats))

    # Сохраняем состояние плохих
    bad_campaigns_state.set_bad_ids([a[0] for a in alerts])

    # Формируем отчёт
    lines = [f"🐕 *Сторож докладывает (всего {len(campaign_ids)} кампаний):*\n"]

    if alerts:
        lines.append(f"\n⚠️ *С проблемами: {len(alerts)}*")
        # Лимит 20 поднят с 10 (Phase 5.16, 17.05.2026) — при батче из 12
        # кампаний прошлый лимит обрезал хвост и пользователь не видел
        # последние 2 алерта. 20 — с запасом, Telegram-сообщение влезает.
        for cid, name, reason in alerts[:20]:
            lines.append(f"\n`{name}` (`{cid}`)")
            lines.append(reason)
        if len(alerts) > 20:
            lines.append(f"\n_...и ещё {len(alerts) - 20} с проблемами_")

    if ok_campaigns:
        lines.append(f"\n\n✅ *В норме: {len(ok_campaigns)}*")
        for cid, name, stats in ok_campaigns[:20]:
            simple_stats = _format_stats_simple(stats)
            lines.append(f"\n• `{name}` — {simple_stats}")
        if len(ok_campaigns) > 20:
            lines.append(f"\n_...и ещё {len(ok_campaigns) - 20} в норме_")

    if no_data_campaigns:
        lines.append(f"\n\n🕐 *Ещё нет данных: {len(no_data_campaigns)}*")
        lines.append(
            "_Эти кампании только запустились или модерация ещё идёт. "
            "Подожди 1-2 часа._"
        )

    if alerts:
        lines.append(
            f"\n\n*Что делать:*\n"
            f"• Выключить конкретную: `/pause <id>`\n"
            f"• Выключить все проблемные ({len(alerts)} шт): `/kill_bad`"
        )
    elif ok_campaigns:
        lines.append(
            "\n\nВсё крутится нормально. Я буду продолжать следить и "
            "пришлю сообщение если что-то пойдёт не так."
        )

    msg = "".join(lines)
    # Telegram limit — 4096 символов на сообщение
    if len(msg) > 4000:
        msg = msg[:3950] + "\n\n_(отчёт обрезан, слишком длинный)_"

    await update.message.reply_text(msg, parse_mode="Markdown")


def _format_stats_simple(stats) -> str:
    """Простой человеческий формат метрик (без CTR/CPC терминов).

    Пример: "Показали 2400 раз, кликнули 18, потрачено 250₽"
    """
    parts = []
    if stats.impressions > 0:
        parts.append(f"показали {stats.impressions:,} раз".replace(",", " "))
    if stats.clicks > 0:
        parts.append(f"кликнули {stats.clicks}")
    if stats.spent_rub > 0:
        parts.append(f"потрачено {stats.spent_rub:.0f}₽")
    if stats.leads > 0:
        parts.append(f"📬 *написали {stats.leads}!*")
    return ", ".join(parts) if parts else "нет данных"


# ============================================================
# Phase 5.17 (17.05.2026) — массовое обновление бюджета batch-кампаний
# ============================================================
#
# Vizit идёт спать на 8-9 часов, хочет снизить бюджет с 500₽ до 300₽ на
# каждую batch-кампанию чтобы меньше тратить пока он не сможет реагировать.
# Команда /set_batch_budget <сумма> — массово обновляет все активные
# кампании с именем начинающимся на 'batch'.


async def set_batch_budget_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Массово установить дневной бюджет для всех batch-кампаний.

    Использование: /set_batch_budget <сумма_в_рублях>
    Пример: /set_batch_budget 300

    Обновляет budget_limit_day на уровне ad_plan И на уровне каждого
    вложенного ad_group (необходимо в CBO режиме для синхронизации).

    Фильтрация: только кампании имя которых начинается на 'batch'
    (наши из /launch_batch). Старые тестовые и smoke_test не трогаются.
    """
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "📖 Использование: `/set_batch_budget <сумма>`\n\n"
            "Пример: `/set_batch_budget 300` — установит 300₽/день\n"
            "на все активные кампании с именем начинающимся на 'batch'.\n\n"
            "Минимум 100₽ (лимит VK Ads).",
            parse_mode="Markdown",
        )
        return

    try:
        new_budget = int(args[0])
    except ValueError:
        await update.message.reply_text(
            f"❌ Бюджет должен быть числом, передано: `{args[0]}`",
            parse_mode="Markdown",
        )
        return

    if new_budget < 100:
        await update.message.reply_text(
            "❌ Минимум 100₽/день — это лимит VK Ads. Меньше не примет."
        )
        return

    await update.message.reply_text(
        f"🔄 Меняю бюджет на {new_budget}₽/день для всех batch-кампаний... "
        f"(10-30 секунд)"
    )

    from src.vk_ads.client import VKAdsClient
    client = VKAdsClient.from_settings()
    if client is None:
        await update.message.reply_text("❌ VK Ads клиент не настроен.")
        return

    try:
        active = await client.get_active_ad_plans()
    except Exception as e:
        logger.exception("set_batch_budget: failed to get active")
        await update.message.reply_text(
            f"❌ Не смог получить список: `{type(e).__name__}: {e}`",
            parse_mode="Markdown",
        )
        return

    # Фильтруем только batch-кампании
    batch_campaigns = [
        c for c in active
        if c.get("name", "").startswith("batch")
    ]

    if not batch_campaigns:
        await update.message.reply_text(
            "ℹ️ Активных batch-кампаний не нашёл. "
            "Возможно ещё на модерации или имена другие."
        )
        return

    success_plans = 0
    success_groups = 0
    failed: list[tuple[int, str]] = []

    for camp in batch_campaigns:
        cid = int(camp["id"])
        name = camp.get("name", "?")[:40]

        # 1) Обновляем ad_plan
        try:
            await client.update_ad_plan_budget(cid, new_budget)
            success_plans += 1
        except Exception as e:
            failed.append((cid, f"ad_plan: {type(e).__name__}"))
            continue

        # 2) Обновляем все ad_groups внутри
        try:
            groups = await client.get_ad_groups_for_campaign(cid)
            for g in groups:
                gid = int(g["id"])
                try:
                    await client.update_ad_group_budget(gid, new_budget)
                    success_groups += 1
                except Exception as e:
                    failed.append(
                        (gid, f"ad_group: {type(e).__name__}")
                    )
        except Exception as e:
            failed.append((cid, f"get_groups: {type(e).__name__}"))

    msg = (
        f"✅ *Бюджет обновлён на {new_budget}₽/день*\n\n"
        f"Кампаний обновлено: {success_plans}/{len(batch_campaigns)}\n"
        f"Групп внутри обновлено: {success_groups}\n"
    )
    if failed:
        msg += f"\n⚠️ Ошибки ({len(failed)}):"
        for fid, err in failed[:5]:
            msg += f"\n• `{fid}` — {err}"
        if len(failed) > 5:
            msg += f"\n• ...и ещё {len(failed) - 5}"

    msg += (
        f"\n\n_В CBO режиме VK сам распределит бюджет между группами. "
        f"При 12 кампаниях × {new_budget}₽/день общий поток = "
        f"{12 * new_budget}₽/день._"
    )

    await update.message.reply_text(msg, parse_mode="Markdown")
    logger.info(
        f"set_batch_budget: установил {new_budget}₽/день для "
        f"{success_plans} кампаний, {success_groups} групп, "
        f"провалов {len(failed)}"
    )


async def pause_non_batch_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Выключить все активные кампании кроме наших batch.

    Чистка после отладочных попыток за день. Оставляет только кампании
    имя которых начинается с 'batch' (из /launch_batch). Всё остальное
    (smoke_test, старые тестовые) выключает в статус 'blocked'.
    """
    from src.vk_ads.client import VKAdsClient
    client = VKAdsClient.from_settings()
    if client is None:
        await update.message.reply_text("❌ VK Ads клиент не настроен.")
        return

    try:
        active = await client.get_active_ad_plans()
    except Exception as e:
        await update.message.reply_text(
            f"❌ Не смог получить список: `{type(e).__name__}: {e}`",
            parse_mode="Markdown",
        )
        return

    non_batch = [
        c for c in active
        if not c.get("name", "").startswith("batch")
    ]

    if not non_batch:
        await update.message.reply_text(
            "✅ Активных НЕ-batch кампаний нет, всё уже чисто."
        )
        return

    await update.message.reply_text(
        f"🔄 Выключаю {len(non_batch)} старых кампаний (не batch)... "
        f"(5-15 секунд)"
    )

    success: list[tuple[int, str]] = []
    failed: list[tuple[int, str]] = []

    for camp in non_batch:
        cid = int(camp["id"])
        name = camp.get("name", "?")[:30]
        try:
            await client.pause_campaign(cid)
            success.append((cid, name))
        except Exception as e:
            failed.append((cid, f"{type(e).__name__}: {e}"))

    msg = f"✅ Выключено: *{len(success)}* старых кампаний\n"
    if success:
        msg += "\n" + "\n".join(f"• `{cid}` {name}" for cid, name in success[:10])
        if len(success) > 10:
            msg += f"\n• ...и ещё {len(success) - 10}"

    if failed:
        msg += f"\n\n❌ Не получилось ({len(failed)}):"
        for fid, err in failed[:3]:
            msg += f"\n• `{fid}` — {err}"

    msg += "\n\n_Теперь активны только 12 batch-кампаний. Спокойной ночи._"

    await update.message.reply_text(msg, parse_mode="Markdown")
