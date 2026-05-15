"""Тесты фильтра ЦА (Phase 5.5)."""

from __future__ import annotations

from datetime import date

import pytest

from src.targetolog.audience_filter import (
    AudienceFilter,
    filter_audience,
    parse_age_from_bdate,
)


# --- parse_age_from_bdate ---


def test_parse_age_from_full_bdate():
    """'15.6.1980' → возраст вычисляется корректно."""
    current_year = date.today().year
    bdate = f"15.6.1980"
    age = parse_age_from_bdate(bdate)
    # Возраст ~ current_year - 1980, может быть -1 если ДР ещё не прошёл
    expected_min = current_year - 1980 - 1
    expected_max = current_year - 1980
    assert age in (expected_min, expected_max), f"got {age}"


def test_parse_age_returns_none_for_partial_bdate():
    """'15.6' без года → None (год скрыт пользователем)."""
    assert parse_age_from_bdate("15.6") is None


def test_parse_age_returns_none_for_empty():
    """Пустая строка → None."""
    assert parse_age_from_bdate("") is None


def test_parse_age_returns_none_for_invalid():
    """Невалидный формат → None."""
    assert parse_age_from_bdate("abc.def.ghi") is None
    assert parse_age_from_bdate("99.99.1800") is None  # год слишком старый


# --- filter_audience ---


def test_filter_matches_target_women():
    """Женщины 41-58 проходят фильтр, остальные нет."""
    current_year = date.today().year

    profiles = [
        {"id": 1, "sex": 1, "bdate": f"1.1.{current_year - 45}"},  # женщина 44-45 — пройдёт
        {"id": 2, "sex": 1, "bdate": f"1.1.{current_year - 50}"},  # женщина 49-50 — пройдёт
        {"id": 3, "sex": 2, "bdate": f"1.1.{current_year - 50}"},  # мужчина — отсеется (пол)
        {"id": 4, "sex": 1, "bdate": f"1.1.{current_year - 25}"},  # женщина 24-25 — отсеется (возраст)
        {"id": 5, "sex": 1, "bdate": f"1.1.{current_year - 70}"},  # женщина 69-70 — отсеется (возраст)
        {"id": 6, "sex": 1, "bdate": "15.5"},                       # женщина с скрытым годом — отсеется
        {"id": 7, "sex": 1, "deactivated": "deleted"},              # удалённая — отсеется
    ]

    matched, stats = filter_audience(profiles, AudienceFilter())

    assert matched == [1, 2]
    assert stats.total_input == 7
    assert stats.matched == 2
    assert stats.skipped_wrong_sex == 1
    assert stats.skipped_wrong_age == 2
    assert stats.skipped_no_bdate == 1
    assert stats.skipped_deactivated == 1


def test_filter_stats_percentage():
    """matched_pct считается корректно."""
    profiles = [
        {"id": 1, "sex": 1, "bdate": "1.1.1980"},  # женщина средних лет → пройдёт
    ]
    matched, stats = filter_audience(profiles, AudienceFilter())
    if matched:
        assert stats.matched_pct == 100.0
    else:
        assert stats.matched_pct == 0.0


def test_filter_no_constraints():
    """Если все ограничения None — все живые профили проходят."""
    profiles = [
        {"id": 1, "sex": 1, "bdate": "1.1.1980"},
        {"id": 2, "sex": 2, "bdate": "1.1.1990"},
        {"id": 3, "deactivated": "banned"},
    ]
    no_filter = AudienceFilter(sex=None, min_age=None, max_age=None)
    matched, stats = filter_audience(profiles, no_filter)

    assert set(matched) == {1, 2}
    assert stats.skipped_deactivated == 1


def test_filter_empty_input():
    """Пустой список → пустой результат, нулевая статистика."""
    matched, stats = filter_audience([], AudienceFilter())
    assert matched == []
    assert stats.total_input == 0
    assert stats.matched == 0
    assert stats.matched_pct == 0.0


# ============================================================
# Phase 5.7 — фильтр по last_seen (активность)
# ============================================================

import time


def _ago_seconds(seconds: int) -> int:
    """Возвращает unix timestamp N секунд назад."""
    return int(time.time()) - seconds


def test_filter_last_seen_keeps_recent():
    """Активные пользователи (last_seen в пределах N дней) проходят."""
    profiles = [
        # Был онлайн 5 дней назад — проходит (порог 14 дней)
        {"id": 1, "last_seen": {"time": _ago_seconds(5 * 86400)}},
        # Был онлайн вчера — проходит
        {"id": 2, "last_seen": {"time": _ago_seconds(1 * 86400)}},
    ]
    flt = AudienceFilter(sex=None, min_age=None, max_age=None, min_last_seen_days=14)
    matched, stats = filter_audience(profiles, flt)

    assert set(matched) == {1, 2}
    assert stats.skipped_inactive == 0


def test_filter_last_seen_drops_inactive():
    """Пользователи которых не было N+ дней — отсеиваются."""
    profiles = [
        # Был онлайн 30 дней назад — отсеивается (порог 14)
        {"id": 1, "last_seen": {"time": _ago_seconds(30 * 86400)}},
        # Был онлайн 100 дней назад — отсеивается
        {"id": 2, "last_seen": {"time": _ago_seconds(100 * 86400)}},
    ]
    flt = AudienceFilter(sex=None, min_age=None, max_age=None, min_last_seen_days=14)
    matched, stats = filter_audience(profiles, flt)

    assert matched == []
    assert stats.skipped_inactive == 2


def test_filter_last_seen_missing_field_treated_inactive():
    """Если last_seen отсутствует (скрытый профиль) — считаем неактивным."""
    profiles = [
        {"id": 1},  # вообще нет last_seen
        {"id": 2, "last_seen": None},  # last_seen=None
        {"id": 3, "last_seen": {}},  # есть, но без time
    ]
    flt = AudienceFilter(sex=None, min_age=None, max_age=None, min_last_seen_days=14)
    matched, stats = filter_audience(profiles, flt)

    assert matched == []
    assert stats.skipped_inactive == 3


def test_filter_last_seen_disabled_when_none():
    """Если min_last_seen_days=None — фильтр не применяется."""
    profiles = [
        {"id": 1, "last_seen": {"time": _ago_seconds(365 * 86400)}},  # год назад
        {"id": 2},  # нет last_seen
    ]
    flt = AudienceFilter(sex=None, min_age=None, max_age=None, min_last_seen_days=None)
    matched, stats = filter_audience(profiles, flt)

    assert set(matched) == {1, 2}
    assert stats.skipped_inactive == 0


def test_filter_deactivated_priority_over_inactive():
    """deactivated проверяется ПЕРЕД last_seen — это правильнее
    статистически (бан/удаление более важная категория)."""
    profiles = [
        # deactivated + старый last_seen — должен попасть в deactivated
        {"id": 1, "deactivated": "banned", "last_seen": {"time": _ago_seconds(100 * 86400)}},
    ]
    flt = AudienceFilter(sex=None, min_age=None, max_age=None, min_last_seen_days=14)
    matched, stats = filter_audience(profiles, flt)

    assert matched == []
    assert stats.skipped_deactivated == 1
    assert stats.skipped_inactive == 0  # НЕ учтён в inactive
