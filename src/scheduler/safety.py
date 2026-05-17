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

    Тексты reason написаны на человеческом языке (Phase 5.14, 17.05.2026)
    — без терминов CTR/CPC/конверсий. Vizit плохо понимает статистику.
    """
    # Аномалия 1: Большой расход без кликов (была до Сторожа)
    if (
        stats.spent_rub >= settings.hourly_no_click_threshold_rub
        and stats.clicks == 0
    ):
        return SafetyDecision(
            action="alert",
            reason=(
                f"💸 Объявление крутится, потрачено {stats.spent_rub:.0f}₽, "
                f"но **ни одного клика**. Это аномалия — обычно клики идут "
                f"с первых же показов. Возможно креатив совсем не цепляет "
                f"или есть технический сбой. Рекомендую выключить."
            ),
            affected_ids=[stats.campaign_id],
        )

    # Аномалия 2 (Сторож): Низкий CTR при достаточных показах
    if stats.impressions >= 1500 and stats.ctr < 0.3:
        return SafetyDecision(
            action="alert",
            reason=(
                f"📉 Объявление показалось *{stats.impressions:,}* раз, "
                f"но кликнули всего *{stats.clicks}* человек. "
                f"В нашей нише на 1000 показов нормально 6-15 кликов, "
                f"а тут получается ~{stats.ctr * 10:.0f} на 1000 — "
                f"совсем мало. Реклама как будто незаметна в ленте. "
                f"Стоит выключить."
            ).replace(",", " "),  # 2,000 → 2 000 (русский формат)
            affected_ids=[stats.campaign_id],
        )

    # Аномалия 3 (Сторож): Дорогой CPC
    if stats.clicks >= 30 and stats.cpc_rub > 30:
        return SafetyDecision(
            action="alert",
            reason=(
                f"💰 На каждого человека который кликает на это объявление "
                f"уходит *{stats.cpc_rub:.0f}₽*. В нашей нише обычно "
                f"10-20₽ за клик. Получается мы переплачиваем — VK Ads "
                f"ставит нашу рекламу в дорогие места. "
                f"Уже потрачено {stats.spent_rub:.0f}₽ на {stats.clicks} "
                f"кликов. Стоит выключить."
            ),
            affected_ids=[stats.campaign_id],
        )

    # Аномалия 4 (Сторож): Нет конверсий при значимых тратах
    if stats.spent_rub >= 400 and stats.leads == 0 and stats.clicks >= 20:
        return SafetyDecision(
            action="alert",
            reason=(
                f"🎯 На объявление кликнули *{stats.clicks}* человек "
                f"(потрачено {stats.spent_rub:.0f}₽), но **никто не написал** "
                f"в сообщество. Это значит — клики есть, а реальной "
                f"пользы нет. Возможно заголовок цепляет не ту аудиторию: "
                f"они кликают из любопытства, но молиться не хотят. "
                f"Стоит выключить."
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
