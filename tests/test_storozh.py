"""Тесты Phase 5.12 — Сторож (auto-monitoring рекламы).

Сторож проверяет активные кампании каждый час, при пробитии порогов
(CTR, CPC, нулевые конверсии) шлёт алерт. Auto-pause нет — Vizit сам
выключает через /pause или /kill_bad.
"""

from __future__ import annotations

from src.scheduler.safety import check_campaign_anomaly
from src.vk_ads.models import CampaignStats


def test_low_ctr_alerts():
    """CTR < 0.3% при показах >= 1500 → alert."""
    stats = CampaignStats(
        campaign_id=100,
        impressions=2000,
        clicks=4,  # CTR = 0.2%
        spent_rub=200,
        leads=0,
    )
    decision = check_campaign_anomaly(stats)
    assert decision.action == "alert"
    assert "кликнули" in decision.reason
    assert 100 in decision.affected_ids


def test_low_ctr_but_few_impressions_ok():
    """CTR < 0.3% но показов < 1500 → ok (статистика мала)."""
    stats = CampaignStats(
        campaign_id=100,
        impressions=500,
        clicks=1,  # CTR = 0.2%, но показов мало
        spent_rub=100,
        leads=0,
    )
    decision = check_campaign_anomaly(stats)
    assert decision.action == "ok"


def test_high_cpc_alerts():
    """CPC > 30₽ при кликах >= 30 → alert."""
    stats = CampaignStats(
        campaign_id=200,
        impressions=5000,
        clicks=40,
        spent_rub=1500,  # CPC = 37.5₽
        leads=2,
    )
    decision = check_campaign_anomaly(stats)
    assert decision.action == "alert"
    assert "переплачиваем" in decision.reason
    assert 200 in decision.affected_ids


def test_high_cpc_but_few_clicks_ok():
    """CPC высокий но кликов < 30 → ok (мало данных)."""
    stats = CampaignStats(
        campaign_id=200,
        impressions=1000,
        clicks=5,
        spent_rub=200,  # CPC = 40₽, но кликов мало
        leads=0,
    )
    decision = check_campaign_anomaly(stats)
    # Этот случай попадёт под "low CTR" (5/1000=0.5%, выше порога 0.3%)
    # значит должно быть ok
    assert decision.action == "ok"


def test_no_conversions_at_high_spend_alerts():
    """Потрачено 400+, кликов 20+, 0 конверсий → alert."""
    stats = CampaignStats(
        campaign_id=300,
        impressions=3000,
        clicks=25,
        spent_rub=500,
        leads=0,
    )
    decision = check_campaign_anomaly(stats)
    assert decision.action == "alert"
    assert "никто не написал" in decision.reason


def test_good_campaign_passes():
    """Нормальные метрики → ok."""
    stats = CampaignStats(
        campaign_id=400,
        impressions=5000,
        clicks=50,  # CTR = 1.0%
        spent_rub=500,  # CPC = 10₽
        leads=5,
    )
    decision = check_campaign_anomaly(stats)
    assert decision.action == "ok"


def test_bad_campaigns_state():
    """In-memory state работает: set → get → clear."""
    from src.scheduler import bad_campaigns_state

    bad_campaigns_state.clear()
    ids, _ = bad_campaigns_state.get_bad_ids()
    assert ids == []

    bad_campaigns_state.set_bad_ids([100, 200, 300])
    ids, ts = bad_campaigns_state.get_bad_ids()
    assert ids == [100, 200, 300]
    assert ts > 0

    bad_campaigns_state.clear()
    ids, _ = bad_campaigns_state.get_bad_ids()
    assert ids == []


def test_campaign_stats_cpc_property():
    """CampaignStats.cpc_rub считается правильно."""
    stats = CampaignStats(
        campaign_id=1, impressions=1000, clicks=50, spent_rub=500, leads=0
    )
    assert stats.cpc_rub == 10.0

    # Деление на ноль обрабатывается
    stats_no_clicks = CampaignStats(
        campaign_id=1, impressions=1000, clicks=0, spent_rub=100, leads=0
    )
    assert stats_no_clicks.cpc_rub == 0.0
