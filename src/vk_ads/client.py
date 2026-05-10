"""VK Ads API клиент.

Использует OAuth-аутентификатор для автоматического получения и обновления
access_token. Endpoints на ads.vk.com (VK Реклама API под капотом — myTarget).

ВАЖНО: Создание объявления = 3 последовательных запроса:
1. POST /api/v2/content/static.json — получить upload_url
2. POST upload_url с multipart/form-data — загрузить картинку, получить content_id
3. POST /api/v2/banners.json — создать объявление с этим content_id

См. src/vk_ads/upload.py для multipart-загрузки.
См. .claude/skills/vk-ads/SKILL.md для подробностей.

Документация: https://ads.vk.com/help
"""

import logging
from typing import Any

import httpx

from src.config import settings
from src.vk_ads.auth import VKAdsAuthenticator

logger = logging.getLogger(__name__)

VK_API_BASE = "https://ads.vk.com/api/v2"


class VKAdsAPIError(Exception):
    """Ошибка VK Ads API."""


class VKAdsClient:
    """Клиент VK Ads API.

    Поддерживает два режима:
    1. OAuth (рекомендуется): создаётся через `VKAdsClient.from_settings()`,
       автоматически обновляет токен при истечении.
    2. Статический токен: для отладки или legacy-сценариев — `VKAdsClient(static_token="...")`

    Использование:
        client = VKAdsClient.from_settings()
        info = await client.get_account_info()
        campaigns = await client.get_campaigns()
    """

    def __init__(
        self,
        authenticator: VKAdsAuthenticator | None = None,
        static_token: str | None = None,
        account_id: int | None = None,
    ):
        if authenticator is None and not static_token:
            raise ValueError(
                "Нужно передать либо authenticator (OAuth), либо static_token"
            )
        self.auth = authenticator
        self.static_token = static_token
        self.account_id = account_id

    @classmethod
    def from_settings(cls) -> "VKAdsClient | None":
        """Создаёт клиент из настроек env, если возможно. Иначе None."""
        if settings.has_vk_oauth:
            assert settings.vk_ads_oauth_client_id is not None
            assert settings.vk_ads_oauth_client_secret is not None
            auth = VKAdsAuthenticator(
                client_id=settings.vk_ads_oauth_client_id,
                client_secret=settings.vk_ads_oauth_client_secret,
            )
            return cls(authenticator=auth, account_id=settings.vk_ads_account_id)

        if settings.has_vk_static_token:
            return cls(
                static_token=settings.vk_ads_token,
                account_id=settings.vk_ads_account_id,
            )

        logger.warning(
            "VK Ads клиент не настроен — нет ни OAuth-credentials, ни static_token"
        )
        return None

    async def _get_token(self) -> str:
        """Возвращает свежий access_token (через OAuth или static)."""
        if self.auth is not None:
            return await self.auth.get_access_token()
        assert self.static_token is not None
        return self.static_token

    async def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> Any:
        """Универсальный запрос с автоматической авторизацией.

        Args:
            method: HTTP метод (GET, POST, и т.д.)
            path: путь от VK_API_BASE (например '/users/current.json')
            params: query параметры
            json_body: JSON тело (для POST/PUT)

        Returns:
            Распарсенный JSON ответа

        Raises:
            VKAdsAPIError: на любой не-2xx ответ
        """
        token = await self._get_token()
        url = f"{VK_API_BASE}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        logger.debug(f"VK API {method} {path} params={params}")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method, url, params=params, json=json_body, headers=headers
            )

        if response.status_code == 401 and self.auth is not None:
            # Токен мог стухнуть — сбросим кэш и повторим один раз
            logger.warning("Получили 401 — сбрасываю кэш токена и повторяю запрос")
            self.auth.invalidate()
            token = await self._get_token()
            headers["Authorization"] = f"Bearer {token}"
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.request(
                    method, url, params=params, json=json_body, headers=headers
                )

        if response.status_code >= 400:
            raise VKAdsAPIError(
                f"VK API {method} {path} вернул {response.status_code}: "
                f"{response.text[:500]}"
            )

        try:
            return response.json()
        except ValueError as e:
            raise VKAdsAPIError(f"VK вернул не JSON: {response.text[:300]}") from e

    # ------------------------------------------------------------------
    # Чтение: информация об аккаунте, баланс, кампании
    # ------------------------------------------------------------------

    async def get_account_info(self) -> dict:
        """Информация о текущем аккаунте: ФИО, баланс, статус.

        GET /users/current.json
        """
        return await self._request("GET", "/users/current.json")

    async def get_balance(self) -> float | None:
        """Баланс кабинета в рублях. None если не удалось распарсить.

        Удобный шорткат — баланс лежит внутри get_account_info().
        """
        try:
            info = await self.get_account_info()
            # В разных версиях API поле называется по-разному:
            # client_info.balance, balance, account.balance
            for path in [
                ("client_info", "balance"),
                ("balance",),
                ("account", "balance"),
            ]:
                value: Any = info
                for key in path:
                    if isinstance(value, dict) and key in value:
                        value = value[key]
                    else:
                        value = None
                        break
                if value is not None:
                    return float(value)
            logger.warning(f"Не нашёл поле баланса в ответе: ключи={list(info.keys())}")
            return None
        except (VKAdsAPIError, ValueError, TypeError) as e:
            logger.error(f"Не удалось получить баланс: {e}")
            return None

    async def get_campaigns(
        self, limit: int = 50, offset: int = 0, status: str | None = None
    ) -> list[dict]:
        """Список рекламных кампаний.

        GET /campaigns.json

        Args:
            limit: сколько вернуть (макс 50 за раз)
            offset: смещение для пагинации
            status: фильтр по статусу ('active', 'blocked', 'deleted', 'all')

        Returns:
            Список словарей кампаний с полями id, name, status, budget_limit_day, ...
        """
        params: dict[str, Any] = {
            "limit": min(limit, 50),
            "offset": offset,
            "fields": "id,name,status,budget_limit_day,budget_limit,objective,created,updated",
        }
        if status:
            params["status"] = status

        result = await self._request("GET", "/campaigns.json", params=params)
        if isinstance(result, dict):
            return result.get("items", [])
        if isinstance(result, list):
            return result
        return []

    async def get_campaign_stats(
        self,
        campaign_ids: list[int],
        date_from: str,
        date_to: str,
        metrics: str = "base",
    ) -> dict:
        """Метрики кампаний за период.

        GET /statistics/campaigns/{ids}/day.json?date_from=...&date_to=...

        Args:
            campaign_ids: список ID кампаний
            date_from: начало периода в формате YYYY-MM-DD
            date_to: конец периода YYYY-MM-DD
            metrics: 'base' (показы, клики, расход) или 'all'
        """
        if not campaign_ids:
            return {"items": []}
        ids_str = ";".join(str(cid) for cid in campaign_ids)
        return await self._request(
            "GET",
            f"/statistics/campaigns/{ids_str}/day.json",
            params={"date_from": date_from, "date_to": date_to, "metrics": metrics},
        )

    # ------------------------------------------------------------------
    # Запись: создание/изменение — TODO Phase 3
    # ------------------------------------------------------------------

    async def create_campaign(self, name: str, daily_budget_rub: int) -> int:
        """Создать кампанию. Возвращает campaign_id.

        ВАЖНО: VK хранит бюджеты в копейках. Передаём rub * 100.
        """
        raise NotImplementedError("Создание кампаний — Phase 3")

    async def create_ad_with_image(
        self,
        adgroup_id: int,
        image_path: str,
        title: str,
        description: str,
        url: str,
    ) -> int:
        """3-шаговый процесс создания объявления с картинкой."""
        raise NotImplementedError("Создание объявлений — Phase 3")

    async def pause_ad(self, ad_id: int) -> None:
        raise NotImplementedError("Pause — Phase 3")

    async def resume_ad(self, ad_id: int) -> None:
        raise NotImplementedError("Resume — Phase 3")

    async def update_budget(self, campaign_id: int, daily_budget_rub: int) -> None:
        raise NotImplementedError("Update budget — Phase 3")
