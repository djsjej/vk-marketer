"""Тесты утреннего отчёта (Трек C).

До этого send_morning_report был заглушкой ('TODO: подключить VK Ads').
Теперь тянет вчерашние метрики, прогоняет через ClaudeAnalyzer и шлёт
живой отчёт. Ключевое требование — graceful degradation: если Claude
упал или VK не настроен, отчёт всё равно уходит (с цифрами или с
понятным сообщением), Vizit не остаётся без утренней сводки.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.scheduler.jobs import send_morning_report


def _stats_item(cid: int, shows: int, clicks: int, spent: float, joinings: int) -> dict:
    return {
        "id": cid,
        "rows": [
            {
                "date": "2026-06-25",
                "base": {"shows": shows, "clicks": clicks, "spent": str(spent)},
                "events": {"joinings": joinings},
            }
        ],
    }


def _client(*, campaigns, stats_items) -> MagicMock:
    c = MagicMock()
    c.get_campaigns = AsyncMock(return_value=campaigns)
    c.get_campaign_stats = AsyncMock(return_value={"items": stats_items})
    return c


@pytest.mark.asyncio
async def test_report_with_real_numbers_and_analysis():
    client = _client(
        campaigns=[{"id": 1, "name": "batch-1"}, {"id": 2, "name": "batch-2"}],
        stats_items=[
            _stats_item(1, 5000, 50, 400.0, 10),  # CPL 40₽
            _stats_item(2, 3000, 20, 600.0, 2),   # CPL 300₽ — плохо
        ],
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()

    fake_analysis = {
        "summary": "Один батч хорош, второй сливает",
        "problems": ["batch-2: CPL 300₽ выше нормы"],
        "winners": ["batch-1: CPL 40₽"],
        "recommendations": [{"action": "pause", "campaign_id": 2, "reason": "дорогой CPL"}],
    }
    with patch("src.vk_ads.client.VKAdsClient.from_settings", return_value=client), patch(
        "src.scheduler.jobs._analyze_morning", AsyncMock(return_value=fake_analysis)
    ):
        await send_morning_report(bot)

    bot.send_message.assert_awaited_once()
    text = bot.send_message.await_args.kwargs["text"]
    # Цифры собраны детерминированно
    assert "1000" in text  # суммарный расход 400+600
    assert "написавших" in text
    # Анализ Claude встроен
    assert "Один батч хорош" in text
    assert "Рекомендации" in text
    # Имя кампании подставлено вместо id
    assert "batch-2" in text


@pytest.mark.asyncio
async def test_report_degrades_when_claude_fails():
    """Claude недоступен → отчёт всё равно уходит с цифрами."""
    client = _client(
        campaigns=[{"id": 1, "name": "c1"}],
        stats_items=[_stats_item(1, 5000, 50, 400.0, 10)],
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()

    with patch("src.vk_ads.client.VKAdsClient.from_settings", return_value=client), patch(
        "src.scheduler.jobs._analyze_morning", AsyncMock(return_value=None)
    ):
        await send_morning_report(bot)

    bot.send_message.assert_awaited_once()
    text = bot.send_message.await_args.kwargs["text"]
    assert "400" in text
    assert "Утренний отчёт" in text


@pytest.mark.asyncio
async def test_no_impressions_yesterday():
    """Кампании были, но вчера не крутились → честное 'показов не было'."""
    client = _client(
        campaigns=[{"id": 1, "name": "c1"}],
        stats_items=[_stats_item(1, 0, 0, 0.0, 0)],
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()

    with patch("src.vk_ads.client.VKAdsClient.from_settings", return_value=client):
        await send_morning_report(bot)

    text = bot.send_message.await_args.kwargs["text"]
    assert "не крутились" in text


@pytest.mark.asyncio
async def test_no_client_still_sends_something():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    with patch("src.vk_ads.client.VKAdsClient.from_settings", return_value=None):
        await send_morning_report(bot)
    bot.send_message.assert_awaited_once()
    assert "не настроен" in bot.send_message.await_args.kwargs["text"]
