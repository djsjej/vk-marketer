"""Тесты авто-записи результатов в базу знаний (Шаг B)."""

from __future__ import annotations

from unittest.mock import patch

from src.knowledge import recorder


def _setup(tmp_path):
    (tmp_path / "working_combos.md").write_text("# Рабочие связки\n", encoding="utf-8")
    (tmp_path / "failed_combos.md").write_text("# Провальные связки\n", encoding="utf-8")


def test_records_failure(tmp_path):
    _setup(tmp_path)
    with patch.object(recorder, "KNOWLEDGE_BASE_DIR", tmp_path):
        ok = recorder.record_campaign_result(
            campaign_id=42, name="bad", impressions=3000, clicks=20,
            spent_rub=500, leads=2, cpl_rub=250, won=False, today="2026-06-26",
        )
    assert ok is True
    text = (tmp_path / "failed_combos.md").read_text(encoding="utf-8")
    assert "(id 42)" in text
    assert "CPL 250" in text


def test_records_winner_in_working(tmp_path):
    _setup(tmp_path)
    with patch.object(recorder, "KNOWLEDGE_BASE_DIR", tmp_path):
        recorder.record_campaign_result(
            campaign_id=7, name="good", impressions=5000, clicks=120,
            spent_rub=300, leads=15, cpl_rub=20, won=True, today="2026-06-26",
        )
    win = (tmp_path / "working_combos.md").read_text(encoding="utf-8")
    fail = (tmp_path / "failed_combos.md").read_text(encoding="utf-8")
    assert "(id 7)" in win
    assert "(id 7)" not in fail


def test_dedup_same_campaign_once(tmp_path):
    _setup(tmp_path)
    with patch.object(recorder, "KNOWLEDGE_BASE_DIR", tmp_path):
        first = recorder.record_campaign_result(
            campaign_id=5, name="x", impressions=1, clicks=1, spent_rub=500,
            leads=0, cpl_rub=0, won=False, today="2026-06-26",
        )
        second = recorder.record_campaign_result(
            campaign_id=5, name="x", impressions=1, clicks=1, spent_rub=500,
            leads=0, cpl_rub=0, won=False, today="2026-06-26",
        )
    assert first is True
    assert second is False
    text = (tmp_path / "failed_combos.md").read_text(encoding="utf-8")
    assert text.count("(id 5)") == 1


def test_marks_small_sample(tmp_path):
    _setup(tmp_path)
    with patch.object(recorder, "KNOWLEDGE_BASE_DIR", tmp_path):
        recorder.record_campaign_result(
            campaign_id=9, name="x", impressions=500, clicks=10, spent_rub=300,
            leads=4, cpl_rub=75, won=False, today="2026-06-26",
        )
    text = (tmp_path / "failed_combos.md").read_text(encoding="utf-8")
    assert "выборка мала" in text
