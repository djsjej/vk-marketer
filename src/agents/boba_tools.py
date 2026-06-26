"""Инструменты которые Боба может вызывать через Anthropic Tool Use API.

Phase 7 — даём Бобе руки. Без tools он только разговаривает; с tools
он может реально узнать что в кабинете, запустить парсинг, прочитать
базу знаний.

Список инструментов (минимальный набор, по рецензии Бобы — не 20 сразу):
1. get_account_status() — баланс кабинета и активные кампании
2. run_audience_parsing(max_per_group) — запустить парсинг горячей аудитории
3. get_campaign_stats(campaign_id, days) — статистика конкретной кампании
4. read_knowledge(file_name) — прочитать раздел базы знаний
5. append_knowledge(file_name, entry) — записать заметку в базу

В будущих фазах добавятся:
- create_remarketing_list (Phase 8 — Тимур)
- delegate_to(specialist, task) (Phase 8 — призыв команды)
- search_communities (Phase 12 — после PKCE flow)
- schedule_cycle (Phase 13 — cron)

Архитектура:
- BOBA_TOOLS_SCHEMA — список tool definitions для передачи в Anthropic API
- execute_tool(name, args) — диспетчер: вызывает нужную Python функцию
- Каждая функция возвращает строку (текстовое описание результата)
  которая идёт обратно к Бобе в tool_result блоке
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Корневая папка проекта (для путей к knowledge_base)
PROJECT_ROOT = Path(__file__).parent.parent.parent
KNOWLEDGE_BASE_DIR = PROJECT_ROOT / "docs" / "knowledge_base"

# Допустимые файлы базы знаний — Боба не может писать в произвольные файлы
ALLOWED_KNOWLEDGE_FILES = {
    "working_combos",
    "failed_combos",
    "hypotheses",
    "vk_moderation_log",
    "orthodox_calendar",
    "README",
}


# ============================================================
# Tool 1: get_account_status
# ============================================================

GET_ACCOUNT_STATUS_SCHEMA = {
    "name": "get_account_status",
    "description": (
        "Получает текущий статус рекламного кабинета VK Ads: баланс, "
        "количество активных кампаний, общий статус. Используй когда "
        "Vizit спрашивает 'что у нас сейчас в кабинете' или перед "
        "планированием новой кампании чтобы понимать ресурсы."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


async def get_account_status() -> str:
    """Возвращает текстовое описание статуса кабинета."""
    try:
        from src.vk_ads.client import VKAdsClient

        ads_client = VKAdsClient.from_settings()
        if ads_client is None:
            return (
                "❌ Не могу подключиться к VK Ads — OAuth credentials не "
                "настроены в окружении. Скажи Vizit'у проверить env vars "
                "в Railway."
            )

        try:
            campaigns = await ads_client.get_campaigns()
        except Exception as e:
            campaigns = []
            logger.warning(f"get_campaigns failed: {e}")

        active_count = sum(
            1
            for c in campaigns
            if c.get("status") in {"active", "running", "blocked_by_low_funds"}
            or c.get("delivery") == "delivering"
        )
        total_count = len(campaigns)

        # Баланс VK через API не отдаёт (проверено Бибой 26.06: /budget,
        # /account, /transactions = 404; /user.json без баланса). Показываем
        # вместо него РАСХОД ЗА СЕГОДНЯ — для контроля денег это важнее.
        from datetime import datetime

        from src.config import settings
        from src.vk_ads.models import aggregate_stats_item

        spent_today = 0.0
        try:
            ids = [int(c["id"]) for c in campaigns if c.get("id")]
            if ids:
                today = datetime.now().strftime("%Y-%m-%d")
                resp = await ads_client.get_campaign_stats(ids, today, today)
                for it in resp.get("items", []) if isinstance(resp, dict) else []:
                    s = aggregate_stats_item(it)
                    if s:
                        spent_today += s.spent_rub
        except Exception as e:
            logger.warning(f"spent_today failed: {e}")

        return (
            f"📊 Статус кабинета VK Ads (id {settings.vk_ads_account_id}):\n"
            f"- Потрачено сегодня: {spent_today:.0f}₽ из лимита "
            f"{settings.max_daily_spend_rub}₽\n"
            f"- Кампаний всего: {total_count}, активных: {active_count}\n"
            f"- Баланс: VK не отдаёт через API — смотри в кабинете VK Ads"
        )
    except Exception as e:
        logger.exception("get_account_status failed")
        return f"❌ Ошибка получения статуса: {type(e).__name__}: {e}"


# ============================================================
# Tool 2: run_audience_parsing
# ============================================================

RUN_AUDIENCE_PARSING_SCHEMA = {
    "name": "run_audience_parsing",
    "description": (
        "Запускает парсинг горячей аудитории — собирает подписчиков 4 "
        "проверенных правосл. сообществ, находит пересечения (кто в 2+ "
        "группах одновременно — это и есть 'горячая' аудитория). При "
        "наличии 2000+ ID автоматически загружает в VK Ads как remarketing "
        "сегмент. Используй когда Vizit просит собрать аудиторию для "
        "новой кампании. Параметр max_per_group: ограничение сколько "
        "подписчиков парсить с каждой группы (5000 для теста, 'all' для "
        "полного парсинга)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "max_per_group": {
                "type": "string",
                "description": (
                    "Количество подписчиков с каждой группы: число "
                    "(например '5000') или 'all' для полного парсинга. "
                    "Тест-режим (5000) занимает 1-2 минуты и даёт около "
                    "17k уникальных. Полный (all) занимает 15-25 минут "
                    "и даёт до 800k уникальных."
                ),
            },
        },
        "required": ["max_per_group"],
    },
}


async def run_audience_parsing(max_per_group: str) -> str:
    """Запускает парсинг горячей аудитории.

    Эта функция возвращает строку с прогнозом и просьбой к Vizit'у
    нажать кнопку. Реальный запуск через бот-команду /vk_audience —
    мы не запускаем парсинг отсюда напрямую чтобы Vizit видел прогресс
    в Telegram, а не где-то на бэкенде.
    """
    if max_per_group not in {"5000", "all"} and not max_per_group.isdigit():
        return (
            f"❌ Неверный max_per_group: '{max_per_group}'. "
            f"Допустимые значения: число (например '5000') или 'all'."
        )

    if max_per_group == "all":
        estimate = "15-25 минут парсинга, ожидается до 800 000 уникальных, "
        estimate += "после пересечения 2+ остаётся 50-100 тысяч горячих"
        command = "/vk_audience full"
    else:
        count = int(max_per_group) if max_per_group.isdigit() else 5000
        estimate = (
            f"1-3 минуты парсинга, ожидается около "
            f"{count * 3 // 1000}-{count * 4 // 1000} тысяч уникальных, "
            f"горячих в 2+ группах около {count // 3}"
        )
        command = f"/vk_audience {count}"

    return (
        f"📋 Готов запустить парсинг. Прогноз: {estimate}.\n\n"
        f"Скажи Vizit'у нажать в Telegram-боте команду `{command}` "
        f"(или кнопку 🔥 Горячая аудитория для тест-режима). Бот сам "
        f"парсит, считает пересечения, и если ≥2000 ID — автоматически "
        f"загружает в VK Ads как remarketing список и присылает ID "
        f"сегмента. Этот ID надо положить в Railway env "
        f"VK_AUDIENCE_SEGMENT_IDS чтобы использовать в кампаниях."
    )


# ============================================================
# Tool 3: get_campaign_stats
# ============================================================

GET_CAMPAIGN_STATS_SCHEMA = {
    "name": "get_campaign_stats",
    "description": (
        "Получает статистику конкретной кампании за последние N дней: "
        "показы, клики, CTR, CPC, расход. Используй для анализа "
        "результатов запущенных кампаний, когда Vizit спрашивает "
        "'как там кампания' или ты планируешь решение о масштабировании/"
        "отключении."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "campaign_id": {
                "type": "integer",
                "description": "ID кампании из VK Ads (ad_plan_id)",
            },
            "days": {
                "type": "integer",
                "description": "За сколько последних дней показать (1-30)",
                "minimum": 1,
                "maximum": 30,
            },
        },
        "required": ["campaign_id", "days"],
    },
}


async def get_campaign_stats(campaign_id: int, days: int) -> str:
    """Возвращает текстовое описание статистики кампании."""
    if not 1 <= days <= 30:
        return f"❌ Параметр days должен быть от 1 до 30, получено: {days}"

    try:
        from datetime import date, timedelta

        from src.vk_ads.client import VKAdsClient

        ads_client = VKAdsClient.from_settings()
        if ads_client is None:
            return "❌ VK Ads клиент не настроен (нет OAuth credentials)."

        date_to = date.today()
        date_from = date_to - timedelta(days=days - 1)

        result = await ads_client.get_campaign_stats(
            campaign_ids=[campaign_id],
            date_from=date_from.isoformat(),
            date_to=date_to.isoformat(),
        )

        items = result.get("items", [])
        if not items:
            return f"❌ По кампании {campaign_id} нет данных за {date_from}—{date_to}."

        # Каждый item — данные по одной кампании за период
        camp_data = items[0]
        rows = camp_data.get("rows", [])

        if not rows:
            return f"❌ Кампания {campaign_id} не показывалась за этот период."

        # Агрегируем по дням
        total_impressions = sum(int(r.get("base", {}).get("shows", 0)) for r in rows)
        total_clicks = sum(int(r.get("base", {}).get("clicks", 0)) for r in rows)
        total_spent = sum(float(r.get("base", {}).get("spent", 0)) for r in rows)

        ctr = (total_clicks / total_impressions * 100) if total_impressions else 0
        cpc = (total_spent / total_clicks) if total_clicks else 0

        # Нормативы из персоны Алины (для православной ниши)
        ctr_status = "❌ ниже нормы" if ctr < 0.5 else "✅ норма" if ctr < 1 else "🔥 хорошо"
        cpc_status = "❌ выше нормы" if cpc > 25 else "✅ норма" if cpc > 15 else "🔥 хорошо"

        impressions_fmt = f"{total_impressions:,}".replace(",", " ")

        return (
            f"📊 Кампания {campaign_id} за {date_from}—{date_to} ({days} дн):\n"
            f"- Показы: {impressions_fmt}\n"
            f"- Клики: {total_clicks}\n"
            f"- CTR: {ctr:.2f}% ({ctr_status})\n"
            f"- CPC: {cpc:.0f}₽ ({cpc_status})\n"
            f"- Расход: {total_spent:.0f}₽\n"
        )
    except Exception as e:
        logger.exception("get_campaign_stats tool failed")
        return f"❌ Ошибка получения статистики: {type(e).__name__}: {e}"


# ============================================================
# Tool 4: read_knowledge
# ============================================================

READ_KNOWLEDGE_SCHEMA = {
    "name": "read_knowledge",
    "description": (
        "Читает раздел базы знаний организации. Доступные файлы: "
        "working_combos (рабочие связки), failed_combos (провальные), "
        "hypotheses (гипотезы на проверке), vk_moderation_log (что "
        "отклонила модерация), orthodox_calendar (православный календарь "
        "для сезонности). Используй ПЕРЕД планированием цикла чтобы "
        "проверить что уже известно, и при упоминании конкретных дат "
        "(особенно перед праздниками)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_name": {
                "type": "string",
                "description": (
                    "Имя файла без расширения. Допустимые: "
                    "working_combos, failed_combos, hypotheses, "
                    "vk_moderation_log, orthodox_calendar, README."
                ),
                "enum": [
                    "working_combos",
                    "failed_combos",
                    "hypotheses",
                    "vk_moderation_log",
                    "orthodox_calendar",
                    "README",
                ],
            },
        },
        "required": ["file_name"],
    },
}


async def read_knowledge(file_name: str) -> str:
    """Читает содержимое файла из базы знаний."""
    if file_name not in ALLOWED_KNOWLEDGE_FILES:
        return (
            f"❌ Файл '{file_name}' не разрешён. Доступные: "
            f"{', '.join(sorted(ALLOWED_KNOWLEDGE_FILES))}"
        )

    file_path = KNOWLEDGE_BASE_DIR / f"{file_name}.md"
    if not file_path.exists():
        return f"❌ Файл {file_path} не существует."

    try:
        content = file_path.read_text(encoding="utf-8")
        if len(content) < 50:
            return f"📄 {file_name}.md: файл существует, но пуст."
        return f"📄 Содержимое {file_name}.md:\n\n{content}"
    except Exception as e:
        logger.exception(f"read_knowledge failed for {file_name}")
        return f"❌ Ошибка чтения {file_name}: {type(e).__name__}: {e}"


# ============================================================
# Tool 5: append_knowledge
# ============================================================

APPEND_KNOWLEDGE_SCHEMA = {
    "name": "append_knowledge",
    "description": (
        "Добавляет новую запись в указанный файл базы знаний. Используй "
        "после реальных тестов (вписать рабочую/провальную связку), при "
        "появлении новой гипотезы для тестирования, при отклонении "
        "креатива модерацией VK. Запись добавляется В КОНЕЦ файла. "
        "Текст должен быть в формате описанном в README базы знаний "
        "(заголовок ## с датой, секции **Период**, **Метрики**, **Вывод**)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_name": {
                "type": "string",
                "description": "Имя файла без расширения",
                "enum": [
                    "working_combos",
                    "failed_combos",
                    "hypotheses",
                    "vk_moderation_log",
                ],
            },
            "entry": {
                "type": "string",
                "description": (
                    "Текст записи в markdown формате. Должен начинаться "
                    "с заголовка '## ' и содержать дату, контекст и "
                    "вывод. Минимум 100 символов чтобы избежать пустых "
                    "записей."
                ),
                "minLength": 100,
            },
        },
        "required": ["file_name", "entry"],
    },
}


async def append_knowledge(file_name: str, entry: str) -> str:
    """Добавляет запись в конец файла базы знаний."""
    if file_name not in {"working_combos", "failed_combos", "hypotheses", "vk_moderation_log"}:
        return (
            f"❌ Запись в '{file_name}' не разрешена. Можно писать только в: "
            f"working_combos, failed_combos, hypotheses, vk_moderation_log."
        )

    if len(entry.strip()) < 100:
        return (
            f"❌ Запись слишком короткая ({len(entry.strip())} символов). "
            f"Минимум 100 — иначе это не полноценная запись."
        )

    if not entry.strip().startswith("##"):
        return (
            "❌ Запись должна начинаться с заголовка markdown '## ' "
            "(см. формат в README.md базы знаний)."
        )

    file_path = KNOWLEDGE_BASE_DIR / f"{file_name}.md"
    if not file_path.exists():
        return f"❌ Файл {file_path} не существует."

    try:
        with file_path.open("a", encoding="utf-8") as f:
            f.write("\n\n")
            f.write(entry.strip())
            f.write("\n")

        logger.info(f"Добавлена запись в {file_name}.md ({len(entry)} символов)")
        return (
            f"✅ Запись добавлена в {file_name}.md ({len(entry)} символов). "
            f"Алина может проверить и при необходимости отредактировать руками."
        )
    except Exception as e:
        logger.exception(f"append_knowledge failed for {file_name}")
        return f"❌ Ошибка записи в {file_name}: {type(e).__name__}: {e}"


# ============================================================
# Tool 6: recommend_images (рука Кирилла — креативщика)
# ============================================================

RECOMMEND_IMAGES_SCHEMA = {
    "name": "recommend_images",
    "description": (
        "Спрашивает у креативщика Кирилла, какие изображения нужны под "
        "заданную тему рекламы. Возвращает 3-4 конкретные идеи картинок "
        "(что на фото, настроение, зачем сработает) и анти-список (чего "
        "избегать по правилам VK Ads и этике православной ниши). Используй "
        "когда Vizit просит рекламу по теме — ПЕРЕД запуском, чтобы сказать "
        "ему какие фото прислать. Фото Vizit присылает сам."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "theme": {
                "type": "string",
                "description": (
                    "Тема рекламы, например 'поминовение усопших на "
                    "Радоницу' или 'молитва о здравии близких'."
                ),
            },
        },
        "required": ["theme"],
    },
}


async def recommend_images(theme: str) -> str:
    """Передаёт тему Кириллу (CampaignPlanner) и возвращает бриф по картинкам."""
    theme = (theme or "").strip()
    if not theme:
        return "❌ Не указана тема — нечего передать Кириллу."
    try:
        from src.claude_brain.campaign_planner import (
            CampaignPlanner,
            format_brief_message,
        )

        brief = await CampaignPlanner().make_brief(theme)
        return "Кирилл подобрал под тему:\n\n" + format_brief_message(brief)
    except Exception as e:
        logger.exception("recommend_images failed")
        return f"❌ Кирилл не смог подобрать картинки сейчас: {type(e).__name__}: {e}"


# ============================================================
# Tool 7: launch_ads (рука Тимура — таргетолога)
# ============================================================
# ВАЖНО: сам обработчик этого инструмента живёт НЕ здесь (нужен доступ к
# Telegram-контексту для режима ожидания фото). Боба-чат в handlers.py
# оборачивает execute_tool своим исполнителем, который перехватывает
# launch_ads. Здесь — только схема, чтобы Боба знал про инструмент.

LAUNCH_ADS_SCHEMA = {
    "name": "launch_ads",
    "description": (
        "Готовит запуск рекламы по теме через таргетолога Тимура: сам "
        "считает, сколько тестов влезает в дневной бюджет на минимальных "
        "ставках (300₽/тест), и пишет тексты. После вызова Vizit'у нужно "
        "прислать ОДНО фото — тогда кампании создадутся. Используй ТОЛЬКО "
        "когда Vizit согласился запускать рекламу по конкретной теме. Не "
        "запускай без явного согласия Vizit'а."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "theme": {
                "type": "string",
                "description": (
                    "Тема рекламы, под которую запускаем (например "
                    "'о здравии близких' или 'поминовение усопших')."
                ),
            },
        },
        "required": ["theme"],
    },
}


# ============================================================
# Tool 8: stop_ads (рука Тимура — выключение кампаний)
# ============================================================

STOP_ADS_SCHEMA = {
    "name": "stop_ads",
    "description": (
        "Выключает (ставит на паузу) рекламу рукой Тимура. target='all' — "
        "выключить ВСЕ активные кампании; либо передай ID кампании "
        "(ad_plan_id числом-строкой), чтобы выключить одну. Используй когда "
        "Vizit говорит 'отключи', 'останови', 'выключи рекламу'. Пауза "
        "безопасна: деньги перестают тратиться, ничего не удаляется. Не "
        "спрашивай разрешения у Тимура — ты сам им командуешь."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "'all' для всех активных, либо ID кампании (число строкой).",
            },
        },
        "required": ["target"],
    },
}


async def stop_ads(target: str) -> str:
    """Выключает кампании (рука Тимура). Пишет в аудит-лог."""
    from src.db.repository import log_action
    from src.vk_ads.client import VKAdsClient

    client = VKAdsClient.from_settings()
    if client is None:
        return "❌ VK Ads не настроен — нечем выключать."

    raw = (target or "").strip()
    low = raw.lower()
    try:
        if low in {"all", "все", "всё", "all_active"}:
            active = await client.get_active_ad_plans()
            if not active:
                return "Активных кампаний нет — выключать нечего."
            stopped = 0
            for c in active:
                cid = int(c["id"])
                try:
                    await client.pause_campaign(cid)
                    await log_action(
                        "pause", target_id=cid,
                        reason="Боба по команде Vizit'а: выключить всё", auto=False,
                    )
                    stopped += 1
                except Exception as e:
                    logger.warning(f"stop_ads: не смог выключить {cid}: {e}")
            return f"Готово: Тимур выключил {stopped} из {len(active)} активных кампаний."

        if raw.isdigit():
            cid = int(raw)
            await client.pause_campaign(cid)
            await log_action(
                "pause", target_id=cid,
                reason="Боба по команде Vizit'а", auto=False,
            )
            return f"Готово: кампания {cid} выключена."

        return (
            f"Не понял, что выключать: '{raw}'. Скажи 'all' (выключить всё) "
            f"или дай номер кампании."
        )
    except Exception as e:
        logger.exception("stop_ads failed")
        return f"❌ Не смог выключить: {type(e).__name__}: {e}"


# ============================================================
# Tool 9: review_results (рука Алины — итоги и следующий раунд)
# ============================================================

REVIEW_RESULTS_SCHEMA = {
    "name": "review_results",
    "description": (
        "Подводит итоги накопленного опыта рукой аналитика Алины: сколько "
        "рабочих связок (победителей) и провалов в базе знаний, какие "
        "лучшие по CPL, и что делать в следующем раунде. Используй когда "
        "Vizit спрашивает 'что зашло', 'итоги', 'какие победители', "
        "'что запускать дальше', или планируешь новый раунд тестов."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}


async def review_results() -> str:
    """Итоги раунда (рука Алины) — читает базу знаний."""
    try:
        from src.knowledge.round_analyzer import summarize_knowledge

        s = summarize_knowledge()
        return (
            f"📊 Алина по итогам: победителей {len(s['winners'])}, "
            f"провалов {len(s['failures'])}.\n{s['recommendation']}"
        )
    except Exception as e:
        logger.exception("review_results failed")
        return f"❌ Не смог подвести итоги: {type(e).__name__}: {e}"


# ============================================================
# Регистрация всех tools для передачи в Anthropic API
# ============================================================

BOBA_TOOLS_SCHEMA: list[dict] = [
    GET_ACCOUNT_STATUS_SCHEMA,
    RUN_AUDIENCE_PARSING_SCHEMA,
    GET_CAMPAIGN_STATS_SCHEMA,
    READ_KNOWLEDGE_SCHEMA,
    APPEND_KNOWLEDGE_SCHEMA,
    RECOMMEND_IMAGES_SCHEMA,
    LAUNCH_ADS_SCHEMA,
    STOP_ADS_SCHEMA,
    REVIEW_RESULTS_SCHEMA,
]
"""Список tool definitions для передачи в поле tools запроса к Anthropic API."""


# Диспетчер: имя tool → Python функция
_TOOL_DISPATCHER = {
    "get_account_status": get_account_status,
    "run_audience_parsing": run_audience_parsing,
    "get_campaign_stats": get_campaign_stats,
    "read_knowledge": read_knowledge,
    "append_knowledge": append_knowledge,
    "recommend_images": recommend_images,
    "stop_ads": stop_ads,
    "review_results": review_results,
}


async def execute_tool(name: str, args: dict[str, Any]) -> str:
    """Диспетчер: получает имя tool и аргументы, вызывает Python функцию.

    Возвращает строку с результатом, которая идёт обратно к Бобе в
    блоке tool_result. Если tool не существует или упал — возвращает
    описание ошибки (не падает).
    """
    if name not in _TOOL_DISPATCHER:
        return (
            f"❌ Неизвестный tool '{name}'. Доступные: "
            f"{', '.join(sorted(_TOOL_DISPATCHER.keys()))}"
        )

    func = _TOOL_DISPATCHER[name]
    try:
        # Все наши tools — async функции
        return await func(**args)
    except TypeError as e:
        # Неправильные аргументы
        return f"❌ Неверные аргументы для {name}: {e}"
    except Exception as e:
        logger.exception(f"Tool {name} crashed")
        return f"❌ Tool {name} упал с ошибкой: {type(e).__name__}: {e}"
