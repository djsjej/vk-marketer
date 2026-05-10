"""Генератор текстов объявлений через Claude.

Учитывает специфику VK Реклама API v2 (поля textblocks):
- title_40_vkads: до 40 символов
- text_2000: до 2000 символов
- about_company_115: до 115 символов
- cta_community_vk: enum CTA (signUp, learnMore, openSite, ...)

Формат под православную аудиторию: тон спокойный, без манипуляций, без эмодзи-флуда.
Когда тема нерелигиозная — Claude сам подстраивается под входящий контекст.
"""

import json
import logging

from anthropic import AsyncAnthropic

from src.config import settings
from src.services.ad_creator import AdCopy

logger = logging.getLogger(__name__)


COPYWRITER_SYSTEM_PROMPT_SINGLE = """Ты — копирайтер VK Рекламы. По заданной теме генерируешь текст одного объявления.

Формат VK-объявления (поля textblocks):
- title: до 40 символов (заголовок)
- text: до 2000 символов (основной текст)
- about: до 115 символов (о компании/проекте)
- cta: один из ["signUp", "learnMore", "openSite", "subscribe", "join"]

Стиль зависит от темы:
- Если тема православная (монастырь, святые, молитвы, пожертвования) — тон спокойный, благоговейный, без эмодзи и мирских клише типа «акция», «срочно», «успей». Можно цитировать святых отцов или Писание с указанием источника. CTA мягкий: signUp, subscribe, learnMore.
- Если тема светская (продукт, услуга, мероприятие) — обычный рекламный тон, можно использовать call-to-action, но без агрессивных «купи сейчас!».

Возвращай ТОЛЬКО JSON следующей структуры, без преамбулы и Markdown:
{
  "title": "...",
  "text": "...",
  "about": "...",
  "cta": "signUp"
}

Каждое поле — строка. Длины строго в лимитах VK."""


COPYWRITER_SYSTEM_PROMPT_VARIANTS = """Ты — копирайтер VK Рекламы. По заданной теме генерируешь N разных вариантов одного объявления для A/B-теста.

Формат VK-объявления (поля textblocks):
- title: до 40 символов (заголовок)
- text: до 2000 символов (основной текст)
- about: до 115 символов (о компании/проекте)
- cta: один из ["signUp", "learnMore", "openSite", "subscribe", "join"]

ВАЖНО: варианты должны действительно отличаться — разные посылы, углы подачи, эмоциональные триггеры. Не пересказ одного и того же другими словами.

Подходы к разным вариантам:
1. Эмоциональный — апеллируй к чувствам и переживаниям
2. Рациональный — конкретные выгоды/факты/польза
3. Социальный — упоминание сообщества, единства, людей
4. Прямой призыв — короткий, сильный, мотивирующий

Стиль зависит от темы:
- Если тема православная (монастырь, святые, молитвы, пожертвования) — тон спокойный, благоговейный, без эмодзи и мирских клише типа «акция», «срочно», «успей». Можно цитировать святых отцов или Писание с указанием источника. CTA мягкий: signUp, subscribe, learnMore.
- Если тема светская (продукт, услуга, мероприятие) — обычный рекламный тон, можно использовать call-to-action, но без агрессивных «купи сейчас!».

Возвращай ТОЛЬКО JSON следующей структуры, без преамбулы и Markdown:
{
  "variants": [
    {"title": "...", "text": "...", "about": "...", "cta": "signUp"},
    {"title": "...", "text": "...", "about": "...", "cta": "learnMore"},
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
                cta=str(data.get("cta", "signUp")),
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
                    cta=str(v.get("cta", "signUp")),
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
    title = first_line[:40] if first_line else "Узнать больше"
    text = caption[:2000] if caption else "Подробнее на странице сообщества."
    about = first_line[:115] if first_line else "Православная община"
    return AdCopy(title=title, text=text, about=about, cta="signUp")
