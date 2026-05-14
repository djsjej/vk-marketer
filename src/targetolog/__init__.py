"""Агент-таргетолог: поиск горячих VK-аудиторий через VK API.

Phase 5 проекта vk-marketer. См. docs/HANDOFF_2026_05_14.md секцию
«Дополнение от позднего вечера 14.05 — пересмотр Phase 5».
"""

from src.targetolog.vk_api_client import VKAPIClient, VKAPIError

__all__ = ["VKAPIClient", "VKAPIError"]
