"""Хранилище chat_id групп где зарегистрированы агент-боты.

Phase 5.16 (17.05.2026): подготовка к межагентным диалогам.
Когда Vizit добавляет бота в группу — мы сохраняем chat_id этой группы.
В будущем (Phase 5.17) боты смогут писать в эти группы сами по триггерам.

In-memory + сохранение в файл /tmp/.agent_groups.json для переживания
рестартов worker'a.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

_STORAGE = Path("/tmp/.agent_groups.json")
_lock = threading.Lock()

# Структура: {agent_id: {"chat_id": int, "title": str}}
_groups: Dict[str, dict] = {}


def _load() -> None:
    """Загружает state из файла при старте."""
    global _groups
    if not _STORAGE.exists():
        return
    try:
        _groups = json.loads(_STORAGE.read_text())
    except Exception as e:
        logger.warning(f"Не смог загрузить {_STORAGE}: {e}")


def _save() -> None:
    """Сохраняет state в файл."""
    try:
        _STORAGE.write_text(json.dumps(_groups, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.warning(f"Не смог сохранить {_STORAGE}: {e}")


_load()


def register_group(agent_id: str, chat_id: int, title: str) -> None:
    """Сохраняет что агент добавлен в группу chat_id."""
    with _lock:
        _groups[agent_id] = {"chat_id": chat_id, "title": title}
        _save()


def get_group_for_agent(agent_id: str) -> dict | None:
    """Возвращает {chat_id, title} группы где работает агент, или None."""
    with _lock:
        return _groups.get(agent_id)


def get_all_groups() -> Dict[str, dict]:
    """Возвращает все зарегистрированные группы по агентам."""
    with _lock:
        return dict(_groups)


def get_unique_chat_ids() -> set:
    """Все уникальные chat_id где есть хотя бы один наш бот."""
    with _lock:
        return {info["chat_id"] for info in _groups.values()}
