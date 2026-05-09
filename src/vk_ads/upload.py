"""Multipart-загрузка картинок в VK Рекламу.

ЭТО КРИТИЧНЫЙ МОДУЛЬ. На нём ломается большинство интеграций
(включая прошлые попытки автора через n8n). Поэтому делаем на чистом httpx,
который умеет multipart нативно.

Процесс:
1. Получить upload_url от VK Ads API
2. POST на этот URL с файлом как multipart/form-data
3. Распарсить ответ, извлечь image_id (или photo_hash)
4. Использовать image_id при создании объявления

Особенности:
- Поле для файла называется по-разному в разных эндпоинтах VK API.
  В новом VK Ads API уточнить — обычно `file` или `photo`.
- Размер файла, формат и разрешение должны соответствовать требованиям VK
  (в зависимости от формата объявления — баннер, карусель, видео-обложка и т.д.)
- Ответ парсится из JSON, но иногда возвращается в виде form-encoded URL.
"""

import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


async def upload_image_to_vk(
    upload_url: str,
    image_path: str | Path,
    field_name: str = "file",
) -> dict:
    """Загрузить картинку на upload_url, полученный от VK API.

    Args:
        upload_url: URL для загрузки (получается через VK API).
        image_path: путь к файлу картинки.
        field_name: имя поля в form-data (по умолчанию "file").

    Returns:
        Dict с ответом VK (обычно содержит image_id или photo_hash).

    Raises:
        httpx.HTTPStatusError: если VK ответил ошибкой.
        FileNotFoundError: если файла нет.
    """
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"Файл не найден: {image_path}")

    logger.info(f"Загружаю {image_path} на {upload_url}")

    async with httpx.AsyncClient(timeout=60.0) as client:
        with image_path.open("rb") as f:
            files = {field_name: (image_path.name, f, _detect_mime(image_path))}
            response = await client.post(upload_url, files=files)

        response.raise_for_status()

        result = response.json()
        logger.info(f"VK upload response keys: {list(result.keys())}")
        return result


def _detect_mime(path: Path) -> str:
    """Простое определение MIME типа по расширению."""
    ext = path.suffix.lower()
    mapping = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    return mapping.get(ext, "application/octet-stream")
