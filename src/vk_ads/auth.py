"""OAuth 2.0 client_credentials аутентификация для VK Ads API.

VK Реклама API v2 использует OAuth2 на ads.vk.com.

Endpoint: https://ads.vk.com/api/v2/oauth2/token.json

Flow:
1. POST с grant_type=client_credentials, client_id, client_secret, permanent=true
2. Получаем access_token (вечный с permanent=true)
3. Кэшируем в БД (SQLite/Postgres) — переживает перезапуски
4. При следующем старте бота — загружаем из БД, не запрашиваем новый

ВАЖНО: VK ограничивает количество активных токенов на client_id (~5 штук).
Если запрашивать новый при каждом перезапуске бота — быстро упираемся в лимит
и получаем `token_limit_exceeded` 403. Persistent caching эту проблему решает.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httpx
from sqlalchemy import select

from src.db.models import TokenCache
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)

OAUTH_URL = "https://ads.vk.com/api/v2/oauth2/token.json"
REFRESH_BEFORE_EXPIRY_SEC = 300  # обновляем за 5 минут до истечения


@dataclass
class TokenInfo:
    """Информация об access_token."""

    access_token: str
    expires_at: float | None  # unix timestamp; None = permanent
    refresh_token: str | None = None
    is_permanent: bool = False

    @property
    def is_expired(self) -> bool:
        """True если токен истёк или истечёт в ближайшие 5 минут.

        Permanent токены никогда не истекают.
        """
        if self.is_permanent or self.expires_at is None:
            return False
        return time.time() >= self.expires_at - REFRESH_BEFORE_EXPIRY_SEC

    @property
    def seconds_until_expiry(self) -> int:
        if self.is_permanent or self.expires_at is None:
            return 99999999
        return max(0, int(self.expires_at - time.time()))


class VKAdsAuthError(Exception):
    """Ошибка получения OAuth-токена от VK."""


class VKAdsAuthenticator:
    """OAuth-аутентификатор VK Ads API с persistent caching через БД.

    При первом get_access_token():
    1. Пробует загрузить токен из БД (по client_id)
    2. Если в БД нет или истёк — делает POST на OAuth endpoint
    3. Сохраняет полученный токен в БД
    4. Кэширует в памяти для последующих вызовов
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        request_permanent: bool = True,
    ):
        if not client_id or not client_secret:
            raise ValueError("client_id и client_secret не должны быть пустыми")
        self.client_id = client_id
        self.client_secret = client_secret
        self.request_permanent = request_permanent
        self._token: TokenInfo | None = None

    async def get_access_token(self) -> str:
        """Возвращает валидный access_token.

        Порядок:
        1. Если есть в памяти и не истёк — отдаём
        2. Если есть в БД и не истёк — загружаем в память и отдаём
        3. Иначе запрашиваем новый, сохраняем в БД и память
        """
        if self._token is not None and not self._token.is_expired:
            return self._token.access_token

        cached = await self._load_from_db()
        if cached and not cached.is_expired:
            logger.info(
                f"Использую кэшированный токен из БД "
                f"(permanent={cached.is_permanent}, "
                f"осталось ~{cached.seconds_until_expiry // 3600}ч)"
            )
            self._token = cached
            return cached.access_token

        logger.info("Кэшированного токена нет / истёк — запрашиваю новый у VK")
        await self._refresh_token()
        await self._save_to_db()
        assert self._token is not None
        return self._token.access_token

    async def _load_from_db(self) -> Optional[TokenInfo]:
        """Загружает токен из БД по client_id, если есть."""
        try:
            async with SessionLocal() as session:
                result = await session.execute(
                    select(TokenCache).where(TokenCache.client_id == self.client_id)
                )
                row = result.scalar_one_or_none()
                if not row:
                    return None
                return TokenInfo(
                    access_token=row.access_token,
                    expires_at=row.expires_at,
                    refresh_token=row.refresh_token,
                    is_permanent=row.is_permanent,
                )
        except Exception as e:
            logger.warning(f"Не смог загрузить токен из БД: {e}")
            return None

    async def _save_to_db(self) -> None:
        """Сохраняет текущий токен в БД (upsert по client_id)."""
        if self._token is None:
            return
        try:
            async with SessionLocal() as session:
                result = await session.execute(
                    select(TokenCache).where(TokenCache.client_id == self.client_id)
                )
                row = result.scalar_one_or_none()
                if row:
                    row.access_token = self._token.access_token
                    row.refresh_token = self._token.refresh_token
                    row.expires_at = self._token.expires_at
                    row.is_permanent = self._token.is_permanent
                    row.updated_at = datetime.utcnow()
                else:
                    session.add(TokenCache(
                        client_id=self.client_id,
                        access_token=self._token.access_token,
                        refresh_token=self._token.refresh_token,
                        expires_at=self._token.expires_at,
                        is_permanent=self._token.is_permanent,
                    ))
                await session.commit()
                logger.info("Токен сохранён в БД")
        except Exception as e:
            logger.error(f"Не смог сохранить токен в БД: {e}")

    async def _refresh_token(self) -> None:
        """Запрашивает новый access_token через client_credentials grant."""
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        if self.request_permanent:
            data["permanent"] = "true"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(OAUTH_URL, data=data)
        except httpx.TimeoutException as e:
            raise VKAdsAuthError(f"Таймаут при запросе токена: {e}") from e
        except httpx.RequestError as e:
            raise VKAdsAuthError(f"Сетевая ошибка при запросе токена: {e}") from e

        if response.status_code != 200:
            raise VKAdsAuthError(
                f"VK вернул {response.status_code}: {response.text[:300]}"
            )

        try:
            resp_data = response.json()
        except ValueError as e:
            raise VKAdsAuthError(f"VK вернул не JSON: {response.text[:300]}") from e

        if "access_token" not in resp_data:
            raise VKAdsAuthError(f"В ответе VK нет access_token: {resp_data}")

        # Permanent токены имеют expires_in=0 — трактуем как «без срока»
        expires_in = resp_data.get("expires_in", 86400)
        is_permanent = self.request_permanent and expires_in == 0
        expires_at = None if is_permanent else time.time() + expires_in

        self._token = TokenInfo(
            access_token=resp_data["access_token"],
            expires_at=expires_at,
            refresh_token=resp_data.get("refresh_token"),
            is_permanent=is_permanent,
        )

        if is_permanent:
            logger.info("Получен PERMANENT access_token (без срока истечения)")
        else:
            logger.info(
                f"Получен access_token, действует {expires_in} сек "
                f"(~{expires_in // 3600}ч)"
            )

    async def invalidate(self) -> None:
        """Принудительно сбросить кэш (in-memory и БД).

        Использовать когда VK вернул 401 с реально протухшим токеном.
        """
        logger.info("Сбрасываю кэш токена (память + БД)")
        self._token = None
        try:
            async with SessionLocal() as session:
                result = await session.execute(
                    select(TokenCache).where(TokenCache.client_id == self.client_id)
                )
                row = result.scalar_one_or_none()
                if row:
                    await session.delete(row)
                    await session.commit()
        except Exception as e:
            logger.warning(f"Не смог удалить токен из БД: {e}")
