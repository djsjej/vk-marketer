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
