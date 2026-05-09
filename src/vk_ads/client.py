"""VK Ads API клиент (новый API ads.vk.com).

Базовая структура. Реальные эндпоинты заполняются в Claude Code сессии
с подключённым Direct MCP или ручной реализацией по доке VK Рекламы.

ВАЖНО: Создание объявления — это 3 последовательных запроса:
1. POST /api/v2/banners/upload_url — получить URL для загрузки
2. POST upload_url с multipart/form-data — загрузить картинку, получить image_id
3. POST /api/v2/banners — создать объявление с этим image_id
4. POST /api/v2/ads — связать banner с adgroup

См. src/vk_ads/upload.py для multipart-загрузки.
См. .claude/skills/vk-ads/SKILL.md для подробностей.
"""

import logging
from typing import Any

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

VK_ADS_BASE = "https://ads.vk.com/api/v2"


class VKAdsClient:
    """Клиент для работы с VK Ads API."""

    def __init__(self, token: str | None = None, account_id: int | None = None):
        self.token = token or settings.vk_ads_token
        self.account_id = account_id or settings.vk_ads_account_id
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "VKAdsClient":
        self._client = httpx.AsyncClient(
            base_url=VK_ADS_BASE,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()

    async def get_campaigns(self) -> list[dict]:
        """Получить список кампаний."""
        # TODO: реализовать GET /api/v2/ad_plans
        raise NotImplementedError("TODO: implement get_campaigns")

    async def get_campaign_stats(
        self, campaign_id: int, date_from: str, date_to: str
    ) -> dict:
        """Получить метрики кампании за период."""
        # TODO: реализовать GET /api/v2/statistics/...
        raise NotImplementedError("TODO: implement get_campaign_stats")

    async def create_campaign(self, name: str, daily_budget: int) -> int:
        """Создать новую кампанию. Возвращает campaign_id."""
        # TODO: реализовать POST /api/v2/ad_plans
        raise NotImplementedError("TODO: implement create_campaign")

    async def create_ad_with_image(
        self,
        adgroup_id: int,
        image_path: str,
        title: str,
        description: str,
        url: str,
    ) -> int:
        """Создать объявление с картинкой. 3-шаговый процесс.

        1. Получить upload_url
        2. Загрузить картинку через multipart (см. upload.py)
        3. Создать banner с полученным image_id
        4. Прикрепить banner к adgroup

        Возвращает ad_id.
        """
        # TODO: реализовать полный цикл
        raise NotImplementedError("TODO: implement create_ad_with_image")

    async def pause_ad(self, ad_id: int) -> None:
        """Поставить объявление на паузу."""
        # TODO: реализовать
        raise NotImplementedError("TODO: implement pause_ad")

    async def resume_ad(self, ad_id: int) -> None:
        """Возобновить объявление."""
        # TODO: реализовать
        raise NotImplementedError("TODO: implement resume_ad")

    async def update_budget(self, campaign_id: int, daily_budget: int) -> None:
        """Изменить дневной бюджет кампании."""
        # TODO: реализовать
        raise NotImplementedError("TODO: implement update_budget")
