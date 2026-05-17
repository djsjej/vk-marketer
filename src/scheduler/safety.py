"""Жёсткие правила безопасности для управления рекламой.

ЭТИ ПРАВИЛА — В КОДЕ, НЕ ДЕЛЕГИРУЕМ CLAUDE. Всё, что касается денег, должно быть
детерминированным и проверяемым.

Главный KPI православной ниши на цель «Написать в сообщество» (package_id 3127):
    CPL ≤ 50₽ — норма (хорошо <30₽, отлично <20₽)

Источник норматива — персона Алины (`src/agents/personas.py`, строка 306),
её зафиксировал Vizit 17.05.2026 после провального батча где CPL вышел 367₽
(в ~7× выше нормы). До этой даты сторож следил за CTR/CPC и не видел
проблему именно по главной метрике.
"""

import logging

from src.config import settings
from src.vk_ads.models import CampaignStats

logger = logging.getLogger(__name__)


# Норматив CPL для православной ниши на цель «Написать в сообщество».
# Если кампания крутится с другой целью — это приближение, но в большинстве
# случаев нам важен именно этот бенчмарк (см. персону Алины).
CPL_LIMIT_RUB = 50.0

# Минимальный расход, после которого имеет смысл судить о CPL.
# До этого порога мы говорим «ждём данных» вместо алерта.
# Логика: если за 50₽ не пришёл ни один написавший, значит CPL уже точно > 50,
# но это может быть просто плохой первый разогрев. На рубеже 100₽ — это уже
# два цикла норматива, и можно делать вывод.
CPL_JUDGE_THRESHOLD_RUB = 100.0


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
    """Проверяет одну кампанию на аномалии — правила Сторожа.

    Главный критерий — CPL (стоимость одного написавшего). Норма ≤ 50₽
    для православной ниши на цель «Написать в сообщество». CTR и CPC
    игнорируем как вторичные: рекламу мы оптимизируем не по кликам,
    а по реальным обращениям.

    Возвращает SafetyDecision с action='ok' если всё в норме, или 'alert'
    если есть проблемы. action='pause' оставлено для будущей версии с
    auto-pause — пока Сторож только уведомляет, Vizit сам выключает.

    Тексты reason — человеческим языком, без терминов CTR/CPC.
    """
    # Аномалия 1: технический сбой — крутится, но НИ ОДНОГО клика.
    # Это не про дорогую рекламу, это про что-то странное (битый URL,
    # отклонённый креатив в показах но не в статусе, или подобное).
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

    # До порога CPL_JUDGE_THRESHOLD_RUB у нас слишком мало данных,
    # чтобы делать выводы — даже хорошая кампания может первые 50-80₽
    # потратить без конверсий, пока VK Ads подбирает аудиторию.
    if stats.spent_rub < CPL_JUDGE_THRESHOLD_RUB:
        return SafetyDecision(action="ok")

    # Аномалия 2: потрачено достаточно, а лидов нет → CPL уже точно
    # превысил норматив и продолжает расти.
    if stats.leads == 0:
        return SafetyDecision(
            action="alert",
            reason=(
                f"🎯 Потрачено уже *{stats.spent_rub:.0f}₽*, кликов "
                f"{stats.clicks}, но **никто не написал** в сообщество. "
                f"Норма в нашей нише — один написавший за ≤{CPL_LIMIT_RUB:.0f}₽. "
                f"Сейчас CPL уже выше норматива и продолжает расти. "
                f"Стоит выключить и переделать креатив."
            ),
            affected_ids=[stats.campaign_id],
        )

    # Аномалия 3: главное правило — CPL выше норматива.
    cpl = stats.cpl_rub
    if cpl > CPL_LIMIT_RUB:
        return SafetyDecision(
            action="alert",
            reason=(
                f"💰 Каждый написавший в сообщество обходится "
                f"в *{cpl:.0f}₽* (потрачено {stats.spent_rub:.0f}₽ на "
                f"{stats.leads} человек). В нашей нише норма — "
                f"≤{CPL_LIMIT_RUB:.0f}₽ за написавшего. Сейчас в "
                f"{cpl / CPL_LIMIT_RUB:.1f}× дороже. Стоит выключить."
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
