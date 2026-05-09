"""Жёсткие правила безопасности для управления рекламой.

ЭТИ ПРАВИЛА — В КОДЕ, НЕ ДЕЛЕГИРУЕМ CLAUDE. Всё, что касается денег, должно быть
детерминированным и проверяемым.
"""

import logging

from src.config import settings
from src.vk_ads.models import CampaignStats

logger = logging.getLogger(__name__)


class SafetyDecision:
    """Решение safety-правил."""

    def __init__(
        self,
        action: str,  # "ok", "pause", "stop_all"
        reason: str = "",
        affected_ids: list[int] | None = None,
    ):
        self.action = action
        self.reason = reason
        self.affected_ids = affected_ids or []

    def __repr__(self) -> str:
        return f"SafetyDecision(action={self.action}, reason='{self.reason}')"


def check_daily_spend(total_spent_rub: float) -> SafetyDecision:
    """Проверяет, не превышен ли дневной лимит расхода."""
    if total_spent_rub >= settings.max_daily_spend_rub:
        return SafetyDecision(
            action="stop_all",
            reason=(
                f"Дневной расход {total_spent_rub:.0f}₽ достиг лимита "
                f"{settings.max_daily_spend_rub}₽. Останавливаю все кампании."
            ),
        )
    return SafetyDecision(action="ok")


def check_campaign_anomaly(stats: CampaignStats) -> SafetyDecision:
    """Проверяет одну кампанию на аномалии."""
    # Аномалия 1: Большой расход за час без кликов
    if (
        stats.spent_rub >= settings.hourly_no_click_threshold_rub
        and stats.clicks == 0
    ):
        return SafetyDecision(
            action="pause",
            reason=(
                f"Кампания #{stats.campaign_id}: расход {stats.spent_rub:.0f}₽ "
                f"без кликов. Аномалия — пауза."
            ),
            affected_ids=[stats.campaign_id],
        )

    # Здесь можно добавить другие правила:
    # - CTR < X% после Y показов
    # - CPL > Z после N заявок
    # - Резкие изменения метрик за короткий период

    return SafetyDecision(action="ok")


def requires_confirmation(action: str, budget_rub: int) -> bool:
    """Нужно ли подтверждение пользователя для этого действия."""
    if action == "create_campaign" and budget_rub > settings.auto_launch_limit_rub:
        return True
    if action in ("scale_up_significantly", "manual_override"):
        return True
    return False
