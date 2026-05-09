"""OAuth 2.0 client_credentials аутентификация для VK Ads API.

VK Реклама (новая) использует myTarget OAuth2 под капотом.
Endpoint: https://target.my.com/api/v2/oauth2/token.json

Flow:
1. POST с grant_type=client_credentials, client_id, client_secret
2. Получаем access_token (живёт ~24 часа) и refresh_token
3. Кэшируем в памяти, обновляем за 5 минут до истечения

Документация:
- https://target.my.com/help/advertisers/api_authorization/ru
- https://ads.vk.com/help (раздел про API)
"""

import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

OAUTH_URL = "https://target.my.com/api/v2/oauth2/token.json"
REFRESH_BEFORE_EXPIRY_SEC = 300  # обновляем за 5 минут до истечения


@dataclass
class TokenInfo:
    """Информация об access_token: значение + когда истекает."""

    access_token: str
    expires_at: float  # unix timestamp
    refresh_token: str | None = None

    @property
    def is_expired(self) -> bool:
        """True если токен уже истёк или истечёт в ближайшие 5 минут."""
        return time.time() >= self.expires_at - REFRESH_BEFORE_EXPIRY_SEC

    @property
    def seconds_until_expiry(self) -> int:
        return max(0, int(self.expires_at - time.time()))


class VKAdsAuthError(Exception):
    """Ошибка получения OAuth-токена от VK."""


class VKAdsAuthenticator:
    """OAuth-аутентификатор VK Ads API.

    Кэширует access_token в памяти. При истечении (или приближении к нему)
    автоматически делает повторный POST на OAuth-endpoint и получает новый.

    Использование:
        auth = VKAdsAuthenticator(client_id="...", client_secret="...")
        token = await auth.get_access_token()  # валидный токен
    """

    def __init__(self, client_id: str, client_secret: str):
        if not client_id or not client_secret:
            raise ValueError("client_id и client_secret не должны быть пустыми")
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: TokenInfo | None = None

    async def get_access_token(self) -> str:
        """Возвращает валидный access_token, обновляя при необходимости.

        Это единственный публичный метод — вся логика кэширования внутри.
        """
        if self._token is None or self._token.is_expired:
            await self._refresh_token()
        assert self._token is not None  # для type checker
        return self._token.access_token

    async def _refresh_token(self) -> None:
        """Запрашивает новый access_token через client_credentials grant."""
        logger.info("Запрашиваю новый access_token у VK (target.my.com)")

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    OAUTH_URL,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                )
        except httpx.TimeoutException as e:
            raise VKAdsAuthError(f"Таймаут при запросе токена: {e}") from e
        except httpx.RequestError as e:
            raise VKAdsAuthError(f"Сетевая ошибка при запросе токена: {e}") from e

        if response.status_code != 200:
            raise VKAdsAuthError(
                f"VK вернул {response.status_code}: {response.text[:300]}"
            )

        try:
            data = response.json()
        except ValueError as e:
            raise VKAdsAuthError(f"VK вернул не JSON: {response.text[:300]}") from e

        if "access_token" not in data:
            raise VKAdsAuthError(f"В ответе VK нет access_token: {data}")

        self._token = TokenInfo(
            access_token=data["access_token"],
            expires_at=time.time() + data.get("expires_in", 86400),
            refresh_token=data.get("refresh_token"),
        )
        logger.info(
            f"Получен access_token, действует "
            f"{self._token.seconds_until_expiry} сек (~{self._token.seconds_until_expiry // 3600}ч)"
        )

    def invalidate(self) -> None:
        """Принудительно сбросить кэш (например, если получили 401 от API)."""
        logger.info("Кэш токена сброшен")
        self._token = None
