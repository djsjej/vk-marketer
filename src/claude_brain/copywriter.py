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


COPYWRITER_SYSTEM_PROMPT = """Ты — копирайтер VK Рекламы. По заданной теме генерируешь текст одного объявления.

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


class CopywriterError(Exception):
    """Ошибка генерации копирайта."""


class ClaudeCopywriter:
    """Генератор рекламных текстов через Claude."""

    def __init__(self, api_key: str | None = None):
        self.client = AsyncAnthropic(api_key=api_key or settings.anthropic_api_key)
        self.model = settings.anthropic_model

    async def generate_copy(self, theme: str, extra_context: str = "") -> AdCopy:
        """Сгенерировать одно объявление по теме.

        Args:
            theme: краткое описание темы (как тема статьи)
            extra_context: дополнительный контекст (например, что это за община,
                           какая ссылка, чему посвящено)

        Returns:
            AdCopy с готовыми текстами под VK Ads textblocks.

        Raises:
            CopywriterError: если Claude вернул мусор.
        """
        user_message = f"Тема: {theme}"
        if extra_context:
            user_message += f"\n\nКонтекст: {extra_context}"
        user_message += "\n\nСгенерируй одно объявление."

        try:
            message = await self.client.messages.create(
                model=self.model,
                max_tokens=2500,
                system=COPYWRITER_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as e:
            raise CopywriterError(f"Claude API недоступен: {e}") from e

        raw = message.content[0].text  # type: ignore
        # Иногда Claude несмотря на инструкцию оборачивает в ```json ... ```
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error(f"Не распарсил JSON копирайтера: {raw[:300]}")
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


def fallback_copy_from_caption(caption: str) -> AdCopy:
    """Если Claude недоступен — собираем AdCopy из caption напрямую.

    Используется как safe-default: пользователь сам пишет текст,
    бот не сочиняет ничего творческого.

    Берёт первую строку как title (обрезая до 40), весь caption как text,
    и стандартный about + signUp.
    """
    caption = caption.strip()
    first_line = caption.split("\n", 1)[0].strip()
    title = first_line[:40] if first_line else "Узнать больше"
    text = caption[:2000] if caption else "Подробнее на странице сообщества."
    about = first_line[:115] if first_line else "Православная община"
    return AdCopy(title=title, text=text, about=about, cta="signUp")
