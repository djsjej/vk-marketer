"""Генератор текстов объявлений под православную аудиторию."""

import json
import logging

from anthropic import AsyncAnthropic

from src.config import settings

logger = logging.getLogger(__name__)


COPYWRITER_SYSTEM_PROMPT = """Ты — копирайтер православной направленности, \
специализирующийся на рекламных текстах для VK Рекламы.

Особенности тона для православной аудитории:
- Нет мирских клише («не упустите шанс», «срочно», «акция»).
- Нет эмодзи кроме редких уместных (✝️, 🙏 — и то осторожно).
- Тон спокойный, благоговейный, но не сухой.
- Можно цитировать святых отцов, Писание (с указанием источника).
- Никакой манипуляции страхом или жадностью — только искренность.
- Призыв к действию мягкий: «Узнать больше», «Прочитать», «Заказать молебен», \
«Подписаться на канал».

Формат VK-объявления:
- Заголовок: 25 символов максимум
- Описание: 90 символов максимум

Тебе дают тему. Ты возвращаешь N вариантов в JSON:
{
  "variants": [
    {"title": "...", "description": "..."},
    {"title": "...", "description": "..."}
  ]
}

Только JSON, никакого текста до или после.
"""


class ClaudeCopywriter:
    """Генератор рекламных текстов через Claude."""

    def __init__(self, api_key: str | None = None):
        self.client = AsyncAnthropic(api_key=api_key or settings.anthropic_api_key)
        self.model = settings.anthropic_model

    async def generate_variants(
        self, topic: str, n_variants: int = 3
    ) -> list[dict]:
        """Генерирует N вариантов заголовка+описания для темы.

        Args:
            topic: тема рекламы (например, "святой Спиридон, чудеса на Корфу")
            n_variants: сколько вариантов сгенерировать (3-5 рекомендуется)

        Returns:
            Список словарей вида [{"title": "...", "description": "..."}, ...]
        """
        message = await self.client.messages.create(
            model=self.model,
            max_tokens=1500,
            system=COPYWRITER_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Тема: {topic}\n\n"
                        f"Сгенерируй {n_variants} разных вариантов объявления."
                    ),
                }
            ],
        )

        raw = message.content[0].text  # type: ignore
        try:
            data = json.loads(raw)
            return data.get("variants", [])
        except json.JSONDecodeError as e:
            logger.error(f"Не удалось распарсить ответ копирайтера: {e}\nRaw: {raw}")
            return []
