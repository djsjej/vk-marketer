"""Тесты дневного рубильника бюджета (Трек A).

`check_daily_budget` — единственное место где Сторож действует
автоматически: при достижении MAX_DAILY_SPEND выключает все активные
кампании. Это предохранитель из Конституции («безопасность бюджета —
приоритет №1»), до этого был заглушкой (`pass`).

Покрываем:
- лимит не пробит → ничего не выключаем, алерта нет
- лимит пробит → выключаем все активные + алерт владельцу
- лимит пробит, но активных нет → тихий выход (защита от спама)
- расход считается по ВСЕМ кампаниям, включая выключенные сегодня
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import settings
from src.scheduler.jobs import check_daily_budget


def _stats_item(cid: int, spent: float) -> dict:
    """Один элемент items[] из ответа VK Stats API с расходом за день."""
    return {
        "id": cid,
        "rows": [{"date": "2026-06-26", "base": {"shows": 100, "clicks": 5, "spent": str(spent)}}],
    }


def _make_client(*, campaigns, stats_items, active) -> MagicMock:
    client = MagicMock()
    client.get_campaigns = AsyncMock(return_value=campaigns)
    client.get_campaign_stats = AsyncMock(return_value={"items": stats_items})
    client.get_active_ad_plans = AsyncMock(return_value=active)
    client.pause_campaign = AsyncMock(return_value={"status": "blocked"})
    return client


@pytest.mark.asyncio
async def test_under_limit_does_nothing():
    """Расход ниже лимита → не паузим, алерта нет."""
    half = settings.max_daily_spend_rub / 2
    client = _make_client(
        campaigns=[{"id": 1, "name": "c1"}],
        stats_items=[_stats_item(1, half)],
        active=[{"id": 1, "name": "c1"}],
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()

    with patch("src.vk_ads.client.VKAdsClient.from_settings", return_value=client):
        await check_daily_budget(bot)

    client.pause_campaign.assert_not_called()
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_over_limit_pauses_all_active_and_alerts():
    """Расход ≥ лимита → выключаем все активные и шлём алерт."""
    over = settings.max_daily_spend_rub + 100
    client = _make_client(
        campaigns=[{"id": 1, "name": "c1"}, {"id": 2, "name": "c2"}],
        # Расход размазан по двум кампаниям, в сумме за лимит.
        stats_items=[_stats_item(1, over / 2 + 50), _stats_item(2, over / 2 + 50)],
        active=[{"id": 1, "name": "c1"}, {"id": 2, "name": "c2"}],
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()

    with patch("src.vk_ads.client.VKAdsClient.from_settings", return_value=client):
        await check_daily_budget(bot)

    assert client.pause_campaign.await_count == 2
    paused_ids = {c.args[0] for c in client.pause_campaign.await_args_list}
    assert paused_ids == {1, 2}
    bot.send_message.assert_awaited_once()
    text = bot.send_message.await_args.kwargs["text"]
    assert "лимит" in text.lower()


@pytest.mark.asyncio
async def test_over_limit_but_nothing_active_is_silent():
    """Лимит пробит, но активных кампаний нет (уже выключили) → тихо, без спама."""
    over = settings.max_daily_spend_rub + 500
    client = _make_client(
        campaigns=[{"id": 1, "name": "c1"}],
        stats_items=[_stats_item(1, over)],
        active=[],  # всё уже выключено на прошлом проходе
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()

    with patch("src.vk_ads.client.VKAdsClient.from_settings", return_value=client):
        await check_daily_budget(bot)

    client.pause_campaign.assert_not_called()
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_spend_counted_across_all_campaigns_not_just_active():
    """Расход считается по всем кампаниям: выключенная сегодня кампания
    с большим расходом тоже учитывается и может пробить лимит, даже если
    активная сейчас почти ничего не потратила."""
    big = settings.max_daily_spend_rub + 200
    client = _make_client(
        campaigns=[{"id": 1, "name": "spent-then-blocked"}, {"id": 2, "name": "active-small"}],
        stats_items=[_stats_item(1, big), _stats_item(2, 10)],
        active=[{"id": 2, "name": "active-small"}],
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()

    with patch("src.vk_ads.client.VKAdsClient.from_settings", return_value=client):
        await check_daily_budget(bot)

    # Лимит пробит суммой → активная (id=2) выключается.
    client.pause_campaign.assert_awaited_once_with(2)
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_client_is_safe():
    """VKAdsClient не настроен → ничего не делаем, не падаем."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    with patch("src.vk_ads.client.VKAdsClient.from_settings", return_value=None):
        await check_daily_budget(bot)
    bot.send_message.assert_not_called()
