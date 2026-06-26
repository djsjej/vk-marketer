"""Анализатор метрик кампаний через Claude API.

Берёт сырые метрики, отдаёт Claude, получает рекомендации в структурированном виде.
"""

import json
import logging

from anthropic import AsyncAnthropic

from src.config import settings
from src.vk_ads.models import CampaignStats

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — опытный performance-маркетолог, специализирующийся на VK Рекламе \
для православной аудитории (монастыри, святые, духовная литература).

Тебе дают метрики рекламных кампаний за период. Твоя задача:
1. Найти явные проблемы (слив бюджета, нулевая конверсия, аномалии).
2. Найти что работает (низкий CPL, высокий CTR относительно ниши).
3. Дать конкретные рекомендации к действию.

Бенчмарки для православной ниши (ЕДИНЫЙ стандарт организации — те же
цифры в Сторже `src/scheduler/safety.py` и в персоне Алины; не называй
других чисел, иначе твои выводы разойдутся с правилами безопасности):
- CPL (главная метрика, цена написавшего в сообщество):
  отлично ≤20₽, хорошо ≤30₽, норма ≤50₽, ВЫШЕ 50₽ — кампания
  неэффективна, кандидат на отключение или переделку
- CTR (вторичная): хорошо >1%, норма >0.5%, ниже 0.5% — слабо
- CPC (вторичная): хорошо <15₽, норма <25₽
CPL — приоритет. Низкий CTR прощается, если CPL в норме (рекламу
оптимизируем по реальным обращениям, не по кликам).

Отвечай ВСЕГДА в формате JSON со схемой:
{
  "summary": "одна фраза с общей оценкой",
  "problems": ["проблема 1", "проблема 2"],
  "winners": ["что работает 1", "что работает 2"],
  "recommendations": [
    {"action": "pause", "campaign_id": 123, "reason": "..."},
    {"action": "scale", "campaign_id": 456, "new_budget": 500, "reason": "..."},
    {"action": "create_variation", "based_on": 456, "reason": "..."}
  ]
}

Никакого текста до или после JSON. Только валидный JSON.
"""


class ClaudeAnalyzer:
    """Анализатор метрик через Claude."""

    def __init__(self, api_key: str | None = None):
        self.client = AsyncAnthropic(api_key=api_key or settings.anthropic_api_key)
        self.model = settings.anthropic_model

    async def analyze_campaigns(self, stats_list: list[CampaignStats]) -> dict:
        """Анализирует список метрик и возвращает рекомендации."""
        if not stats_list:
            return {
                "summary": "Нет активных кампаний",
                "problems": [],
                "winners": [],
                "recommendations": [],
            }

        # Сериализуем метрики в человекочитаемый формат
        metrics_text = self._format_metrics(stats_list)

        message = await self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Метрики за последние сутки:\n\n{metrics_text}",
                }
            ],
        )

        raw = message.content[0].text  # type: ignore
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error(f"Не удалось распарсить ответ Claude как JSON: {e}\nRaw: {raw}")
            return {
                "summary": "Ошибка анализа",
                "problems": [f"Не удалось распарсить ответ Claude: {e}"],
                "winners": [],
                "recommendations": [],
            }

    @staticmethod
    def _format_metrics(stats_list: list[CampaignStats]) -> str:
        lines = []
        for s in stats_list:
            lines.append(
                f"- Кампания #{s.campaign_id}: показы {s.impressions}, "
                f"клики {s.clicks}, CTR {s.ctr:.2f}%, "
                f"расход {s.spent_rub:.0f}₽, заявки {s.leads}, "
                f"CPL {s.cpl_rub:.0f}₽"
            )
        return "\n".join(lines)
