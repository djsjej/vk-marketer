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
    #
    # ВАЖНО: новый кабинет VK Ads (на ads.vk.com, тип Advertiser) использует
    # другие эндпоинты чем старый myTarget legacy:
    # - Кампании: /ad_plans.json (НЕ /campaigns.json)
    # - Группы: /ad_groups.json
    # - Статистика: /statistics/ad_groups/day.json или /statistics/ad_plans/day.json
    # - Баланс: эндпоинт не задокументирован, либо его нет для direct-кабинетов
    # ------------------------------------------------------------------

    async def get_balance(self) -> float | None:
        """Баланс кабинета в рублях. None если не удалось распарсить.

        Для новых кабинетов VK Ads (тип Advertiser) общедоступного эндпоинта
        для баланса не задокументировано — он отображается только в UI.
        Пробуем агентский эндпоинт `/agency/clients.json` на случай если
        кабинет относится к агентству. Если ничего не нашли — возвращаем None
        без шума в логах.
        """
        try:
            result = await self._request("GET", "/agency/clients.json")
            items = (
                result.get("items", [])
                if isinstance(result, dict)
                else (result if isinstance(result, list) else [])
            )
            if items:
                logger.info(
                    f"VK /agency/clients.json items[0] keys: "
                    f"{list(items[0].keys()) if isinstance(items[0], dict) else type(items[0])}"
                )
                if balance := self._extract_balance_from_dict(items[0]):
                    return balance
        except VKAdsAPIError:
            # Direct cabinets don't have agency endpoint — это норма
            pass

        logger.info("Баланс через API недоступен (для новых кабинетов это норма)")
        return None

    @staticmethod
    def _extract_balance_from_dict(info: Any) -> float | None:
        """Ищем баланс по списку известных путей в произвольном dict."""
        if not isinstance(info, dict):
            return None
        paths = [
            ("account_balance",),
            ("balance",),
            ("client_info", "balance"),
            ("client_info", "account_balance"),
            ("account", "balance"),
            ("account", "account_balance"),
            ("wallet", "balance"),
            ("info", "balance"),
        ]
        for path in paths:
            value: Any = info
            for key in path:
                if isinstance(value, dict) and key in value:
                    value = value[key]
                else:
                    value = None
                    break
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return None

    async def get_campaigns(
        self, limit: int = 50, offset: int = 0, status: str | None = None
    ) -> list[dict]:
        """Список рекламных кампаний (ad_plans).

        GET /ad_plans.json — для нового кабинета VK Ads.
        Старый легаси-эндпоинт /campaigns.json не работает в новом кабинете.

        Args:
            limit: сколько вернуть (макс 50 за раз)
            offset: смещение для пагинации
            status: фильтр по статусу ('active', 'blocked', 'deleted')
        """
        params: dict[str, Any] = {
            "limit": min(limit, 50),
            "offset": offset,
            "fields": "id,name,status,objective,date_start,date_end,budget_limit_day,budget_limit",
        }
        if status:
            params["status"] = status

        result = await self._request("GET", "/ad_plans.json", params=params)
        if isinstance(result, dict):
            return result.get("items", [])
        if isinstance(result, list):
            return result
        return []

    async def get_ad_groups(
        self,
        limit: int = 100,
        offset: int = 0,
        ad_plan_id: int | None = None,
    ) -> list[dict]:
        """Список групп объявлений (ad_groups). Опционально фильтр по кампании.

        GET /ad_groups.json
        """
        params: dict[str, Any] = {
            "limit": min(limit, 100),
            "offset": offset,
            "fields": "id,ad_plan_id,name,status,targetings,banners,delivery,budget_limit_day",
        }
        if ad_plan_id:
            params["_ad_plan_id"] = ad_plan_id  # фильтр в новом API через _-префикс

        result = await self._request("GET", "/ad_groups.json", params=params)
        if isinstance(result, dict):
            return result.get("items", [])
        if isinstance(result, list):
            return result
        return []

    async def get_ad_groups_stats(
        self,
        date_from: str,
        date_to: str,
        ad_group_ids: list[int] | None = None,
        metrics: str = "all",
    ) -> dict:
        """Метрики групп объявлений за период.

        GET /statistics/ad_groups/day.json?date_from=...&date_to=...&metrics=all
        Возвращает items[].rows[] — данные по дням.

        Args:
            date_from: YYYY-MM-DD
            date_to: YYYY-MM-DD
            ad_group_ids: если не задано — возвращает по всем группам кабинета
            metrics: 'all' / 'base' / 'uniques' / etc.
        """
        params: dict[str, Any] = {
            "date_from": date_from,
            "date_to": date_to,
            "metrics": metrics,
        }
        if ad_group_ids:
            params["id"] = ",".join(str(i) for i in ad_group_ids)
        return await self._request("GET", "/statistics/ad_groups/day.json", params=params)

    async def get_campaign_stats(
        self,
        campaign_ids: list[int],
        date_from: str,
        date_to: str,
        metrics: str = "all",
    ) -> dict:
        """Метрики кампаний (ad_plans) за период.

        GET /statistics/ad_plans/day.json?date_from=...&date_to=...&id=1,2,3&metrics=all
        """
        if not campaign_ids:
            return {"items": []}
        return await self._request(
            "GET",
            "/statistics/ad_plans/day.json",
            params={
                "id": ",".join(str(cid) for cid in campaign_ids),
                "date_from": date_from,
                "date_to": date_to,
                "metrics": metrics,
            },
        )

    # account_info как public method больше не имеет смысла —
    # /users/current.json не существует в новом кабинете.
    async def get_account_info(self) -> dict:
        """DEPRECATED: эндпоинт /users/current.json удалён в новом кабинете VK Ads.

        Оставлен для совместимости с тестами, всегда падает с 404.
        """
        return await self._request("GET", "/users/current.json")

    # ------------------------------------------------------------------
    # Загрузка контента (картинок) — отдельный модуль upload.py,
    # но клиент даёт удобный шорткат
    # ------------------------------------------------------------------

    async def upload_image(
        self,
        image_bytes: bytes,
        filename: str = "image.jpg",
        mime_type: str | None = None,
    ) -> int:
        """Загрузить картинку в VK Ads → возвращает content_id.

        Шорткат к src.vk_ads.upload.upload_image_bytes — берёт токен из клиента.
        """
        from src.vk_ads.upload import upload_image_bytes

        token = await self._get_token()
        result = await upload_image_bytes(
            access_token=token,
            image_bytes=image_bytes,
            filename=filename,
            mime_type=mime_type,
        )
        return int(result["id"])

    # ------------------------------------------------------------------
    # Создание кампаний и групп — VK Ads API v2 структура:
    # /ad_plans.json — кампания (контейнер)
    #   └── /ad_groups.json — группа объявлений (с таргетингом)
    #         └── /banners.json — объявления (с креативом)
    #
    # Можно создавать всё одним POST на /ad_plans.json (с вложенными
    # ad_groups и banners), либо по отдельности. По доке VK Ads быстрый старт
    # рекомендует одним запросом.
    # ------------------------------------------------------------------

    async def create_ad_plan(self, payload: dict) -> dict:
        """Создать рекламную кампанию (ad_plan).

        POST /ad_plans.json

        Payload может содержать вложенные ad_groups (а те — banners),
        тогда всё создастся за один запрос.

        Returns:
            Полный словарь созданного ad_plan, включая id и id вложенных групп/баннеров.
        """
        return await self._request("POST", "/ad_plans.json", json_body=payload)

    async def create_ad_group(self, payload: dict) -> dict:
        """Создать группу объявлений отдельно (если кампания уже есть).

        POST /ad_groups.json. Должно содержать ad_plan_id.
        """
        return await self._request("POST", "/ad_groups.json", json_body=payload)

    async def create_banner(self, payload: dict) -> dict:
        """Создать одиночное объявление в существующей группе.

        POST /banners.json. Должно содержать ad_group_id и content (id картинки).
        """
        return await self._request("POST", "/banners.json", json_body=payload)

    # ------------------------------------------------------------------
    # Управление статусом — Phase 4
    # ------------------------------------------------------------------

    async def pause_ad_plan(self, ad_plan_id: int) -> dict:
        """Поставить кампанию на паузу. POST /ad_plans/{id}.json со status='blocked'."""
        return await self._request(
            "POST",
            f"/ad_plans/{ad_plan_id}.json",
            json_body={"status": "blocked"},
        )

    async def resume_ad_plan(self, ad_plan_id: int) -> dict:
        """Возобновить. status='active'."""
        return await self._request(
            "POST",
            f"/ad_plans/{ad_plan_id}.json",
            json_body={"status": "active"},
        )

    async def update_ad_plan_budget(
        self, ad_plan_id: int, daily_budget_rub: int | None = None,
        total_budget_rub: int | None = None,
    ) -> dict:
        """Изменить дневной/общий бюджет.

        VK хранит бюджеты в рублях (не копейках) для нового API ad_plans.
        Подтверждено в прошлых рабочих интеграциях.
        """
        body: dict = {}
        if daily_budget_rub is not None:
            body["budget_limit_day"] = daily_budget_rub
        if total_budget_rub is not None:
            body["budget_limit"] = total_budget_rub
        if not body:
            raise ValueError("Нужно передать хотя бы один из бюджетов")
        return await self._request(
            "POST", f"/ad_plans/{ad_plan_id}.json", json_body=body
        )

    # ------------------------------------------------------------------
    # Старые stub'ы — оставляем для совместимости, но реализуем через новые
    # ------------------------------------------------------------------

    async def create_campaign(self, name: str, daily_budget_rub: int) -> int:
        """Минимальное создание кампании без объявлений (для тестов).

        Для реальной работы используй create_ad_plan() с полным payload
        или AdCreator orchestrator из src.services.ad_creator.
        """
        result = await self.create_ad_plan({
            "name": name,
            "status": "active",
            "budget_limit_day": daily_budget_rub,
            "objective": "socialengagement",
        })
        return int(result["id"])

    async def create_ad_with_image(
        self,
        adgroup_id: int,
        image_path: str,
        title: str,
        description: str,
        url: str,
    ) -> int:
        """3-шаговый процесс создания объявления с картинкой.

        DEPRECATED: используй AdCreator из src.services.ad_creator,
        он умеет в age splits и Claude-копирайтинг.
        """
        from src.vk_ads.upload import upload_image_file

        token = await self._get_token()
        upload_result = await upload_image_file(token, image_path)
        content_id = upload_result["id"]

        banner = await self.create_banner({
            "ad_group_id": adgroup_id,
            "name": title[:60],
            "urls": {"primary": {"url": url}},
            "textblocks": {
                "title_40_vkads": {"text": title[:40]},
                "text_2000": {"text": description[:2000]},
            },
            "content": {"image_600x600": {"id": content_id}},
        })
        return int(banner["id"])

    async def pause_ad(self, ad_id: int) -> None:
        """DEPRECATED — используй pause_ad_plan."""
        await self.pause_ad_plan(ad_id)

    async def resume_ad(self, ad_id: int) -> None:
        """DEPRECATED — используй resume_ad_plan."""
        await self.resume_ad_plan(ad_id)

    async def update_budget(self, campaign_id: int, daily_budget_rub: int) -> None:
        """DEPRECATED — используй update_ad_plan_budget."""
        await self.update_ad_plan_budget(campaign_id, daily_budget_rub=daily_budget_rub)
