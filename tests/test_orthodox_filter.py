"""Тесты фильтра православных сообществ (Phase 5)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.targetolog.orthodox_filter import (
    claude_filter_orthodox,
    quick_filter_obviously_bad,
)


# --- quick_filter_obviously_bad ---


@pytest.mark.parametrize("group,expected", [
    # Очевидно православное — оставляем
    ({"name": "Серафимо-Дивеевский монастырь", "description": ""}, True),
    ({"name": "Православная женщина", "description": "Молимся вместе"}, True),
    ({"name": "Храм Христа Спасителя", "description": "Официальное сообщество"}, True),
    # Очевидный треш — отсеиваем
    ({"name": "Православные шутят", "description": "Юмор и приколы"}, False),
    ({"name": "Церковь Сатаны", "description": "Сатанинский храм"}, False),
    ({"name": "Магия и эзотерика", "description": "Колдовство и таро"}, False),
    ({"name": "Анекдоты про попов", "description": ""}, False),
    # Чужие конфессии — отсеиваем
    ({"name": "Католический приход", "description": "Римско-католический"}, False),
    ({"name": "Кришнаиты Москвы", "description": "Сообщество кришна"}, False),
    ({"name": "Свидетели Иеговы СПб", "description": ""}, False),
    # Атеистическое — отсеиваем
    ({"name": "Атеисты России", "description": "Против религии"}, False),
    ({"name": "Антирелигиозный клуб", "description": ""}, False),
    # Пустые поля — оставляем (вдруг хорошее)
    ({"name": "", "description": ""}, True),
    ({}, True),  # совсем без полей
])
def test_quick_filter_obviously_bad(group, expected):
    """Эвристик правильно отсеивает явный треш и оставляет православное."""
    assert quick_filter_obviously_bad(group) is expected


def test_quick_filter_case_insensitive():
    """Стоп-слова работают независимо от регистра."""
    assert quick_filter_obviously_bad({"name": "ЮМОР", "description": ""}) is False
    assert quick_filter_obviously_bad({"name": "САТАНА", "description": ""}) is False


def test_quick_filter_checks_activity_field():
    """Поле `activity` тоже учитывается (это категория группы от VK)."""
    assert quick_filter_obviously_bad({
        "name": "Сообщество", "description": "", "activity": "Юмор"
    }) is False


# --- claude_filter_orthodox ---


@pytest.mark.asyncio
async def test_claude_filter_keeps_only_orthodox_per_decision():
    """Claude отметил 2 группы как orthodox=true → они остаются, остальные отсеиваются."""
    groups = [
        {"id": 1, "name": "Серафимо-Дивеевский монастырь", "description": "РПЦ"},
        {"id": 2, "name": "Православная женщина", "description": "Молимся"},
        {"id": 3, "name": "Подслушано в храме", "description": "Юмор"},
    ]

    # Мокаем ответ Claude — JSON с решениями
    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].text = json.dumps({
        "results": [
            {"id": 1, "orthodox": True, "reason": "Официальное сообщество монастыря РПЦ"},
            {"id": 2, "orthodox": True, "reason": "Православное сообщество для женщин"},
            {"id": 3, "orthodox": False, "reason": "Юмористический паблик"},
        ]
    })

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch(
        "src.targetolog.orthodox_filter.AsyncAnthropic",
        return_value=mock_client,
    ):
        filtered = await claude_filter_orthodox(groups)

    assert len(filtered) == 2
    assert filtered[0]["id"] == 1
    assert filtered[1]["id"] == 2
    # Reason сохранён в результате
    assert "_orthodox_reason" in filtered[0]
    assert "монастыря" in filtered[0]["_orthodox_reason"]


@pytest.mark.asyncio
async def test_claude_filter_handles_json_wrapped_in_markdown():
    """Claude иногда оборачивает JSON в ```json``` — фильтр должен снимать."""
    groups = [{"id": 1, "name": "Тест", "description": ""}]

    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].text = (
        "```json\n"
        '{"results": [{"id": 1, "orthodox": true, "reason": "OK"}]}\n'
        "```"
    )

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch(
        "src.targetolog.orthodox_filter.AsyncAnthropic",
        return_value=mock_client,
    ):
        filtered = await claude_filter_orthodox(groups)

    assert len(filtered) == 1


@pytest.mark.asyncio
async def test_claude_filter_failsafe_when_claude_unavailable():
    """Если Claude API выкинул исключение — отдаём весь батч без фильтрации.

    Это правильнее чем потерять данные: лучше показать Vizit'у больше
    групп (он сам глазами увидит треш) чем выкинуть всё.
    """
    groups = [
        {"id": 1, "name": "Группа 1"},
        {"id": 2, "name": "Группа 2"},
    ]

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API down"))

    with patch(
        "src.targetolog.orthodox_filter.AsyncAnthropic",
        return_value=mock_client,
    ):
        filtered = await claude_filter_orthodox(groups)

    # Fail-safe — обе группы остались
    assert len(filtered) == 2


@pytest.mark.asyncio
async def test_claude_filter_failsafe_when_json_invalid():
    """Если Claude вернул не-JSON — fail-safe, возвращаем батч как есть."""
    groups = [{"id": 1, "name": "Тест"}]

    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].text = "Не могу определить — какое-то странное название"

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch(
        "src.targetolog.orthodox_filter.AsyncAnthropic",
        return_value=mock_client,
    ):
        filtered = await claude_filter_orthodox(groups)

    # Fail-safe
    assert len(filtered) == 1


@pytest.mark.asyncio
async def test_claude_filter_empty_input():
    """Пустой вход — пустой выход, без обращения к Claude."""
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock()

    with patch(
        "src.targetolog.orthodox_filter.AsyncAnthropic",
        return_value=mock_client,
    ):
        filtered = await claude_filter_orthodox([])

    assert filtered == []
    # Claude не должен был вызываться
    mock_client.messages.create.assert_not_called()
