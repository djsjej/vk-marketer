"""Тесты на aggregate_stats_item — парсинг ответа VK Ads Statistics API.

Регрессионный пакет на баг 17.05.2026, когда сторож писал
"🕐 Ещё нет данных" про все 12 крутящихся кампаний, потому что
искал shows/clicks/spent на верхнем уровне `total`, а они в `rows[].base`.

Структура ответа VK API задокументирована в docs/VK_STATISTICS_API.md
и подтверждена рабочим парсером в src/agents/boba_tools.py.
"""

from src.vk_ads.models import CampaignStats, aggregate_stats_item


def test_aggregate_real_vk_response_shape():
    """Реальная структура VK: shows/clicks/spent внутри rows[].base,
    joinings внутри rows[].events. Это то, чего сторож раньше не видел."""
    item = {
        "id": 21162583,
        "rows": [
            {
                "date": "2026-05-17",
                "base": {
                    "shows": 1072,
                    "clicks": 1,
                    "spent": "132.00",
                    "ctr": 0.09,
                },
                "events": {"joinings": 0, "likes": 5},
                "uniques": {"total": 980},
            }
        ],
    }
    stats = aggregate_stats_item(item)
    assert stats is not None
    assert stats.campaign_id == 21162583
    assert stats.impressions == 1072
    assert stats.clicks == 1
    assert stats.spent_rub == 132.0
    assert stats.leads == 0


def test_aggregate_sums_across_multiple_days():
    """Если период несколько дней, метрики складываются по rows."""
    item = {
        "id": 1,
        "rows": [
            {"date": "2026-05-15", "base": {"shows": 100, "clicks": 2, "spent": "20"}, "events": {"joinings": 1}},
            {"date": "2026-05-16", "base": {"shows": 200, "clicks": 5, "spent": "50"}, "events": {"joinings": 2}},
            {"date": "2026-05-17", "base": {"shows": 300, "clicks": 8, "spent": "80"}, "events": {"joinings": 3}},
        ],
    }
    stats = aggregate_stats_item(item)
    assert stats is not None
    assert stats.impressions == 600
    assert stats.clicks == 15
    assert stats.spent_rub == 150.0
    assert stats.leads == 6


def test_aggregate_returns_none_when_no_rows():
    """Если VK ничего не возвратил по кампании (rows пустой или отсутствует),
    хелпер возвращает None — caller записывает кампанию в 'нет данных'.
    Это легитимный кейс: кампания только что запустилась, агрегация ещё идёт."""
    assert aggregate_stats_item({"id": 1, "rows": []}) is None
    assert aggregate_stats_item({"id": 1}) is None
    assert aggregate_stats_item({"id": 1, "rows": None}) is None


def test_aggregate_returns_none_when_no_id():
    """Без id агрегация смысла не имеет."""
    assert aggregate_stats_item({}) is None
    assert aggregate_stats_item({"id": 0, "rows": [{"base": {"shows": 100}}]}) is None
    assert aggregate_stats_item({"id": None, "rows": [{"base": {"shows": 100}}]}) is None


def test_aggregate_handles_zero_metrics_gracefully():
    """Кампания может крутиться 0 показов сегодня (например, паузанутая
    или не успела разогнаться) — хелпер возвращает валидный CampaignStats
    с нулями, не падает."""
    item = {
        "id": 99,
        "rows": [{"date": "2026-05-17", "base": {}, "events": {}, "uniques": {}}],
    }
    stats = aggregate_stats_item(item)
    assert stats is not None
    assert stats.impressions == 0
    assert stats.clicks == 0
    assert stats.spent_rub == 0.0
    assert stats.leads == 0


def test_aggregate_does_not_look_at_top_level_total():
    """Регрессия: до фикса сторож читал item['total']['shows']. Убедимся
    что новый парсер игнорирует это поле и берёт ТОЛЬКО из rows[].base.
    Это и есть точка где раньше всё ломалось."""
    item = {
        "id": 1,
        # Раньше парсер читал отсюда (а реальный VK эти поля не возвращает):
        "total": {"shows": 99999, "clicks": 999, "spent": "9999"},
        # А реальные данные — здесь, и они должны попасть в stats:
        "rows": [
            {"date": "2026-05-17", "base": {"shows": 100, "clicks": 5, "spent": "50"}}
        ],
    }
    stats = aggregate_stats_item(item)
    assert stats is not None
    assert stats.impressions == 100, "Должны взять из rows[].base, не из top-level total"
    assert stats.clicks == 5
    assert stats.spent_rub == 50.0


def test_aggregate_handles_string_spent():
    """VK иногда отдаёт spent строкой ('132.45'). Должны корректно конвертнуть."""
    item = {
        "id": 1,
        "rows": [{"date": "2026-05-17", "base": {"spent": "132.45"}}],
    }
    stats = aggregate_stats_item(item)
    assert stats is not None
    assert stats.spent_rub == 132.45


def test_aggregate_resilient_to_garbage_spent():
    """Если VK вдруг прислал мусор в spent (null, нечисловую строку) —
    не падаем, считаем 0 и идём дальше. Лучше слегка занизить расход
    в отчёте, чем уронить весь сторож."""
    item = {
        "id": 1,
        "rows": [
            {"base": {"spent": None, "shows": 100}},
            {"base": {"spent": "not a number", "shows": 200}},
            {"base": {"spent": "50.5", "shows": 300}},
        ],
    }
    stats = aggregate_stats_item(item)
    assert stats is not None
    assert stats.impressions == 600
    assert stats.spent_rub == 50.5  # только валидный, остальные пропущены
