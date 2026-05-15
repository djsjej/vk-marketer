"""Фильтрация горячей аудитории по полу/возрасту/гео.

Phase 5.5 — после того как мы нашли горячую аудиторию через пересечения,
сужаем её до **реальной ЦА pomolimsy** (женщины 41-58, Россия). Делаем
это через VK API users.get с полями sex, bdate, city.

Зачем: горячая аудитория из 5000 человек с 60% женщин 41-58 = 3000
конверсионных. Загружать в VK Ads все 5000 без фильтра — это всё ещё
работает (VK Ads сам сужает по возрасту), но даёт хуже точность
ремаркетинга и тратит часть бюджета на не-ЦА.

Также важно: VK скрывает дату рождения у многих людей. Те у кого
bdate в принципе недоступна — пропускаются. Это нормально, VK Ads
обычно может вычислить возраст по поведению.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger(__name__)


@dataclass
class AudienceFilter:
    """Параметры фильтра ЦА pomolimsy."""

    sex: int | None = 1  # 1=женский, 2=мужской, None=любой
    min_age: int | None = 41
    max_age: int | None = 58
    # Гео не фильтруем здесь — VK Ads сам делает гео в targetings.
    # Но можно при необходимости добавить country_id или city_ids.


@dataclass
class FilterStats:
    """Статистика после фильтрации — полезно показать Vizit'у."""

    total_input: int
    matched: int
    skipped_no_bdate: int   # bdate скрыт → не можем посчитать возраст
    skipped_wrong_sex: int
    skipped_wrong_age: int
    skipped_deactivated: int  # удалённые/заблокированные

    @property
    def matched_pct(self) -> float:
        return (self.matched / self.total_input * 100) if self.total_input else 0.0


def parse_age_from_bdate(bdate: str) -> int | None:
    """Парсит возраст из VK-формата bdate.

    VK возвращает bdate в одном из трёх форматов:
    - 'D.M.YYYY' — день, месяц, год (полная дата) → можем вычислить возраст
    - 'D.M' — только день и месяц, год скрыт пользователем → возраст НЕ знаем
    - '' или поля нет — bdate вообще не указан

    Returns:
        Возраст в годах если можно вычислить, иначе None.
    """
    if not bdate:
        return None

    parts = bdate.split(".")
    if len(parts) != 3:
        return None  # только день+месяц, год скрыт

    try:
        day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
    except (ValueError, IndexError):
        return None

    if year < 1900 or year > 2025:
        return None  # явно невалидный год

    today = date.today()
    age = today.year - year
    # Корректировка если день рождения ещё не наступил в этом году
    if (today.month, today.day) < (month, day):
        age -= 1

    return age if age >= 0 else None


def filter_audience(
    profiles: list[dict],
    audience_filter: AudienceFilter,
) -> tuple[list[int], FilterStats]:
    """Фильтрует список профилей по полу и возрасту.

    Args:
        profiles: результат VKAPIClient.users_get_batch — список dict
            с полями id, sex, bdate (если доступен), city, может быть
            deactivated.
        audience_filter: параметры фильтра. По умолчанию ЦА pomolimsy:
            женщины 41-58.

    Returns:
        Кортеж (matched_ids, stats):
        - matched_ids — список user_id прошедших фильтр
        - stats — статистика что отсеялось почему
    """
    matched_ids: list[int] = []
    stats = FilterStats(
        total_input=len(profiles),
        matched=0,
        skipped_no_bdate=0,
        skipped_wrong_sex=0,
        skipped_wrong_age=0,
        skipped_deactivated=0,
    )

    for profile in profiles:
        uid = profile.get("id")
        if not uid:
            continue

        # Удалённые/заблокированные
        if profile.get("deactivated"):
            stats.skipped_deactivated += 1
            continue

        # Пол
        if audience_filter.sex is not None:
            user_sex = profile.get("sex", 0)
            if user_sex != audience_filter.sex:
                stats.skipped_wrong_sex += 1
                continue

        # Возраст (если нужен фильтр)
        if audience_filter.min_age is not None or audience_filter.max_age is not None:
            bdate = profile.get("bdate", "")
            age = parse_age_from_bdate(bdate)
            if age is None:
                # bdate скрыт или не парсится — пропускаем
                stats.skipped_no_bdate += 1
                continue
            if audience_filter.min_age is not None and age < audience_filter.min_age:
                stats.skipped_wrong_age += 1
                continue
            if audience_filter.max_age is not None and age > audience_filter.max_age:
                stats.skipped_wrong_age += 1
                continue

        # Прошёл все фильтры
        matched_ids.append(uid)
        stats.matched += 1

    return matched_ids, stats
