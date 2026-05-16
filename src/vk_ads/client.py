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
    """Ошибка VK Ads API.

    Хранит диагностические данные для тикетов в поддержку VK:
    - status_code: HTTP-код ответа
    - request_id: значение x-request-id из response headers (поддержка просит
      именно его чтобы найти запрос в их логах)
    - request_time: дата/время ответа в ISO 8601 UTC формате
    - body: первые 500 символов тела ответа
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        request_time: str | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.request_time = request_time
        self.body = body

    def diag_summary(self) -> str:
        """Краткая diag-сводка для пересылки в поддержку VK."""
        parts = []
        if self.status_code is not None:
            parts.append(f"HTTP {self.status_code}")
        if self.request_id:
            parts.append(f"x-request-id: {self.request_id}")
        if self.request_time:
            parts.append(f"time: {self.request_time}")
        return " | ".join(parts) if parts else "(нет диагностических данных)"


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
        api_version: str = "v2",
    ) -> Any:
        """Универсальный запрос с автоматической авторизацией.

        Args:
            method: HTTP метод (GET, POST, и т.д.)
            path: путь от base (например '/users/current.json')
            params: query параметры
            json_body: JSON тело (для POST/PUT)
            api_version: 'v2' (default) или 'v3' — для statistics v3

        Returns:
            Распарсенный JSON ответа

        Raises:
            VKAdsAPIError: на любой не-2xx ответ
        """
        token = await self._get_token()
        base = (
            VK_API_BASE
            if api_version == "v2"
            else VK_API_BASE.replace("/api/v2", f"/api/{api_version}")
        )
        url = f"{base}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        logger.debug(f"VK API {method} {path} (v{api_version}) params={params}")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method, url, params=params, json=json_body, headers=headers
            )

        if response.status_code == 401 and self.auth is not None:
            # Токен мог стухнуть — сбросим кэш и повторим один раз
            logger.warning("Получили 401 — сбрасываю кэш токена и повторяю запрос")
            await self.auth.invalidate()
            token = await self._get_token()
            headers["Authorization"] = f"Bearer {token}"
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.request(
                    method, url, params=params, json=json_body, headers=headers
                )

        if response.status_code >= 400:
            # Извлекаем x-request-id и время — поддержка VK просит именно
            # эти данные чтобы найти запрос в их серверных логах.
            req_id = (
                response.headers.get("x-request-id")
                or response.headers.get("X-Request-Id")
                or response.headers.get("request-id")
            )
            # Время: предпочитаем Date header от VK сервера; fallback на наш UTC.
            from datetime import datetime, timezone
            date_hdr = response.headers.get("date") or response.headers.get("Date")
            req_time = date_hdr or datetime.now(timezone.utc).isoformat()

            body_text = response.text[:500]
            logger.error(
                "VK API error %s on %s %s | x-request-id=%s | time=%s | body=%s",
                response.status_code, method, path, req_id, req_time, body_text,
            )
            raise VKAdsAPIError(
                f"VK API {method} {path} вернул {response.status_code}: {body_text}",
                status_code=response.status_code,
                request_id=req_id,
                request_time=req_time,
                body=body_text,
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

    async def get_or_register_url(self, url: str) -> dict:
        """Регистрирует URL/сообщество в кабинете VK Ads и возвращает его внутренний ID.

        Это критический шаг для создания кампаний с objective='socialengagement':
        VK хочет в `ad_object_id` НЕ численный VK community ID (типа 216409501),
        а внутренний URL-ID кабинета (типа 5439821), который выдаётся при
        первом обращении к /api/v1/urls/?url=...

        Сначала пробуем v1 GET (старый рабочий способ из n8n-кампаний).
        Если v1 не сработал — POST /api/v2/urls.json (новый способ для нового кабинета).

        Args:
            url: полный URL сообщества (https://vk.com/pomolimsy или
                 https://vk.com/club216409501).

        Returns:
            Dict с полем `id` — внутренний URL-ID кабинета.

        Raises:
            VKAdsAPIError: если оба способа не сработали.
        """
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}

        # Попытка 1: GET /api/v1/urls/?url=...
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    "https://ads.vk.com/api/v1/urls/",
                    headers=headers,
                    params={"url": url},
                )
            logger.info(
                f"V1 URL register: status={response.status_code}, "
                f"body={response.text[:500]}"
            )
            if response.status_code == 200:
                try:
                    data = response.json()
                    if isinstance(data, dict) and "id" in data:
                        logger.info(
                            f"✅ URL '{url}' зарегистрирован v1, "
                            f"id={data['id']}, url_object_id={data.get('url_object_id')}"
                        )
                        return data
                except ValueError:
                    pass
        except httpx.RequestError as e:
            logger.warning(f"V1 URL register сетевая ошибка: {e}")

        # Попытка 2: POST /api/v2/urls.json с body {"url": "..."}
        logger.info("V1 не сработал, пробую V2 POST /urls.json")
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    "https://ads.vk.com/api/v2/urls.json",
                    headers={**headers, "Content-Type": "application/json"},
                    json={"url": url},
                )
            logger.info(
                f"V2 URL register: status={response.status_code}, "
                f"body={response.text[:500]}"
            )
            if response.status_code in (200, 201):
                try:
                    data = response.json()
                    if isinstance(data, dict) and "id" in data:
                        logger.info(
                            f"✅ URL '{url}' зарегистрирован v2, id={data['id']}"
                        )
                        return data
                except ValueError:
                    pass
        except httpx.RequestError as e:
            logger.warning(f"V2 URL register сетевая ошибка: {e}")

        raise VKAdsAPIError(
            f"Не смог зарегистрировать URL '{url}' ни через v1, ни через v2. "
            f"Проверь что URL валидный и доступен."
        )

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

    async def get_ad_plan_raw(self, ad_plan_id: int | str) -> dict | None:
        """Полная сырая структура одной кампании (ad_plan) с максимумом полей.

        Используется для диагностики — посмотреть как реально выглядит
        кампания, созданная вручную через UI кабинета, чтобы сверить
        нашу сборку payload в /services/ad_creator.py.

        GET /ad_plans.json?_id=<id>&fields=...

        Список полей — из allowed списка, который VK сам присылает в
        unknown_resource_fields ошибке. Добавили `campaigns` чтобы получить
        nested структуру ad_groups одним запросом.
        """
        params: dict[str, Any] = {
            "_id": str(ad_plan_id),
            "fields": (
                "id,name,status,objective,ad_object_type,ad_object_id,"
                "date_start,date_end,budget_limit_day,budget_limit,"
                "autobidding_mode,delivery,created,campaigns,ad_groups,"
                "budget_optimization_enabled,efficiency_status"
            ),
        }
        result = await self._request("GET", "/ad_plans.json", params=params)
        if isinstance(result, dict):
            items = result.get("items") or []
            return items[0] if items else None
        if isinstance(result, list) and result:
            return result[0]
        return None

    async def get_banners_raw(
        self, ad_group_id: int | None = None, limit: int = 50
    ) -> list[dict]:
        """Список объявлений (banners) с максимумом полей, включая patterns.

        Используется для диагностики — увидеть structure banner'а живой
        кампании (где у patterns, какие там значения и т.п.).

        GET /banners.json?fields=...
        """
        params: dict[str, Any] = {
            "limit": min(limit, 50),
            "fields": (
                "id,ad_group_id,ad_plan_id,campaign_id,name,status,"
                "content,content_settings,call_to_action,blocked_patterns,"
                "delivery,category,deeplink,created,moderation_status,"
                "moderation_reasons"
            ),
        }
        if ad_group_id:
            params["_ad_group_id"] = ad_group_id
        result = await self._request("GET", "/banners.json", params=params)
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
            "fields": (
                "id,ad_plan,ad_plan_id,name,status,targetings,banners,"
                "delivery,budget_limit_day,budget_limit,age_restrictions,"
                "autobidding_mode,date_start,date_end,created"
            ),
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

    # --- v3 Statistics API (для команды /analyze и Phase 4) ---
    # См. docs/VK_STATISTICS_API.md
    # Главные отличия от v2:
    #  - параметр `fields` вместо `metrics` (тот же набор: base, events, etc.)
    #  - поддержка пагинации (limit, offset)
    #  - сортировка через sort_by
    #  - фильтры по статусам (banner_status, ad_group_status)

    async def get_statistics_v3(
        self,
        *,
        object_type: str,  # 'ad_plans' | 'ad_groups' | 'banners' | 'users'
        ids: list[int],
        date_from: str,
        date_to: str | None = None,
        fields: str = "base,events,uniques,social_network",
        attribution: str = "conversion",
        sort_by: str | None = None,
        sort_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """v3 Statistics API — основной метод для аналитики.

        GET /api/v3/statistics/{object_type}/day.json

        Args:
            object_type: 'ad_plans', 'ad_groups', 'banners', 'users'
            ids: список ID объектов (макс 200)
            date_from: YYYY-MM-DD (обязательно)
            date_to: YYYY-MM-DD (по умолчанию текущая дата)
            fields: наборы метрик через запятую: 'base', 'events',
                'uniques', 'social_network', 'video', 'all', etc.
                Для нашего use case (socialengagement, package 3122) дефолт
                'base,events,uniques,social_network' покрывает CTR, CPL,
                joinings, vk_join, охват.
            attribution: 'conversion' (default) или 'impression'
            sort_by: напр. 'base.clicks' или 'base.ctr'
            sort_dir: 'asc' | 'desc'
            limit: макс 250
            offset: для пагинации

        Returns:
            {"items": [{"id": ..., "total": {"base": {...}, ...}}, ...],
             "total": {...},  # суммарно по всем
             "limit": 50, "offset": 0, "count": N}

        Raises:
            VKAdsAPIError на 4xx/5xx (включая ERR_LIMIT_EXCEEDED если >200 ids).
        """
        if object_type not in ("ad_plans", "ad_groups", "banners", "users"):
            raise ValueError(
                f"object_type должен быть ad_plans/ad_groups/banners/users, не {object_type}"
            )
        if len(ids) > 200:
            raise ValueError(f"максимум 200 объектов в запросе, не {len(ids)}")

        params: dict[str, Any] = {
            "id": ",".join(str(i) for i in ids),
            "date_from": date_from,
            "fields": fields,
            "attribution": attribution,
            "limit": limit,
            "offset": offset,
        }
        if date_to:
            params["date_to"] = date_to
        if sort_by:
            params["sort_by"] = sort_by
            params["d"] = sort_dir

        return await self._request(
            "GET",
            f"/statistics/{object_type}/day.json",
            params=params,
            api_version="v3",
        )

    async def get_faststat_v3(
        self,
        *,
        object_type: str,  # 'ad_plans' | 'banners' | 'campaigns' | 'users'
        ids: list[int],
    ) -> dict:
        """v3 Real-time статистика за последние 60 минут (поминутно).

        GET /api/v3/statistics/faststat/{object_type}.json

        Без учёта фильтрации некорректного трафика. Значения могут
        отличаться от итоговой статистики.

        Returns:
            {"last_seen_msg_time": {...},
             "<object_type>": {"<id>": {"timestamp": ..., "minutely": {
               "clicks": [60 значений], "shows": [60 значений]
             }}}}
        """
        return await self._request(
            "GET",
            f"/statistics/faststat/{object_type}.json",
            params={"id": ",".join(str(i) for i in ids)},
            api_version="v3",
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

        POST /ad_plans.json — единый POST со всей структурой:
        - Топ-уровень: name, status, objective, budget, ad_object_type/id, dates
        - `campaigns: [...]` — массив ad_groups (VK называет их "campaigns" внутри
          ad_plan по легаси-причинам). Каждый ad_group содержит свой banner.

        Returns:
            Полный словарь созданного ad_plan, включая id и id вложенных групп/баннеров.
        """
        return await self._request("POST", "/ad_plans.json", json_body=payload)

    async def create_ad_group(self, payload: dict) -> dict:
        """Создать группу объявлений отдельно.

        POST /ad_groups.json с payload **напрямую** (без обёртки batch).
        VK на старую обёртку `{"ad_groups": [payload]}` отвечает:
            unknown_resource_field: "Unknown fields: ad_groups"

        Payload должен содержать ad_plan_id. Можно передавать nested
        banners[] — это валидно по подтверждению поддержки VK May 2026.
        """
        response = await self._request(
            "POST", "/ad_groups.json", json_body=payload
        )
        # Ответ может быть как flat object с id, так и обёрткой — берём
        # либо сам объект, либо первый из ad_groups[].
        if isinstance(response, dict) and "ad_groups" in response:
            items = response["ad_groups"]
            return items[0] if items else response
        if isinstance(response, list) and response:
            return response[0]
        return response if isinstance(response, dict) else {}

    async def create_banner(self, payload: dict) -> dict:
        """Создать одиночное объявление.

        POST /banners.json с body `{"banners": [<payload>]}`. Payload должен
        содержать ad_group_id и content.
        """
        response = await self._request(
            "POST", "/banners.json", json_body={"banners": [payload]}
        )
        return self._unwrap_batch_response(response, key="banners")

    @staticmethod
    def _unwrap_batch_response(response: Any, key: str) -> dict:
        """Распаковывает batch-ответ VK Ads в первый элемент.

        Возможные форматы ответа:
        - {"campaigns": [{...}]} или {"ad_groups": [{...}]} итд
        - [{...}]
        - {"items": [{...}]}
        - {...} — уже распакованный (на всякий случай)
        """
        if isinstance(response, dict):
            for k in (key, "items"):
                items = response.get(k)
                if isinstance(items, list) and items:
                    return items[0]
            # Может прийти уже распакованный (с id в корне)
            if "id" in response:
                return response
        elif isinstance(response, list) and response:
            return response[0]
        raise VKAdsAPIError(
            f"Не смог распаковать batch-ответ VK для {key}: {response}"
        )

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

    # ============================================================
    # Phase 5.4 — Ремаркетинг: создание Users Lists из горячих ID
    # ============================================================
    # API: POST /api/v3/remarketing/users_lists.json
    # Документация (от Vizit'а 15.05.2026):
    # https://ads.vk.com/doc/api/resource/RemarketingUsersLists
    #
    # Особенности:
    # - Тело запроса — multipart/form-data, НЕ JSON
    # - Поля: file (текстовый файл с ID, по одному на строку),
    #         name (название), type='vk' (для VK user_id)
    # - Минимум 2000 ID, максимум 5 000 000
    # - В ответе — объект RemarketingUsersList со status='receiving'
    # - Статус нужно опросить через GET /api/v3/.../{id} пока не станет 'ready'

    async def create_audience_segment_from_users_list(
        self,
        name: str,
        users_list_id: int,
    ) -> dict:
        """Создать аудиторию (Segment) с привязкой к users_list одним POST.

        Phase 5.10 (16.05.2026): VK Ads разделяет на 2 объекта:
        - RemarketingUsersList — только список ID (через create_remarketing_users_list)
        - Segment — аудитория для targetings, может быть привязана к одному или
          нескольким users_lists/counters/lookalike

        В targetings.segments кампании идёт ID Segment, не users_list.
        Это была причина ошибки 'unallowed_value' при первом запуске smoke test
        16.05.2026 — мы передавали users_list_id вместо segment_id.

        Документация: https://ads.vk.com/doc/api/method/Segments/POST_segments
        Endpoint v2: /api/v2/remarketing/segments.json
        (v3 для segments не подтверждён, используем v2 как в доке)

        Args:
            name: название аудитории (отображается в UI кабинета во вкладке
                «Аудитории», не путать с «Списками пользователей»)
            users_list_id: ID существующего RemarketingUsersList (создан через
                create_remarketing_users_list). Сегмент будет содержать всех
                пользователей этого списка.

        Returns:
            dict с полями Segment: id, name, pass_condition, created, updated.
            Поле `id` — это и есть segment_id который идёт в targetings.segments
            кампании.

        Raises:
            VKAdsAPIError: на ошибки API (включая validation_failed,
                segment_limit_exceeded, unknown_source).
        """
        # pass_condition: 1 означает что пользователь должен быть в 1 из
        # связанных источников. У нас один users_list — этого достаточно.
        # Если бы привязывали несколько источников и хотели пересечение —
        # ставили бы pass_condition: N (число источников).
        payload = {
            "name": name,
            "pass_condition": 1,
            "relations": [
                {
                    "object_type": "remarketing_users_list",
                    "params": {
                        "source_id": users_list_id,
                        "type": "positive",  # включаем (не negative)
                    },
                }
            ],
        }

        token = await self._get_token()
        # API v2 для segments — подтверждено документацией от Vizit'а 16.05.2026.
        # Наш users_list создан через v3, но v2 segments принимает v3 user_list_id
        # (если внутри VK Ads это одна и та же база). Если не пройдёт — нужно
        # попробовать /api/v3/remarketing/segments.json.
        url = "https://ads.vk.com/api/v2/remarketing/segments.json"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        logger.info(
            f"VK Ads: создаю Segment '{name}' с привязкой к "
            f"users_list_id={users_list_id}"
        )

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=headers, json=payload)

        if response.status_code not in (200, 201):
            try:
                err_body = response.json()
            except Exception:
                err_body = {"raw": response.text}
            raise VKAdsAPIError(
                f"VK Ads segment create failed: HTTP "
                f"{response.status_code}, body={err_body}"
            )

        result = response.json()
        segment_id = int(result.get("id", 0))
        if not segment_id:
            raise VKAdsAPIError(
                f"VK Ads вернул сегмент без id: {result}"
            )

        logger.info(
            f"VK Ads: Segment создан, id={segment_id}, "
            f"name={result.get('name')}"
        )
        return result

    async def create_remarketing_users_list(
        self,
        name: str,
        user_ids: list[int],
        list_type: str = "vk",
    ) -> dict:
        """Создаёт список пользователей для ремаркетинга и сразу загружает ID.

        Args:
            name: название списка (отображается в UI кабинета)
            user_ids: ID пользователей VK (минимум 2000, максимум 5 000 000)
            list_type: тип списка. 'vk' для VK ID, 'phones' для телефонов,
                'emails' для email, и т.д. (см. документацию)

        Returns:
            dict с полями RemarketingUsersList объекта: id, status, name,
            entries_count, type, и т.д. После создания status='receiving',
            нужно ждать пока станет 'ready'.

        Raises:
            ValueError: если user_ids меньше 2000 или больше 5M
            VKAdsAPIError: на ошибки API (включая бизнес-ошибки вроде
                too_many_ids, not_enough_ids, invalid_uint)
        """
        if len(user_ids) < 2000:
            raise ValueError(
                f"VK Ads требует минимум 2000 ID для создания списка, "
                f"передано {len(user_ids)}. Запусти /vk_audience full или "
                f"добавь больших сообществ в seed."
            )
        if len(user_ids) > 5_000_000:
            raise ValueError(
                f"VK Ads ограничивает 5 000 000 ID, передано {len(user_ids)}."
            )

        # Файл — простой plain text с одним ID на строку
        file_content = "\n".join(str(uid) for uid in user_ids).encode("utf-8")

        token = await self._get_token()
        url = "https://ads.vk.com/api/v3/remarketing/users_lists.json"
        headers = {"Authorization": f"Bearer {token}"}

        # multipart/form-data — httpx сам сформирует правильный Content-Type
        # с boundary'ем. Поле `file` нужно как (filename, content, mime).
        files = {
            "file": ("users.txt", file_content, "text/plain"),
        }
        data = {
            "name": name,
            "type": list_type,
        }

        logger.info(
            f"VK Ads: создаю remarketing list '{name}' с {len(user_ids)} ID"
        )

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                url, headers=headers, files=files, data=data
            )

        if response.status_code != 200:
            try:
                err_body = response.json()
            except Exception:
                err_body = {"raw": response.text}
            raise VKAdsAPIError(
                f"VK Ads remarketing list create failed: HTTP "
                f"{response.status_code}, body={err_body}"
            )

        result = response.json()
        logger.info(
            f"VK Ads: список создан, id={result.get('id')}, "
            f"status={result.get('status')}, entries={result.get('entries_count')}"
        )
        return result

    async def get_remarketing_users_list(self, list_id: int) -> dict:
        """Получает текущее состояние remarketing-списка.

        Используется чтобы опросить статус (receiving → mapped → ready).
        """
        token = await self._get_token()
        url = f"https://ads.vk.com/api/v3/remarketing/users_lists/{list_id}.json"
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers)

        if response.status_code != 200:
            raise VKAdsAPIError(
                f"VK Ads get remarketing list failed: HTTP {response.status_code}"
            )

        return response.json()
