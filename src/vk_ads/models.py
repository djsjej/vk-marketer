"""Pydantic модели для VK Ads сущностей. Заполняются по мере имплементации API."""

from datetime import datetime
from pydantic import BaseModel, Field


class Campaign(BaseModel):
    """Рекламная кампания (Ad Plan в новом VK API)."""

    id: int
    name: str
    status: str  # active, paused, deleted
    daily_budget_rub: int
    total_spent_rub: float = 0.0
    created_at: datetime | None = None


class AdGroup(BaseModel):
    """Группа объявлений внутри кампании."""

    id: int
    campaign_id: int
    name: str
    status: str
    targeting: dict = Field(default_factory=dict)


class Banner(BaseModel):
    """Объявление (креатив с текстом и картинкой)."""

    id: int
    adgroup_id: int
    title: str
    description: str
    image_id: str
    url: str
    status: str  # active, paused, rejected, moderation


class CampaignStats(BaseModel):
    """Метрики кампании за период."""

    campaign_id: int
    impressions: int = 0
    clicks: int = 0
    spent_rub: float = 0.0
    leads: int = 0

    @property
    def ctr(self) -> float:
        """CTR в процентах."""
        return (self.clicks / self.impressions * 100) if self.impressions else 0.0

    @property
    def cpl_rub(self) -> float:
        """Cost per lead в рублях."""
        return self.spent_rub / self.leads if self.leads else 0.0

    @property
    def cpm_rub(self) -> float:
        """Cost per mille (1000 показов) в рублях."""
        return (self.spent_rub / self.impressions * 1000) if self.impressions else 0.0

    @property
    def cpc_rub(self) -> float:
        """Cost per click в рублях."""
        return self.spent_rub / self.clicks if self.clicks else 0.0


def aggregate_stats_item(item: dict) -> CampaignStats | None:
    """Парсит один элемент `items[]` из ответа VK Ads Statistics API
    в CampaignStats, суммируя метрики по дням.

    Реальная структура ответа VK на `/statistics/{ad_plans|ad_groups|banners}/day.json`:

        {
            "items": [{
                "id": <int>,
                "rows": [
                    {"date": "YYYY-MM-DD",
                     "base":    {"shows": int, "clicks": int, "spent": str, ...},
                     "events":  {"joinings": int, "likes": int, ...},
                     "uniques": {...}},
                    ...одна строка на каждый день периода...
                ]
            }]
        }

    Метрики `shows`/`clicks`/`spent` лежат внутри `base`, а целевые действия
    для socialengagement (вступления) — внутри `events.joinings`.

    До 17.05.2026 ручной /watchdog в `handlers.py` и автосторож в
    `scheduler/jobs.py` искали эти поля на верхнем уровне `total`
    (`total.shows`, `total.clicks`, `total.goals`). VK таких полей не
    возвращает — поэтому все 12 крутящихся кампаний попадали в
    «🕐 Ещё нет данных» хотя реально показывались. Третья реализация
    в `src/agents/boba_tools.py` парсила правильно (через `rows[].base`)
    — отсюда и взяли формулу. Источник структуры — `docs/VK_STATISTICS_API.md`.

    Args:
        item: один словарь из `items[]` ответа VK.

    Returns:
        CampaignStats или None если у элемента нет id или нет rows
        (тогда вызывающий должен записать кампанию в «нет данных»).
    """
    cid_raw = item.get("id", 0)
    try:
        cid = int(cid_raw) if cid_raw else 0
    except (TypeError, ValueError):
        return None
    if not cid:
        return None

    rows = item.get("rows") or []
    if not rows:
        return None

    total_shows = 0
    total_clicks = 0
    total_spent = 0.0
    total_joinings = 0
    for r in rows:
        base = r.get("base") or {}
        events = r.get("events") or {}
        total_shows += int(base.get("shows", 0) or 0)
        total_clicks += int(base.get("clicks", 0) or 0)
        # spent в API приходит строкой ("123.45") — приводим аккуратно.
        try:
            total_spent += float(base.get("spent", 0) or 0)
        except (TypeError, ValueError):
            pass
        total_joinings += int(events.get("joinings", 0) or 0)

    return CampaignStats(
        campaign_id=cid,
        impressions=total_shows,
        clicks=total_clicks,
        spent_rub=total_spent,
        leads=total_joinings,
    )
