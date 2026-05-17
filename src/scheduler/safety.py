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
    """Проверяет одну кампанию на аномалии — правила Сторожа (Phase 5.12).

    Возвращает SafetyDecision с action='ok' если всё в норме, или 'alert'
    если есть проблемы. action='pause' оставлено для будущей версии с
    auto-pause — пока Сторож только уведомляет, Vizit сам выключает.
    """
    # Аномалия 1: Большой расход без кликов (была до Сторожа)
    if (
        stats.spent_rub >= settings.hourly_no_click_threshold_rub
        and stats.clicks == 0
    ):
        return SafetyDecision(
            action="alert",
            reason=(
                f"💸 Расход {stats.spent_rub:.0f}₽ без единого клика. "
                f"Аномалия — либо креатив не работает, либо технический сбой."
            ),
            affected_ids=[stats.campaign_id],
        )

    # Аномалия 2 (Сторож): Низкий CTR при достаточных показах
    # Порог 0.3% — это уже плохой результат для тёплой ЦА. Норма 0.6-1.5%.
    # Минимум 1500 показов чтобы значение CTR было статистически осмысленным.
    if stats.impressions >= 1500 and stats.ctr < 0.3:
        return SafetyDecision(
            action="alert",
            reason=(
                f"📉 CTR {stats.ctr:.2f}% при {stats.impressions} показах. "
                f"Креатив не цепляет — рекомендую выключить."
            ),
            affected_ids=[stats.campaign_id],
        )

    # Аномалия 3 (Сторож): Дорогой CPC
    # 30₽ — это потолок для нашей ниши. Норма 10-20₽.
    # Минимум 30 кликов чтобы средний CPC был осмысленным.
    if stats.clicks >= 30 and stats.cpc_rub > 30:
        return SafetyDecision(
            action="alert",
            reason=(
                f"💰 CPC {stats.cpc_rub:.0f}₽ при {stats.clicks} кликах. "
                f"Слишком дорого — рекомендую выключить или снизить ставку."
            ),
            affected_ids=[stats.campaign_id],
        )

    # Аномалия 4 (Сторож): Нет конверсий при значимых тратах
    # Если потратили 400₽+ и нет ни одной заявки/сообщения — кампания
    # привлекает не целевую аудиторию. Часто это сигнал что заголовок
    # обманывает (CTR может быть нормальный, а конверсий 0).
    if stats.spent_rub >= 400 and stats.leads == 0 and stats.clicks >= 20:
        return SafetyDecision(
            action="alert",
            reason=(
                f"🎯 Потрачено {stats.spent_rub:.0f}₽, {stats.clicks} кликов, "
                f"но 0 конверсий. Креатив привлекает не ту аудиторию."
            ),
            affected_ids=[stats.campaign_id],
        )

    return SafetyDecision(action="ok")


def requires_confirmation(action: str, budget_rub: int) -> bool:
    """Нужно ли подтверждение пользователя для этого действия."""
    if action == "create_campaign" and budget_rub > settings.auto_launch_limit_rub:
        return True
    if action in ("scale_up_significantly", "manual_override"):
        return True
    return False
