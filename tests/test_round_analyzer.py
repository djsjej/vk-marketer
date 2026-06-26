"""Тесты анализатора раунда (Шаг C — самообучение)."""

from __future__ import annotations

from unittest.mock import patch

from src.knowledge import recorder, round_analyzer


def _kb(tmp_path, working: str, failed: str):
    (tmp_path / "working_combos.md").write_text(working, encoding="utf-8")
    (tmp_path / "failed_combos.md").write_text(failed, encoding="utf-8")


def test_summary_with_winners(tmp_path):
    working = (
        "# Рабочие\n\n"
        "## Связка: О здравии родителей (id 10)\n**Метрики:** ... CPL 20₽\n\n"
        "## Связка: Помяните ушедших (id 11)\n**Метрики:** ... CPL 35₽\n"
    )
    _kb(tmp_path, working, "# Провалы\n")
    with patch.object(recorder, "KNOWLEDGE_BASE_DIR", tmp_path):
        s = round_analyzer.summarize_knowledge()
    assert len(s["winners"]) == 2
    # Лучший по CPL — первый (20₽)
    assert s["winners"][0]["id"] == 10
    assert "победител" in s["recommendation"].lower()
    assert "20" in s["recommendation"]


def test_summary_only_failures(tmp_path):
    failed = "# Провалы\n\n## Провал: Дорогая связка (id 5)\n**Метрики:** ... CPL 250₽\n"
    _kb(tmp_path, "# Рабочие\n", failed)
    with patch.object(recorder, "KNOWLEDGE_BASE_DIR", tmp_path):
        s = round_analyzer.summarize_knowledge()
    assert s["winners"] == []
    assert len(s["failures"]) == 1
    assert "провал" in s["recommendation"].lower()


def test_summary_empty(tmp_path):
    _kb(tmp_path, "# Рабочие\n", "# Провалы\n")
    with patch.object(recorder, "KNOWLEDGE_BASE_DIR", tmp_path):
        s = round_analyzer.summarize_knowledge()
    assert "данных" in s["recommendation"].lower()


def test_review_results_tool(tmp_path):
    import asyncio

    working = "# Рабочие\n\n## Связка: Топ (id 1)\n**Метрики:** ... CPL 18₽\n"
    _kb(tmp_path, working, "# Провалы\n")
    from src.agents.boba_tools import review_results

    with patch.object(recorder, "KNOWLEDGE_BASE_DIR", tmp_path):
        result = asyncio.get_event_loop().run_until_complete(review_results())
    assert "Алина" in result
    assert "победител" in result.lower()
