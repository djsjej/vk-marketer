"""Фильтр православных сообществ ВКонтакте.

Проблема: VK groups.search возвращает результаты по релевантности к
ключевому слову, без учёта смысла. По запросу «монастырь» прилетает
всё что угодно — настоящие монастыри, сатанинские «храмы», юмор-паблики,
околорелигиозный треш, секты, и т.д.

Для нашей бизнес-модели Pomolimsy (молитвы за близких) это критично:
рекламироваться на подписчиков сатанинского сообщества было бы катастрофой.

Решение — двухуровневый фильтр:

1. quick_filter_obviously_bad — простой эвристик на словах.
   Отсеивает 90% очевидного треша мгновенно. Дёшево.

2. claude_filter_orthodox — Claude читает название+описание каждого
   оставшегося сообщества и решает «реально православное или нет».
   Дорого (один запрос на N групп), но точно.

Использовать так:

    filtered = [g for g in groups if quick_filter_obviously_bad(g)]
    orthodox_only = await claude_filter_orthodox(filtered)
"""

from __future__ import annotations

import json
import logging
import re

from anthropic import AsyncAnthropic

from src.config import settings

logger = logging.getLogger(__name__)


# Стоп-слова которые явно указывают на НЕ-православный контекст.
# Регулярки — чтобы ловить и склонения, и формы (сатан → сатанинский, сатана).
# Не используем full-word match потому что в русском много склонений.
_OBVIOUSLY_BAD_PATTERNS = [
    r"сатан",          # сатанинский, сатана
    r"оккульт",        # оккультный, оккультизм
    r"\bмаги[яиюей]",  # магия, магии (как корень — но не "магистр")
    r"колдов",         # колдовство, колдун
    r"ведьм",          # ведьма, ведьмак
    r"астрол",         # астрология, астролог
    r"таро\b",         # карты таро
    r"эзотери",        # эзотерика, эзотерический
    r"приколы?\b",     # прикол, приколы
    r"\bюмор",         # юмор (но не «гуманитар»)
    r"\bмем[ыов]?\b",  # мем, мемы
    r"анекдот",        # анекдоты
    r"шутк",           # шутки, шуточный
    r"\bлгбт",         # лгбт-сообщества
    r"атеист",         # атеизм
    r"антирелиг",      # антирелигиозный
    r"антиправослав",  # антиправославный
    r"антицерков",     # антицерковный
    r"sect|секта",     # секта (если явно)
    r"свидетел[ия]\s+иеговы",  # секта Свидетелей Иеговы
    r"саентол",        # саентология
    r"кришна",         # кришнаизм (другая религия, не наша ниша)
    r"будд[аеи]",      # буддизм
    r"ислам",          # ислам
    r"мусульман",      # мусульманство
    r"католи[кч]",     # католики, католический
    r"\bпротестант",   # протестанты
    r"иуда[иеи]",      # иудаизм
    r"\bхэллоуин",     # хэллоуин
    r"halloween",
]


# Компилируем регулярки один раз для скорости.
_BAD_PATTERN_RE = re.compile("|".join(_OBVIOUSLY_BAD_PATTERNS), re.IGNORECASE)


def quick_filter_obviously_bad(group: dict) -> bool:
    """Быстрый эвристик — стоит ли группу пропускать дальше.

    Args:
        group: словарь группы из VK API (name, description, activity).

    Returns:
        True — оставляем для следующего этапа (Claude).
        False — точно не наше, отсеиваем сразу.

    Note:
        Намеренно консервативный — лучше пропустить сомнительное в Claude
        чем отрезать реальное православное сообщество по ошибке.
    """
    name = group.get("name") or ""
    description = group.get("description") or ""
    activity = group.get("activity") or ""

    text = f"{name} {description} {activity}".lower()

    if _BAD_PATTERN_RE.search(text):
        return False
    return True


# System prompt для Claude — определяет какие группы реально православные.
_CLAUDE_FILTER_SYSTEM = """Ты помогаешь православному маркетологу отбирать VK-сообщества для рекламы проекта молитвенной помощи (написание имён близких для поминовения).

Тебе дают список VK-сообществ с названием и описанием. Для каждого нужно решить:
- TRUE если сообщество **реально православное** (Русская Православная Церковь, монастырь РПЦ, православный храм/приход/собор, духовный центр, православные молитвы, иконы, святые отцы, православная семья, новости РПЦ)
- FALSE во всех остальных случаях

FALSE для:
- Других конфессий (католики, протестанты, иудеи, мусульмане, буддисты)
- Сект и культов (свидетели Иеговы, саентология, кришнаиты, секта-NN)
- Оккультного (магия, гороскопы, эзотерика, шаманы)
- Юмористических («Православные шутят», приколы про церковь)
- Антирелигиозных (атеизм, антицерковные)
- Сатанинских и тёмных
- Просто старых храмов как туристических достопримечательностей без религиозного контента
- Региональных сообществ городов где «храм» в названии но контент о городе вообще

TRUE для:
- Официальные сообщества монастырей, лавр, храмов РПЦ
- Православные образовательные/просветительские проекты
- Православные родительские/семейные сообщества с религиозным контентом
- Православные молитвенные/поминальные сообщества
- Иконы, святые отцы, Псалтирь, патерики
- Православные СМИ (Православие.фм, Фома, и т.п.)
- Если описание пустое — оценивай только по названию; если название явно православное (упомянут монастырь, святой, литургия) — TRUE; если двусмысленное — FALSE (лучше упустить чем попасть на чужое).

Отвечай ТОЛЬКО валидным JSON без какого-либо текста до/после, в формате:
{"results": [{"id": <group_id>, "orthodox": true|false, "reason": "<краткая причина 5-10 слов>"}]}

Поле "reason" обязательно — это даёт прозрачность владельцу проекта."""


async def claude_filter_orthodox(
    groups: list[dict],
    batch_size: int = 30,
) -> list[dict]:
    """Прогоняет группы через Claude, оставляет только реально православные.

    Args:
        groups: список словарей групп от VK API. Каждый должен иметь
            поля id, name, description.
        batch_size: сколько групп слать в одном запросе к Claude.
            30 — компромисс: достаточно контекста на одну группу, но
            не упираемся в лимит токенов.

    Returns:
        Подмножество входного списка — только подтверждённые православные.
        Поле `_orthodox_reason` (внутреннее) добавляется к каждому
        прошедшему элементу — для отчёта Vizit'у почему оставили.

    Если Claude недоступен — возвращаем исходный список без фильтрации
    (fail-safe: лучше показать всё чем потерять данные).
    """
    if not groups:
        return []

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    result: list[dict] = []

    for batch_start in range(0, len(groups), batch_size):
        batch = groups[batch_start:batch_start + batch_size]

        # Готовим компактное представление каждой группы для Claude.
        # Описание обрезаем — чтобы не разбавлять контекст и экономить токены.
        items_for_claude = []
        for g in batch:
            desc = (g.get("description") or "")[:300]
            items_for_claude.append({
                "id": g.get("id"),
                "name": g.get("name") or "?",
                "description": desc,
            })

        user_message = (
            "Вот список VK-сообществ. Для каждого определи "
            "православное оно или нет:\n\n"
            f"{json.dumps(items_for_claude, ensure_ascii=False, indent=2)}"
        )

        try:
            message = await client.messages.create(
                model=settings.anthropic_model,
                max_tokens=4000,
                system=_CLAUDE_FILTER_SYSTEM,
                messages=[{"role": "user", "content": user_message}],
            )
            response_text = message.content[0].text  # type: ignore
        except Exception as e:
            logger.exception("Claude недоступен для orthodox filter — отдаём батч без фильтрации")
            # Fail-safe — лучше показать всё, чем потерять валидные группы
            result.extend(batch)
            continue

        # Парсим JSON-ответ Claude. Бывает обёрнут в ``` или ```json — снимаем.
        clean = response_text.strip()
        clean = re.sub(r"^```(?:json)?\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean)

        try:
            parsed = json.loads(clean)
            decisions = parsed.get("results", [])
        except (json.JSONDecodeError, AttributeError) as e:
            logger.warning(
                "Не смог распарсить ответ Claude для orthodox filter: %s. Ответ: %s",
                e,
                response_text[:300],
            )
            # Fail-safe — отдаём батч без фильтрации
            result.extend(batch)
            continue

        # Сопоставляем решения с исходными группами по id
        decisions_by_id = {
            d["id"]: d for d in decisions if isinstance(d, dict) and "id" in d
        }

        for g in batch:
            gid = g.get("id")
            decision = decisions_by_id.get(gid)
            if decision and decision.get("orthodox") is True:
                # Сохраняем причину для отчёта пользователю
                g_with_reason = {**g, "_orthodox_reason": decision.get("reason", "")}
                result.append(g_with_reason)

    return result
