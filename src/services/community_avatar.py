"""Утилиты для работы с аватарами VK сообществ.

Phase 5.13 (17.05.2026): фикс косметического бага в превью кампаний.
В кружочке слева у объявления должен быть АВАТАР сообщества
(pomolimsy), а не та же картинка что и в основном креативе.

Workflow:
1. Парсим screen_name из URL сообщества (`https://vk.com/pomolimsy` → `pomolimsy`)
2. Через VK API (groups.getById с fields=photo_200) получаем URL аватарки
3. Скачиваем bytes аватара
4. Загружаем в VK Ads через VKAdsClient.upload_image
5. Возвращаем content_id для использования в `icon_256x256` payload

Будущая оптимизация (Phase 6+): кешировать content_id в БД/env чтобы
не качать аватар при каждом создании кампании.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import httpx

from src.config import settings
from src.targetolog.vk_api_client import VKAPIClient

logger = logging.getLogger(__name__)


def parse_screen_name_from_url(community_url: str) -> str:
    """Извлекает screen_name из URL сообщества.

    Примеры:
    - https://vk.com/pomolimsy → pomolimsy
    - vk.com/pomolimsy → pomolimsy
    - vk.com/club216409501 → club216409501
    - pomolimsy → pomolimsy (уже screen_name)

    Args:
        community_url: URL или screen_name.

    Returns:
        screen_name (строка без vk.com/ и протокола).

    Raises:
        ValueError: если не удалось распарсить.
    """
    if not community_url:
        raise ValueError("community_url пустой")

    # Если уже выглядит как screen_name (без слешей) — возвращаем как есть
    if "/" not in community_url and "." not in community_url:
        return community_url.strip()

    # Парсим как URL
    if not community_url.startswith(("http://", "https://")):
        community_url = "https://" + community_url

    parsed = urlparse(community_url)
    path = parsed.path.strip("/")

    if not path:
        raise ValueError(f"Не смог извлечь screen_name из {community_url}")

    # Берём первый сегмент (после vk.com/)
    return path.split("/")[0]


async def fetch_community_avatar_bytes(community_url: str) -> bytes:
    """Скачивает аватар сообщества VK как bytes.

    Args:
        community_url: URL сообщества (https://vk.com/pomolimsy)
            или просто screen_name (pomolimsy).

    Returns:
        bytes — JPEG/PNG данные аватара (обычно 200x200 или 400x400 px).

    Raises:
        ValueError: если URL неправильный или сообщество не найдено.
        httpx.HTTPError: если скачивание упало.
    """
    if not settings.vk_api_service_token:
        raise ValueError(
            "Не задан VK_API_SERVICE_TOKEN в env — не могу запросить VK API"
        )

    screen_name = parse_screen_name_from_url(community_url)
    logger.info(f"[avatar] Запрашиваю аватар сообщества '{screen_name}'")

    async with VKAPIClient(service_token=settings.vk_api_service_token) as vk:
        response = await vk.call(
            "groups.getById",
            group_ids=screen_name,
            fields="photo_200,photo_400_orig",
        )

    # Парсим ответ — поддерживаем оба формата (v5.131+ и старее)
    if isinstance(response, dict) and "groups" in response:
        groups = response["groups"]
    elif isinstance(response, list):
        groups = response
    else:
        raise ValueError(
            f"Неожиданный формат ответа groups.getById: {type(response).__name__}"
        )

    if not groups:
        raise ValueError(f"Сообщество '{screen_name}' не найдено в VK API")

    group_info = groups[0]
    # Берём максимальное доступное качество
    photo_url = (
        group_info.get("photo_400_orig")
        or group_info.get("photo_200")
        or group_info.get("photo_100")
        or group_info.get("photo_50")
    )

    if not photo_url:
        raise ValueError(
            f"У сообщества '{screen_name}' нет аватара в ответе API: "
            f"{list(group_info.keys())}"
        )

    logger.info(f"[avatar] Скачиваю с {photo_url}")

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(photo_url)
        response.raise_for_status()

    avatar_bytes = response.content
    logger.info(
        f"[avatar] Получен аватар '{screen_name}' "
        f"({len(avatar_bytes) // 1024} KB)"
    )
    return avatar_bytes
