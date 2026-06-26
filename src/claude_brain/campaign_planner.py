"""Планировщик кампании по теме (Шаг 1 продукта).

Вход в продукт по видению Vizit'а: «говорю тему → бот анализирует → даёт
рекомендации, какие картинки нужны». Это роль креативщика Кирилла из
персон организации — здесь она впервые подключается к делу.

Безопасно: только вызов Claude, без денег и без VK API. Возвращает бриф
с конкретными идеями картинок (что прислать) и анти-списком (что НЕ
присылать — по правилам VK Ads и этике православной ниши).
"""

from __future__ import annotations

import json
import logging

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field

from src.config import settings

logger = logging.getLogger(__name__)


# Экспертиза Кирилла (src/agents/personas.py) сжата в рабочий промпт.
# Правила VK Ads по изображениям и табу православной ниши — оттуда же.
PLANNER_SYSTEM_PROMPT = """Ты — Кирилл, креативщик VK Рекламы для православной аудитории \
(монастыри, святые, поминовение, молитва). Воцерковлён, понимаешь нюансы традиции \
и поведенческую психологию.

Тебе дают ТЕМУ будущей рекламы. Цель рекламы — чтобы человек НАПИСАЛ имена близких \
для поминовения в сообщество. Твоя задача — разобрать тему и сказать заказчику, \
КАКИЕ ИЗОБРАЖЕНИЯ ему нужно прислать для теста (он пришлёт фото сам).

Дай 3-4 конкретные идеи картинок. Каждая идея — это чёткое ТЗ на одно фото: \
что на нём, какое настроение, почему оно сработает на эту тему и аудиторию \
(женщины 41-58, православные). Идеи должны быть РАЗНЫМИ по подаче, чтобы было \
что A/B-тестировать.

Правила православной ниши (соблюдай строго):
- Образы: храмы, иконы, свечи, духовные лица, монастырь, рассвет/закат, природа.
- Тон: умиротворённый, торжественный, благодарственный.
- НЕЛЬЗЯ: кликбейт, манипуляция страхом, обещание «чудес/исцелений», сцены похорон, \
  шок-контент, яркие эмодзи на картинке, современный сленг.

Правила VK Ads по изображениям (попадут в анти-список avoid):
- Текста на картинке ≤20% площади (лучше вообще без текста).
- Без агрессивной цветокоррекции и перенасыщенных фильтров.
- Без склейки нескольких картинок, без «до/после», без имитации кнопок интерфейса.
- Без эмодзи и спецсимволов на изображении.
- Грамотно, без сцен из похорон и шок-изображений.

Верни ТОЛЬКО JSON без преамбулы и Markdown:
{
  "analysis": "1-2 фразы: на какую боль/повод бьём и каким углом",
  "image_ideas": [
    {"description": "что на фото", "mood": "настроение", "why": "почему сработает"},
    ...
  ],
  "avoid": ["чего избегать в картинках 1", "..."]
}
"""


class ImageIdea(BaseModel):
    """Одна идея картинки — ТЗ на фото."""

    description: str
    mood: str = ""
    why: str = ""


class CampaignBrief(BaseModel):
    """Бриф под тему: разбор + идеи картинок + анти-список."""

    theme: str
    analysis: str = ""
    image_ideas: list[ImageIdea] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)


class PlannerError(Exception):
    """Ошибка планировщика."""


class CampaignPlanner:
    """Разбирает тему и выдаёт бриф по картинкам через Claude."""

    def __init__(self, api_key: str | None = None):
        self.client = AsyncAnthropic(api_key=api_key or settings.anthropic_api_key)
        self.model = settings.anthropic_model

    async def make_brief(self, theme: str) -> CampaignBrief:
        """По теме возвращает CampaignBrief. Бросает PlannerError при сбое."""
        theme = (theme or "").strip()
        if not theme:
            raise PlannerError("Пустая тема")

        try:
            message = await self.client.messages.create(
                model=self.model,
                max_tokens=2000,
                system=PLANNER_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": f"Тема: {theme}"}],
            )
        except Exception as e:
            raise PlannerError(f"Claude API недоступен: {e}") from e

        return self._parse(theme, message.content[0].text)  # type: ignore

    @staticmethod
    def _parse(theme: str, raw: str) -> CampaignBrief:
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
            logger.error(f"Планировщик: не распарсил JSON: {raw[:300]}")
            raise PlannerError(f"Claude вернул не JSON: {e}") from e

        ideas = []
        for it in data.get("image_ideas", []):
            if isinstance(it, dict) and it.get("description"):
                ideas.append(
                    ImageIdea(
                        description=str(it["description"]),
                        mood=str(it.get("mood", "")),
                        why=str(it.get("why", "")),
                    )
                )
        if not ideas:
            raise PlannerError("В ответе нет идей картинок")

        return CampaignBrief(
            theme=theme,
            analysis=str(data.get("analysis", "")),
            image_ideas=ideas,
            avoid=[str(a) for a in data.get("avoid", []) if a],
        )


def format_brief_message(brief: CampaignBrief) -> str:
    """Бриф → сообщение в Telegram (Markdown)."""
    lines = [f"🎨 *Под тему «{brief.theme}» нужны такие картинки:*"]
    if brief.analysis:
        lines.append(f"\n_{brief.analysis}_")
    for i, idea in enumerate(brief.image_ideas, 1):
        lines.append(f"\n*{i}.* {idea.description}")
        if idea.mood:
            lines.append(f"   _настроение:_ {idea.mood}")
        if idea.why:
            lines.append(f"   _зачем:_ {idea.why}")
    if brief.avoid:
        lines.append("\n*Чего избегать на фото:*")
        for a in brief.avoid:
            lines.append(f"• {a}")
    lines.append(
        "\n\nКогда подберёшь фото — пришли его сюда с темой в подписи, "
        "и я соберу объявления и тесты."
    )
    return "\n".join(lines)
