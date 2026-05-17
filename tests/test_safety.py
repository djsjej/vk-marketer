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


# ============================================================================
# CPL-based анализ Сторожа (Phase 5.16, 17.05.2026)
#
# Норматив CPL ≤ 50₽ для православной ниши на цель «Написать в сообщество».
# Зафиксирован Vizit'ом после провального батча где CPL вышел 367₽ (×7
# норма) — старая логика смотрела только на CTR/CPC и не видела самой
# главной метрики. Сейчас CPL — главное правило, CTR/CPC игнорируем.
# ============================================================================


def test_cpl_under_limit_returns_ok():
    """1 написавший за 40₽ — это норма (50₽ — граница)."""
    stats = CampaignStats(
        campaign_id=1, impressions=2000, clicks=10, spent_rub=200, leads=5
    )
    # CPL = 200/5 = 40₽ — в пределах нормы
    assert stats.cpl_rub == 40.0
    decision = check_campaign_anomaly(stats)
    assert decision.action == "ok"


def test_cpl_over_limit_triggers_alert():
    """5 написавших за 367₽ — точно дороже норматива, алерт."""
    stats = CampaignStats(
        campaign_id=42, impressions=2000, clicks=10, spent_rub=367, leads=5
    )
    # CPL = 367/5 = 73.4₽ — выше 50₽
    assert stats.cpl_rub > 50
    decision = check_campaign_anomaly(stats)
    assert decision.action == "alert"
    assert 42 in decision.affected_ids
    # В тексте упоминается реальное число и норматив
    assert "73" in decision.reason
    assert "50" in decision.reason


def test_cpl_at_exact_limit_returns_ok():
    """Ровно на границе 50₽ — это норма, не алерт."""
    stats = CampaignStats(
        campaign_id=1, impressions=1000, clicks=5, spent_rub=500, leads=10
    )
    # CPL = 500/10 = 50₽ — ровно граница
    assert stats.cpl_rub == 50.0
    decision = check_campaign_anomaly(stats)
    assert decision.action == "ok"


def test_no_leads_with_significant_spend_triggers_alert():
    """Потрачено 150₽, ни одного написавшего — CPL уже точно > 50, алерт."""
    stats = CampaignStats(
        campaign_id=99, impressions=2000, clicks=10, spent_rub=150, leads=0
    )
    decision = check_campaign_anomaly(stats)
    assert decision.action == "alert"
    assert 99 in decision.affected_ids
    assert "никто не написал" in decision.reason.lower() or "не написал" in decision.reason


def test_no_leads_with_low_spend_waits():
    """Потрачено меньше порога (50₽), лидов 0 — рано судить, ждём."""
    stats = CampaignStats(
        campaign_id=1, impressions=500, clicks=3, spent_rub=50, leads=0
    )
    # Хотя CPL формально бесконечный (0 лидов), мы не алертим пока spent<100
    decision = check_campaign_anomaly(stats)
    assert decision.action == "ok"


def test_low_ctr_alone_no_longer_alerts():
    """Регрессия: до 17.05.2026 сторож алертил при CTR<0.3%. Теперь CTR
    вторичен — если CPL в норме, низкий CTR не должен триггерить."""
    # Низкий CTR (0.2%) но шикарная конверсия (5 написавших на 5 кликов)
    stats = CampaignStats(
        campaign_id=1, impressions=2500, clicks=5, spent_rub=200, leads=5
    )
    assert stats.ctr < 0.3
    assert stats.cpl_rub == 40.0  # в норме
    decision = check_campaign_anomaly(stats)
    # Главное — CPL норм, значит OK, не алерт
    assert decision.action == "ok"


def test_high_cpc_alone_no_longer_alerts():
    """Регрессия: дорогой CPC сам по себе больше не повод выключать —
    если за дорогие клики приходят качественные лиды по нормативу."""
    # CPC = 50₽ за клик (дорого по старому правилу), но 10 написавших
    # из 30 кликов = CPL 1500/10 = 150₽... это всё ещё высокий CPL,
    # сделаем чище: CPC 50, 30 кликов, 30 лидов → CPL 50₽
    stats = CampaignStats(
        campaign_id=1, impressions=1000, clicks=30, spent_rub=1500, leads=30
    )
    assert stats.cpc_rub == 50.0
    assert stats.cpl_rub == 50.0  # ровно граница
    decision = check_campaign_anomaly(stats)
    assert decision.action == "ok"
