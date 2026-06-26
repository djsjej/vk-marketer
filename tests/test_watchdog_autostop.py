"""Тесты авто-стопа Сторожа (Шаг A автономии).

Сторож не только алертит, а САМ выключает дорогие по CPL кампании.
Пороги консервативны (CPL судим только после 100₽), пауза безопасна.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.scheduler.jobs import check_metrics_and_anomalies


def _item(cid: int, shows: int, clicks: int, spent: float, joinings: int) -> dict:
    return {
        "id": cid,
        "rows": [
            {
                "date": "2026-06-26",
                "base": {"shows": shows, "clicks": clicks, "spent": str(spent)},
                "events": {"joinings": joinings},
            }
        ],
    }


def _client(active, stats_items):
    c = MagicMock()
    c.get_active_ad_plans = AsyncMock(return_value=active)
    c.get_campaign_stats = AsyncMock(return_value={"items": stats_items})
    c.pause_campaign = AsyncMock(return_value={"status": "blocked"})
    return c


@pytest.mark.asyncio
async def test_watchdog_auto_stops_bad_campaign():
    """CPL дорогой → Сторож сам выключает кампанию и шлёт отчёт."""
    # 500₽ потрачено, 2 лида → CPL 250₽ (выше нормы 50)
    client = _client(
        active=[{"id": 1, "name": "bad"}],
        stats_items=[_item(1, 3000, 20, 500.0, 2)],
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()

    with patch("src.vk_ads.client.VKAdsClient.from_settings", return_value=client), patch(
        "src.db.repository.save_stats_snapshots", AsyncMock(return_value=1)
    ), patch("src.db.repository.log_action", AsyncMock()), patch(
        "src.knowledge.recorder.record_campaign_result", MagicMock(return_value=True)
    ) as rec, patch("src.scheduler.jobs.settings") as s:
        s.auto_stop_enabled = True
        await check_metrics_and_anomalies(bot)

    client.pause_campaign.assert_awaited_once_with(1)
    # Провал записан в базу знаний
    assert rec.call_args.kwargs["won"] is False
    text = bot.send_message.await_args.kwargs["text"]
    assert "сам выключил" in text.lower()


@pytest.mark.asyncio
async def test_watchdog_does_not_stop_good_campaign():
    """CPL в норме → не трогаем, отчёта нет."""
    # 200₽, 5 лидов → CPL 40₽ (норма)
    client = _client(
        active=[{"id": 2, "name": "good"}],
        stats_items=[_item(2, 5000, 50, 200.0, 5)],
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()

    with patch("src.vk_ads.client.VKAdsClient.from_settings", return_value=client), patch(
        "src.db.repository.save_stats_snapshots", AsyncMock(return_value=1)
    ), patch("src.scheduler.jobs.settings") as s:
        s.auto_stop_enabled = True
        await check_metrics_and_anomalies(bot)

    client.pause_campaign.assert_not_called()
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_watchdog_only_alerts_when_autostop_disabled():
    """Выключатель: auto_stop_enabled=False → только алерт, без паузы."""
    client = _client(
        active=[{"id": 3, "name": "bad"}],
        stats_items=[_item(3, 3000, 20, 500.0, 2)],
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()

    with patch("src.vk_ads.client.VKAdsClient.from_settings", return_value=client), patch(
        "src.db.repository.save_stats_snapshots", AsyncMock(return_value=1)
    ), patch("src.scheduler.jobs.settings") as s:
        s.auto_stop_enabled = False
        await check_metrics_and_anomalies(bot)

    client.pause_campaign.assert_not_called()
    text = bot.send_message.await_args.kwargs["text"]
    assert "вручную" in text.lower()
