"""Тесты Сторожа (auto-monitoring рекламы).

Сторож проверяет активные кампании каждый час, при провале по CPL шлёт
алерт. Auto-pause нет — Vizit сам выключает через /pause или /kill_bad.

История: до 17.05.2026 (Phase 5.12-5.14) сторож алертил по CTR и CPC.
17.05 после провального батча (CPL = 367₽ при норме ≤50₽) переписан на
CPL-based анализ (Phase 5.16). Эти тесты — регрессия на новые правила
и явная проверка что старые срабатывания (низкий CTR / дорогой CPC)
больше не триггерят алерт, если главная метрика — CPL — в норме.
"""

from __future__ import annotations

from src.scheduler.safety import check_campaign_anomaly
from src.vk_ads.models import CampaignStats


# ============================================================================
# Регрессии на старые правила — теперь они НЕ должны срабатывать,
# если CPL в норме. Это часть Phase 5.16 фикса: главная метрика
# теперь CPL, CTR и CPC вторичны.
# ============================================================================


def test_low_ctr_no_longer_alerts_when_cpl_normal():
    """CTR 0.2% (низкий) но CPL 40₽ (в норме) → ok.
    До Phase 5.16 эта кампания получала бы алерт «низкий CTR»."""
    stats = CampaignStats(
        campaign_id=100,
        impressions=2000,
        clicks=4,  # CTR = 0.2%
        spent_rub=200,
        leads=5,  # CPL = 40₽ — в норме
    )
    decision = check_campaign_anomaly(stats)
    assert decision.action == "ok"


def test_high_cpc_no_longer_alerts_when_cpl_normal():
    """CPC 37.5₽ (выше старого порога 30₽) но CPL 30₽ (хорошо) → ok.
    Дорогие клики прощаются если конверсия их компенсирует."""
    stats = CampaignStats(
        campaign_id=200,
        impressions=5000,
        clicks=40,
        spent_rub=1500,  # CPC = 37.5₽
        leads=50,  # CPL = 30₽ — хорошо
    )
    decision = check_campaign_anomaly(stats)
    assert decision.action == "ok"


# ============================================================================
# Главное правило сторожа — алерт по CPL > 50₽
# ============================================================================


def test_high_cpl_alerts():
    """CPL = 100₽ (двойной норматив) → алерт."""
    stats = CampaignStats(
        campaign_id=42,
        impressions=2000,
        clicks=20,
        spent_rub=500,
        leads=5,  # CPL = 100₽
    )
    decision = check_campaign_anomaly(stats)
    assert decision.action == "alert"
    assert 42 in decision.affected_ids
    assert "100" in decision.reason
    assert "50" in decision.reason


def test_no_conversions_at_significant_spend_alerts():
    """Потрачено больше порога суждения, 0 лидов → алерт «никто не написал»."""
    stats = CampaignStats(
        campaign_id=300,
        impressions=3000,
        clicks=25,
        spent_rub=500,
        leads=0,
    )
    decision = check_campaign_anomaly(stats)
    assert decision.action == "alert"
    assert "не написал" in decision.reason


def test_normal_cpl_passes():
    """CPL = 40₽ (в норме) → ok."""
    stats = CampaignStats(
        campaign_id=400,
        impressions=5000,
        clicks=50,
        spent_rub=200,
        leads=5,  # CPL = 40₽
    )
    decision = check_campaign_anomaly(stats)
    assert decision.action == "ok"


def test_excellent_cpl_passes():
    """CPL = 20₽ (отлично) → ok."""
    stats = CampaignStats(
        campaign_id=401,
        impressions=10000,
        clicks=100,
        spent_rub=200,
        leads=10,  # CPL = 20₽
    )
    decision = check_campaign_anomaly(stats)
    assert decision.action == "ok"


# ============================================================================
# In-memory state для /kill_bad
# ============================================================================


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
    """CampaignStats.cpc_rub считается правильно (для UI/Алины,
    но в правилах сторожа больше не используется)."""
    stats = CampaignStats(
        campaign_id=1, impressions=1000, clicks=50, spent_rub=500, leads=0
    )
    assert stats.cpc_rub == 10.0

    stats_no_clicks = CampaignStats(
        campaign_id=1, impressions=1000, clicks=0, spent_rub=100, leads=0
    )
    assert stats_no_clicks.cpc_rub == 0.0
