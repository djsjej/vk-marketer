"""HTTP-клиент к VK API на httpx.

Использует service token из settings.vk_api_service_token (Railway env
VK_API_SERVICE_TOKEN). Получен от Vizit'а 14.05.2026, привязан к
зарегистрированному VK ID приложению 54593625 «Парсер».

Ограничения service token (по докам VK):
Доступные публичные методы:
- groups.getById (информация о сообществе)
- groups.getMembers (подписчики ПУБЛИЧНОГО сообщества)
- wall.get (посты публичной стены)
- users.get (базовая инфа о пользователе)
- newsfeed.search (поиск по новостям)

НЕ доступны (потребуют user token через PKCE OAuth позже, если понадобятся):
- likes.getList (кто лайкнул пост)
- friends.get (друзья пользователя)
- users.getSubscriptions (на что подписан человек)
- messages.* (личные сообщения)

Rate limit: 5 запросов/сек на токен. Мы делаем 4/сек с запасом
(в _throttle), чтобы не упереться в 429.

Документация VK API:
- https://dev.vk.com/ru/api/api-requests
- https://dev.vk.com/ru/method/groups.getById
- https://dev.vk.com/ru/method/groups.getMembers
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

VK_API_BASE = "https://api.vk.com/method"
# Версия API. 5.199 — стабильная на 2026.
# Старые версии (5.131, 5.81) тоже работают, но новая форма ответа
# у groups.getById только в 5.199+ (с полями groups/profiles).
VK_API_VERSION = "5.199"


class VKAPIError(Exception):
    """Ошибка от VK API (response.error).

    Коды ошибок VK см. https://dev.vk.com/ru/reference/errors. Самые
    частые для нашего случая:
    - 5: пользовательская авторизация не удалась (плохой токен)
    - 6: слишком много запросов в секунду
    - 15: доступ запрещён (закрытое сообщество)
    - 100: один из параметров неверный
    - 113: invalid user id (несуществующий пользователь)
    - 203: доступ к группе запрещён
    """

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"VK API error {code}: {message}")


class VKAPIClient:
    """Минимальный async-клиент к VK API.

    Использовать как async context manager (для управления httpx.AsyncClient):

        async with VKAPIClient(service_token="...") as client:
            group = await client.groups_get_by_id("pomolimsy")

    Все методы возвращают распакованное поле 'response' из VK API ответа.
    """

    def __init__(
        self,
        service_token: str,
        api_version: str = VK_API_VERSION,
        rate_limit_per_sec: int = 4,
        timeout_seconds: float = 30.0,
    ):
        self.service_token = service_token
        self.api_version = api_version
        self.rate_limit_per_sec = rate_limit_per_sec
        self.timeout_seconds = timeout_seconds
        # time.monotonic() безопаснее event_loop.time() — не зависит от loop
        self._last_call_time: float = 0.0
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "VKAPIClient":
        self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _throttle(self) -> None:
        """Простейший rate limiter — пауза перед запросом если нужно.

        Не идеальный (single-instance, без честной shared-очереди), но
        для нашего объёма (один процесс, один токен) хватает.
        """
        min_interval = 1.0 / self.rate_limit_per_sec
        now = time.monotonic()
        elapsed = now - self._last_call_time
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)
        self._last_call_time = time.monotonic()

    async def call(self, method: str, **params: Any) -> Any:
        """Универсальный вызов любого VK API метода.

        Автоматически:
        - Добавляет access_token и v (API version)
        - Throttle перед каждым запросом
        - Парсит ответ и поднимает VKAPIError на ошибки

        Returns:
            Распакованное поле 'response' из VK API JSON ответа.
            Может быть dict, list, число, строка — зависит от метода.

        Raises:
            VKAPIError: если VK вернул объект error
            httpx.HTTPStatusError: если HTTP-статус не 2xx
            RuntimeError: если клиент не открыт как context manager
        """
        if not self._client:
            raise RuntimeError(
                "VKAPIClient должен использоваться как async context manager: "
                "`async with VKAPIClient(...) as client:`"
            )

        await self._throttle()

        # access_token и v добавляем последними чтобы не перетереть
        # явно переданные params если кто-то их укажет (но обычно никто
        # не должен — это служебные параметры).
        request_params = {**params, "access_token": self.service_token, "v": self.api_version}

        url = f"{VK_API_BASE}/{method}"
        logger.debug(
            "VK API call: %s (params: %s)",
            method,
            [k for k in params.keys()],  # не логируем значения — могут быть PII
        )

        # VK принимает и GET и POST. POST безопаснее (токен не в URL → не
        # в логах reverse-proxy).
        response = await self._client.post(url, data=request_params)
        response.raise_for_status()

        data = response.json()
        if "error" in data:
            err = data["error"]
            raise VKAPIError(
                code=err.get("error_code", -1),
                message=err.get("error_msg", "Unknown VK API error"),
            )

        return data.get("response")

    async def groups_get_by_id(self, group_id: str | int) -> dict:
        """Получить информацию о сообществе по ID или screen_name.

        Args:
            group_id: числовой ID (216409501) или screen_name ('pomolimsy',
                'pravoslavnie_hristiane'). VK сам распарсит и то и другое.
                URL `vk.com/pomolimsy` нужно сначала почистить — оставить
                только последний сегмент (`pomolimsy`).

        Returns:
            dict с полями группы:
            - id (int)
            - name (str) — отображаемое название
            - screen_name (str) — slug в URL
            - members_count (int) — число подписчиков
            - description (str) — описание сообщества
            - is_closed (int): 0=открытое, 1=закрытое, 2=частное

        Raises:
            VKAPIError(100): сообщество не найдено
            VKAPIError(203): доступ запрещён (закрытое)
        """
        # VK API метод groups.getById принимает CSV в group_ids.
        # Запрашиваем одну группу → ожидаем список с одним элементом.
        response = await self.call(
            "groups.getById",
            group_ids=str(group_id),
            fields="members_count,description",
        )

        # Форма ответа меняется между версиями API:
        # - v5.131+: {"groups": [...], "profiles": []}  (новая)
        # - v5.81 и старее: прямой list of group objects (старая)
        # Поддерживаем обе на всякий случай.
        if isinstance(response, dict) and "groups" in response:
            groups = response["groups"]
        elif isinstance(response, list):
            groups = response
        else:
            raise VKAPIError(
                -1,
                f"Неожиданный формат ответа groups.getById: {type(response).__name__}",
            )

        if not groups:
            raise VKAPIError(100, f"Сообщество не найдено: {group_id}")

        return groups[0]
