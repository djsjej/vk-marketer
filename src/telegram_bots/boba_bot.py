"""Отдельный Telegram-бот Боба — стратег команды VK Marketing.

Phase 5.15 (17.05.2026): начало multi-bot архитектуры. Каждый агент
команды получает свой бот через @BotFather. Боба — первый.

В отличие от команды /boba в главном боте (которая работает в одном
чате с Vizit'ом и Claude-главным), этот бот:
- Имеет своё имя (@BobaStrategVKBot) и аватарку
- Vizit может писать ему лично или добавить в группу
- Хранит свою историю разговора отдельно от главного бота
- Получает контекст текущего проекта (активные кампании, алерты Сторожа)
  через системный промпт каждого запроса

Архитектура запускается из run_multi_bots.py — оба Application
работают параллельно в одном процессе через asyncio.gather.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.agents import BOBA_PERSONA
from src.agents.dialog import DialogAgent
from src.config import settings

logger = logging.getLogger(__name__)


# Стартовое приветствие в стиле Бобы — без "Здравствуйте, я ИИ-ассистент"
BOBA_GREETING = (
    "Слушай, я Боба. CMO команды по VK-маркетингу твоего проекта.\n\n"
    "За 13 лет в digital видел и хорошее, и провальное. Сейчас разбираюсь "
    "в твоей теме с молитвенным сообществом — нетривиальная ниша.\n\n"
    "Что могу делать:\n"
    "— обсуждать стратегию, креативы, аудитории\n"
    "— разбирать метрики кампаний после запуска\n"
    "— ставить задачи Кириллу/Тимуру/Рите/Алине (когда они появятся)\n"
    "— называть вещи своими именами без воды\n\n"
    "Пиши что у тебя сейчас на уме. Я в курсе текущих 12 кампаний "
    "из batch-запуска, они на модерации."
)


async def boba_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start у бота Бобы — представление."""
    # Чистим историю при /start
    context.user_data["boba_history"] = []
    await update.message.reply_text(BOBA_GREETING)


async def boba_handle_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Любое текстовое сообщение → Боба отвечает через Anthropic API.

    История диалога хранится в context.user_data['boba_history'].
    Контекст проекта (активные кампании, последние алерты) добавляется
    к каждому сообщению автоматически — Боба всегда знает текущее состояние.
    """
    user_message = update.message.text
    if not user_message:
        return

    # Игнорируем сообщения не от владельца (если бот вдруг в публичной группе)
    if update.effective_user.id != settings.telegram_owner_id:
        logger.warning(
            f"Boba bot: чужой пользователь {update.effective_user.id} пишет, игнорируем"
        )
        return

    history = context.user_data.get("boba_history", [])

    # Контекст проекта добавляется к сообщению пользователя
    # (не к persona, чтобы агент знал что это про здесь и сейчас)
    project_context = await _build_project_context()
    enriched_message = (
        f"{project_context}\n\n---\n\nВопрос/сообщение от Vizit'a:\n{user_message}"
    )

    # «Печатает...» индикатор
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    try:
        agent = DialogAgent(persona=BOBA_PERSONA)
        response = await agent.chat(
            user_message=enriched_message,
            history=history,
        )
    except Exception as e:
        logger.exception("Boba dialog failed")
        await update.message.reply_text(
            f"Anthropic API подвис: {type(e).__name__}: {e}\n\n"
            f"Попробуй ещё раз через минуту."
        )
        return

    # Сохраняем историю (без project_context — он динамический, не нужен в истории)
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": response.final_text})
    # Обрезаем до последних 20 сообщений (10 пар)
    context.user_data["boba_history"] = history[-20:]

    await update.message.reply_text(response.final_text)


async def boba_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /reset — очистка истории разговора с Бобой."""
    context.user_data["boba_history"] = []
    await update.message.reply_text(
        "История разговора очищена. Начинаем с чистого листа."
    )


async def _build_project_context() -> str:
    """Собирает текущее состояние проекта для передачи Бобе в каждом запросе.

    Боба должен ВСЕГДА знать:
    - Сколько сейчас активных кампаний
    - Какие из них в проблемах (от Сторожа)
    - Последние данные о метриках (если есть)

    Без этого Боба отвечает абстрактно. С этим — он реальный консультант
    с актуальными данными.
    """
    parts = ["[ТЕКУЩИЙ КОНТЕКСТ ПРОЕКТА — это для тебя, не цитируй дословно]"]

    # Активные кампании
    try:
        from src.vk_ads.client import VKAdsClient

        client = VKAdsClient.from_settings()
        if client is not None:
            active = await client.get_active_ad_plans()
            parts.append(f"Активных кампаний в VK Ads: {len(active)}")
            if active:
                # Топ-5 имён
                names = [c.get("name", "?") for c in active[:5]]
                parts.append(f"Примеры имён: {', '.join(names)}")
    except Exception as e:
        logger.warning(f"Boba context: VK Ads недоступен: {e}")
        parts.append("VK Ads клиент не отвечает (могут быть проблемы с OAuth)")

    # Проблемные кампании от Сторожа
    try:
        from src.scheduler import bad_campaigns_state

        bad_ids, last_update = bad_campaigns_state.get_bad_ids()
        if bad_ids:
            parts.append(
                f"Сторож пометил {len(bad_ids)} кампаний с проблемами: "
                f"{', '.join(str(i) for i in bad_ids[:10])}"
            )
        elif last_update > 0:
            from datetime import datetime
            age_min = int((datetime.now().timestamp() - last_update) / 60)
            parts.append(f"Сторож проверял {age_min} мин назад — проблем нет")
        else:
            parts.append("Сторож пока не запускался в этой сессии (или нет данных)")
    except Exception:
        pass

    return "\n".join(parts)


def build_boba_application() -> Application | None:
    """Создаёт Application для бота Бобы. Возвращает None если токен не задан.

    Запускается параллельно с главным ботом из run_multi_bots.py.
    """
    if not settings.tg_bot_boba_token:
        logger.info("TG_BOT_BOBA_TOKEN не задан — Боба-бот не запускается")
        return None

    application = (
        Application.builder()
        .token(settings.tg_bot_boba_token)
        .build()
    )

    # Фильтр — только владелец может общаться с Бобой
    owner_filter = filters.User(user_id=settings.telegram_owner_id)

    application.add_handler(
        CommandHandler("start", boba_start, filters=owner_filter)
    )
    application.add_handler(
        CommandHandler("reset", boba_reset, filters=owner_filter)
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & owner_filter,
            boba_handle_message,
        )
    )

    logger.info("Боба-бот сконфигурирован, готов к запуску")
    return application
