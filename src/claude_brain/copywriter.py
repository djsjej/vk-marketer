"""Генератор текстов объявлений через Claude.

Учитывает специфику VK Реклама API v2 (поля textblocks):
- title_40_vkads: до 40 символов
- text_2000: до 2000 символов
- about_company_115: до 115 символов
- cta_community_vk: enum CTA (см. banner_fields.json от Бибы)

Формат под православную аудиторию: тон спокойный, без манипуляций, без эмодзи-флуда.
Когда тема нерелигиозная — Claude сам подстраивается под входящий контекст.

Бизнес-модель Vizit'а (vk.com/pomolimsy):
1. Объявление зовёт человека НАПИСАТЬ имена близких в сообщество
2. Человек пишет → принимается ботом/админом
3. Имя попадает в молитвенную рассылку
4. Опционально — добровольное пожертвование за поминовение

Поэтому основной CTA в православной теме — "write" (Написать),
а не "signUp" (Вступить).
"""

import json
import logging

from anthropic import AsyncAnthropic

from src.config import settings
from src.services.ad_creator import AdCopy

logger = logging.getLogger(__name__)


# Допустимые значения cta_community_vk из banner_fields.json
# (опросили через Бибу /banner_fields.json):
# signUp, buy, contactUs, subscribe, message, write, visitSite, learnMore,
# getoffer, book, enroll, askQuestion, startChat, getPrice.
# В промптах ограничиваем подмножеством релевантных для нашей ниши.
COPYWRITER_SYSTEM_PROMPT_SINGLE = """Ты — копирайтер VK Рекламы. По заданной теме генерируешь текст одного объявления.

Формат VK-объявления (поля textblocks):
- title: до 40 символов (заголовок)
- text: до 2000 символов (основной текст)
- about: до 115 символов (о компании/проекте)
- cta: один из ["write", "message", "subscribe", "learnMore", "signUp"]

Стиль зависит от темы:
- Если тема православная (монастырь, святые, молитвы, поминовение, пожертвования) —
  тон спокойный, благоговейный, без эмодзи и мирских клише типа «акция», «срочно», «успей».
  Можно цитировать святых отцов или Писание с указанием источника.
  ОСНОВНОЕ ДЕЙСТВИЕ — попросить читателя НАПИСАТЬ имена близких для поминовения
  (за здравие или за упокой), а не вступить или подписаться. Текст подводит к этому
  действию: «Напишите имена дорогих сердцу — за них помолятся в монастыре».
  CTA: "write" (Написать) — основной выбор для православной темы.
  Альтернативы: "message", "subscribe", "learnMore".
- Если тема светская (продукт, услуга, мероприятие) — обычный рекламный тон,
  но без агрессивных «купи сейчас!». CTA подбирай по смыслу из enum выше.

Возвращай ТОЛЬКО JSON следующей структуры, без преамбулы и Markdown:
{
  "title": "...",
  "text": "...",
  "about": "...",
  "cta": "write"
}

Каждое поле — строка. Длины строго в лимитах VK."""


COPYWRITER_SYSTEM_PROMPT_VARIANTS = """Ты — копирайтер VK Рекламы. По заданной теме генерируешь N разных вариантов одного объявления для A/B-теста.

Формат VK-объявления (поля textblocks):
- title: до 40 символов (заголовок)
- text: до 2000 символов (основной текст)
- about: до 115 символов (о компании/проекте)
- cta: один из ["write", "message", "subscribe", "learnMore", "signUp"]

ВАЖНО: варианты должны действительно отличаться — разные посылы, углы подачи, эмоциональные триггеры. Не пересказ одного и того же другими словами.

Подходы к разным вариантам:
1. Эмоциональный — апеллируй к чувствам и переживаниям
2. Рациональный — конкретные выгоды/факты/польза
3. Социальный — упоминание сообщества, единства, людей
4. Прямой призыв — короткий, сильный, мотивирующий

Стиль зависит от темы:
- Если тема православная (монастырь, святые, молитвы, поминовение, пожертвования) —
  тон спокойный, благоговейный, без эмодзи и мирских клише.
  Можно цитировать святых отцов или Писание с указанием источника.
  ОСНОВНОЕ ДЕЙСТВИЕ — попросить написать имена близких для поминовения
  (за здравие или за упокой), а не вступить в сообщество.
  Подводи читателя к этому призыву через каждый вариант, по-разному
  (память о близких, благодарность, просьба о помощи, традиция).
  CTA: "write" — основной выбор для православной темы.
- Если тема светская — обычный рекламный тон без агрессии.

Возвращай ТОЛЬКО JSON следующей структуры, без преамбулы и Markdown:
{
  "variants": [
    {"title": "...", "text": "...", "about": "...", "cta": "write"},
    {"title": "...", "text": "...", "about": "...", "cta": "message"},
    ...
  ]
}

Длины строго в лимитах VK. Количество элементов в variants должно соответствовать запросу."""


class CopywriterError(Exception):
    """Ошибка генерации копирайта."""


class ClaudeCopywriter:
    """Генератор рекламных текстов через Claude."""

    def __init__(self, api_key: str | None = None):
        self.client = AsyncAnthropic(api_key=api_key or settings.anthropic_api_key)
        self.model = settings.anthropic_model

    async def generate_copy(self, theme: str, extra_context: str = "") -> AdCopy:
        """Сгенерировать одно объявление по теме (legacy, для одного варианта)."""
        user_message = f"Тема: {theme}"
        if extra_context:
            user_message += f"\n\nКонтекст: {extra_context}"
        user_message += "\n\nСгенерируй одно объявление."

        try:
            message = await self.client.messages.create(
                model=self.model,
                max_tokens=2500,
                system=COPYWRITER_SYSTEM_PROMPT_SINGLE,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as e:
            raise CopywriterError(f"Claude API недоступен: {e}") from e

        return self._parse_single(message.content[0].text)  # type: ignore

    async def generate_copy_variants(
        self, theme: str, n: int = 4, extra_context: str = ""
    ) -> list[AdCopy]:
        """Сгенерировать N разных вариантов объявления для A/B-выбора.

        Args:
            theme: краткое описание темы
            n: сколько вариантов (рекомендуется 3-5)
            extra_context: доп. контекст (что за проект, ссылка)

        Returns:
            Список AdCopy (длина = n при успехе, может быть меньше если Claude
            прислал меньше вариантов).
        """
        if n < 1 or n > 8:
            raise ValueError("n должно быть от 1 до 8")

        user_message = f"Тема: {theme}\n\nСгенерируй ровно {n} разных вариантов."
        if extra_context:
            user_message = f"Тема: {theme}\n\nКонтекст: {extra_context}\n\nСгенерируй ровно {n} разных вариантов."

        try:
            message = await self.client.messages.create(
                model=self.model,
                max_tokens=4000,
                system=COPYWRITER_SYSTEM_PROMPT_VARIANTS,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as e:
            raise CopywriterError(f"Claude API недоступен: {e}") from e

        return self._parse_variants(message.content[0].text)  # type: ignore

    @staticmethod
    def _parse_single(raw: str) -> AdCopy:
        """Парсит JSON одного варианта в AdCopy."""
        cleaned = (
            raw.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error(f"Не распарсил JSON: {raw[:300]}")
            raise CopywriterError(f"Claude вернул не JSON: {e}") from e

        try:
            return AdCopy(
                title=str(data["title"])[:40],
                text=str(data["text"])[:2000],
                about=str(data["about"])[:115],
                cta=str(data.get("cta", "write")),
            )
        except KeyError as e:
            raise CopywriterError(f"В ответе нет поля {e}: {data}") from e

    @staticmethod
    def _parse_variants(raw: str) -> list[AdCopy]:
        """Парсит JSON со списком вариантов в [AdCopy]."""
        cleaned = (
            raw.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error(f"Не распарсил JSON вариантов: {raw[:500]}")
            raise CopywriterError(f"Claude вернул не JSON: {e}") from e

        variants_raw = data.get("variants", [])
        if not isinstance(variants_raw, list) or not variants_raw:
            raise CopywriterError(f"В ответе нет вариантов: {data}")

        result = []
        for i, v in enumerate(variants_raw):
            if not isinstance(v, dict):
                logger.warning(f"Вариант {i} не словарь: {v}")
                continue
            try:
                result.append(AdCopy(
                    title=str(v["title"])[:40],
                    text=str(v["text"])[:2000],
                    about=str(v["about"])[:115],
                    cta=str(v.get("cta", "write")),
                ))
            except KeyError as e:
                logger.warning(f"В варианте {i} нет поля {e}: {v}")
                continue

        if not result:
            raise CopywriterError("Не удалось распарсить ни одного варианта")
        return result


def fallback_copy_from_caption(caption: str) -> AdCopy:
    """Если Claude недоступен — собираем AdCopy из caption напрямую."""
    caption = caption.strip()
    first_line = caption.split("\n", 1)[0].strip()
    title = first_line[:40] if first_line else "Напишите имена"
    text = caption[:2000] if caption else "Напишите имена близких — за них помолятся."
    about = first_line[:115] if first_line else "Православная община"
    return AdCopy(title=title, text=text, about=about, cta="write")
