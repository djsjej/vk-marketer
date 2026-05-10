"""Загрузка статических картинок в VK Рекламу.

VK Реклама API v2 принимает картинки одним POST-запросом:

    POST https://ads.vk.com/api/v2/content/static.json
    Authorization: Bearer <access_token>
    Content-Type: multipart/form-data
    file=@image.jpg

Ответ:
    {
        "id": 21506470,
        "variants": {
            "original": {"url": "...", "height": 600, "width": 600, "size": 14407},
            "uploaded": {"url": "...", "height": 600, "width": 600, "size": 14407}
        }
    }

Возвращаемый id (content_id) используется при создании баннеров (banners.content).

Требования к изображениям (см. https://ads.vk.com/doc/api/resource/Content):
- Форматы: jpg, png
- Для полноценного баннера нужны ТРИ размера: 256x256, 600x600, 1080x607
  (загружаются по очереди, у каждого свой content_id, потом подставляются
  в соответствующие поля banner.content)
"""

import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

VK_CONTENT_STATIC_URL = "https://ads.vk.com/api/v2/content/static.json"


class VKContentUploadError(Exception):
    """Ошибка при загрузке картинки в VK."""


def _detect_mime(filename: str) -> str:
    """Определить MIME по расширению."""
    ext = Path(filename).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
    }.get(ext, "image/jpeg")


async def upload_image_bytes(
    access_token: str,
    image_bytes: bytes,
    filename: str = "image.jpg",
    mime_type: str | None = None,
    timeout: float = 60.0,
) -> dict:
    """Загрузить картинку (как байты) в VK Ads.

    Args:
        access_token: Bearer токен (от OAuth)
        image_bytes: содержимое картинки
        filename: имя файла (для multipart, влияет на MIME если не задан)
        mime_type: явный MIME-тип, иначе угадывается по расширению
        timeout: секунд

    Returns:
        Полный словарь ответа VK (id, variants). content_id = result["id"].

    Raises:
        VKContentUploadError: если VK вернул не-2xx или невалидный JSON
    """
    if not image_bytes:
        raise VKContentUploadError("Пустые байты — нечего загружать")

    mime = mime_type or _detect_mime(filename)
    logger.info(
        f"Загружаю картинку в VK: {filename}, {len(image_bytes)} bytes, mime={mime}"
    )

    files = {"file": (filename, image_bytes, mime)}
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                VK_CONTENT_STATIC_URL, files=files, headers=headers
            )
    except httpx.RequestError as e:
        raise VKContentUploadError(f"Сетевая ошибка при загрузке: {e}") from e

    if response.status_code >= 400:
        raise VKContentUploadError(
            f"VK вернул {response.status_code}: {response.text[:500]}"
        )

    try:
        result = response.json()
    except ValueError as e:
        raise VKContentUploadError(
            f"VK вернул не JSON: {response.text[:300]}"
        ) from e

    if "id" not in result:
        raise VKContentUploadError(f"В ответе VK нет id: {result}")

    logger.info(f"Картинка загружена, content_id={result['id']}")
    return result


async def upload_image_file(
    access_token: str,
    image_path: str | Path,
    timeout: float = 60.0,
) -> dict:
    """Прокси для upload_image_bytes — читает файл с диска."""
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")
    return await upload_image_bytes(
        access_token=access_token,
        image_bytes=path.read_bytes(),
        filename=path.name,
        timeout=timeout,
    )
