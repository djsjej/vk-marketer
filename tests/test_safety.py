"""Тесты safety-правил — критично, потому что эти правила охраняют бюджет."""

import pytest

from src.scheduler.safety import check_campaign_anomaly, check_daily_spend
from src.vk_ads.models import CampaignStats


def test_daily_spend_under_limit_returns_ok(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "max_daily_spend_rub", 2000)
    decision = check_daily_spend(1500)
    assert decision.action == "ok"


def test_daily_spend_at_limit_returns_stop_all(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "max_daily_spend_rub", 2000)
    decision = check_daily_spend(2000)
    assert decision.action == "stop_all"


def test_daily_spend_over_limit_returns_stop_all(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "max_daily_spend_rub", 2000)
    decision = check_daily_spend(2500)
    assert decision.action == "stop_all"
    assert "2500" in decision.reason


def test_high_spend_no_clicks_pauses_campaign(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "hourly_no_click_threshold_rub", 300)

    stats = CampaignStats(
        campaign_id=42, impressions=10000, clicks=0, spent_rub=400, leads=0
    )
    decision = check_campaign_anomaly(stats)
    assert decision.action == "alert"
    assert 42 in decision.affected_ids


def test_normal_campaign_returns_ok():
    stats = CampaignStats(
        campaign_id=42, impressions=1000, clicks=10, spent_rub=50, leads=2
    )
    decision = check_campaign_anomaly(stats)
    assert decision.action == "ok"


def test_ctr_calculation():
    stats = CampaignStats(campaign_id=1, impressions=1000, clicks=10)
    assert stats.ctr == 1.0


def test_cpl_calculation():
    stats = CampaignStats(campaign_id=1, spent_rub=300, leads=3)
    assert stats.cpl_rub == 100.0


def test_cpl_with_zero_leads():
    stats = CampaignStats(campaign_id=1, spent_rub=300, leads=0)
    assert stats.cpl_rub == 0.0
