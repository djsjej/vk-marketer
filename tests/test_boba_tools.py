"""Тесты для Phase 7 — tools у Бобы и tool use loop в DialogAgent."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.boba_tools import (
    BOBA_TOOLS_SCHEMA,
    KNOWLEDGE_BASE_DIR,
    append_knowledge,
    execute_tool,
    read_knowledge,
    run_audience_parsing,
)
from src.agents.dialog import DialogAgent


# ============================================================
# Tests for individual tools
# ============================================================


@pytest.mark.asyncio
async def test_read_knowledge_returns_file_content():
    """read_knowledge читает существующий файл базы знаний."""
    result = await read_knowledge("orthodox_calendar")
    assert "Радоница" in result
    assert "📄" in result  # маркер успешного чтения


@pytest.mark.asyncio
async def test_read_knowledge_rejects_arbitrary_files():
    """read_knowledge не читает произвольные файлы (защита от ../../etc/passwd)."""
    result = await read_knowledge("../../../etc/passwd")
    assert "не разрешён" in result


@pytest.mark.asyncio
async def test_read_knowledge_handles_missing_file():
    """read_knowledge корректно обрабатывает несуществующий файл."""
    # Используем имя из ALLOWED но удаляем файл — на самом деле все файлы
    # должны существовать. Проверим через подмену KNOWLEDGE_BASE_DIR.
    with patch("src.agents.boba_tools.KNOWLEDGE_BASE_DIR", "/nonexistent"):
        # KNOWLEDGE_BASE_DIR / "working_combos.md" не существует
        from pathlib import Path
        with patch("src.agents.boba_tools.KNOWLEDGE_BASE_DIR", Path("/tmp/nonexistent_kb")):
            result = await read_knowledge("working_combos")
            assert "не существует" in result


@pytest.mark.asyncio
async def test_append_knowledge_rejects_short_entries():
    """Записи короче 100 символов отвергаются."""
    result = await append_knowledge(
        "hypotheses",
        "## Слишком коротко"
    )
    assert "слишком короткая" in result.lower()


@pytest.mark.asyncio
async def test_append_knowledge_rejects_no_heading():
    """Записи без markdown заголовка отвергаются."""
    long_entry = "Это длинная запись без заголовка markdown. " * 5
    result = await append_knowledge("hypotheses", long_entry)
    assert "## " in result  # упоминает что нужен заголовок


@pytest.mark.asyncio
async def test_append_knowledge_rejects_disallowed_files(tmp_path):
    """Запись в README или orthodox_calendar не разрешена."""
    result = await append_knowledge(
        "orthodox_calendar",  # не в whitelist для записи
        "## Тест " + "x" * 200
    )
    assert "не разрешена" in result


@pytest.mark.asyncio
async def test_append_knowledge_writes_to_file(tmp_path):
    """append_knowledge действительно дописывает в файл."""
    # Создаём временный аналог hypotheses.md в tmp_path
    test_file = tmp_path / "hypotheses.md"
    test_file.write_text("# Гипотезы\n\nИнитиальное содержимое.\n", encoding="utf-8")

    with patch("src.agents.boba_tools.KNOWLEDGE_BASE_DIR", tmp_path):
        long_entry = (
            "## H1: Тестовая гипотеза\n\n"
            "**Дата:** 2026-05-15\n"
            "**Автор:** Тест\n"
            "**Статус:** ожидает теста\n\n"
            "**Гипотеза:** Это тестовая запись для проверки записи в файл. "
            "Длина больше 100 символов для прохождения валидации."
        )
        result = await append_knowledge("hypotheses", long_entry)

    assert "✅" in result
    assert "добавлена" in result.lower()

    # Проверяем что запись действительно в файле
    new_content = test_file.read_text(encoding="utf-8")
    assert "Инитиальное содержимое" in new_content  # старое сохранилось
    assert "H1: Тестовая гипотеза" in new_content  # новое добавилось


@pytest.mark.asyncio
async def test_run_audience_parsing_returns_command_hint():
    """run_audience_parsing подсказывает команду для запуска через бот."""
    result = await run_audience_parsing("5000")
    assert "/vk_audience" in result
    assert "5000" in result

    result_full = await run_audience_parsing("all")
    assert "/vk_audience full" in result_full


@pytest.mark.asyncio
async def test_run_audience_parsing_rejects_invalid_value():
    """Невалидное значение max_per_group → ошибка, не падение."""
    result = await run_audience_parsing("abc")
    assert "❌" in result


# ============================================================
# Tests for execute_tool dispatcher
# ============================================================


@pytest.mark.asyncio
async def test_execute_tool_dispatches_correctly():
    """execute_tool вызывает правильную функцию по имени."""
    result = await execute_tool("read_knowledge", {"file_name": "README"})
    assert "📄" in result or "База знаний" in result


@pytest.mark.asyncio
async def test_execute_tool_handles_unknown_name():
    """Неизвестный tool не падает, возвращает описание ошибки."""
    result = await execute_tool("nonexistent_tool", {})
    assert "❌" in result
    assert "nonexistent_tool" in result


@pytest.mark.asyncio
async def test_execute_tool_handles_bad_arguments():
    """Неверные аргументы tool не валят систему."""
    result = await execute_tool(
        "read_knowledge",
        {"wrong_arg": "value"},  # должно быть file_name
    )
    assert "❌" in result


# ============================================================
# Tests for BOBA_TOOLS_SCHEMA
# ============================================================


def test_boba_tools_schema_has_all_5_tools():
    """Все 5 заявленных инструментов есть в схеме."""
    tool_names = {t["name"] for t in BOBA_TOOLS_SCHEMA}
    expected = {
        "get_account_status",
        "run_audience_parsing",
        "get_campaign_stats",
        "read_knowledge",
        "append_knowledge",
    }
    assert tool_names == expected


def test_boba_tools_schema_valid_format():
    """Каждый tool имеет name, description, input_schema."""
    for tool in BOBA_TOOLS_SCHEMA:
        assert "name" in tool
        assert "description" in tool
        assert len(tool["description"]) > 50  # содержательное описание
        assert "input_schema" in tool
        assert tool["input_schema"]["type"] == "object"


# ============================================================
# Tests for tool use loop in DialogAgent
# ============================================================


def _make_text_block(text: str) -> MagicMock:
    """Mock одного text-блока в ответе Anthropic API."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_tool_use_block(
    tool_id: str, name: str, args: dict
) -> MagicMock:
    """Mock одного tool_use блока в ответе Anthropic API."""
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.name = name
    block.input = args
    return block


def _make_response(blocks: list[MagicMock]) -> MagicMock:
    """Mock ответа Anthropic API с заданными блоками."""
    response = MagicMock()
    response.content = blocks
    return response


@pytest.mark.asyncio
async def test_dialog_agent_requires_executor_with_tools():
    """Если передаём tools, обязателен tool_executor."""
    long_persona = "Ты эксперт. " * 50
    agent = DialogAgent(persona=long_persona)

    with pytest.raises(ValueError, match="tool_executor"):
        await agent.chat(
            "тест",
            tools=BOBA_TOOLS_SCHEMA,
            tool_executor=None,
        )


@pytest.mark.asyncio
async def test_dialog_agent_calls_tool_and_returns_final_text():
    """Боба запрашивает tool, получает результат, выдаёт финальный текст."""
    long_persona = "Ты эксперт. " * 50
    agent = DialogAgent(persona=long_persona)

    # Первый вызов API — Боба запрашивает tool
    first_response = _make_response(
        [
            _make_text_block("Сейчас проверю статус..."),
            _make_tool_use_block(
                "tool_use_1",
                "get_account_status",
                {},
            ),
        ]
    )
    # Второй вызов API — Боба получил результат tool и выдаёт финальный ответ
    second_response = _make_response(
        [_make_text_block("Баланс пустой. Нужно пополнить.")]
    )

    agent.client.messages.create = AsyncMock(
        side_effect=[first_response, second_response]
    )

    # Mock tool executor
    tool_executor = AsyncMock(return_value="📊 Баланс: 0₽, 0 активных кампаний")

    response = await agent.chat(
        "Боба, какой статус кабинета?",
        tools=BOBA_TOOLS_SCHEMA,
        tool_executor=tool_executor,
    )

    # Финальный текст — это второй ответ
    assert response.text == "Баланс пустой. Нужно пополнить."

    # API был вызван дважды (один tool call)
    assert agent.client.messages.create.call_count == 2

    # Tool executor был вызван с правильным именем и аргументами
    tool_executor.assert_called_once_with("get_account_status", {})

    # В истории есть assistant с tool_use, потом user с tool_result, потом финальный assistant
    history = response.updated_history
    assert history[0]["role"] == "user"  # исходный вопрос
    assert history[1]["role"] == "assistant"  # с tool_use
    assert any(b.get("type") == "tool_use" for b in history[1]["content"])
    assert history[2]["role"] == "user"  # tool_result
    assert any(b.get("type") == "tool_result" for b in history[2]["content"])
    assert history[3]["role"] == "assistant"  # финальный ответ


@pytest.mark.asyncio
async def test_dialog_agent_handles_tool_executor_crash():
    """Если tool executor падает с exception, диалог не падает."""
    long_persona = "Ты эксперт. " * 50
    agent = DialogAgent(persona=long_persona)

    first_response = _make_response(
        [
            _make_tool_use_block("t1", "read_knowledge", {"file_name": "x"}),
        ]
    )
    second_response = _make_response(
        [_make_text_block("Не получилось прочитать, продолжаю без этого.")]
    )

    agent.client.messages.create = AsyncMock(
        side_effect=[first_response, second_response]
    )

    # Tool executor падает
    async def crashing_executor(name, args):
        raise RuntimeError("симулированная ошибка")

    response = await agent.chat(
        "тест",
        tools=BOBA_TOOLS_SCHEMA,
        tool_executor=crashing_executor,
    )

    # Боба всё равно получил ответ (хоть и с ошибкой в tool_result)
    assert "продолжаю" in response.text.lower()


@pytest.mark.asyncio
async def test_dialog_agent_stops_at_max_iterations():
    """Защита от бесконечного цикла tool use."""
    long_persona = "Ты эксперт. " * 50
    agent = DialogAgent(persona=long_persona)

    # Боба бесконечно запрашивает tools
    infinite_tool_response = _make_response(
        [_make_tool_use_block("t1", "read_knowledge", {"file_name": "README"})]
    )
    agent.client.messages.create = AsyncMock(return_value=infinite_tool_response)

    tool_executor = AsyncMock(return_value="результат")

    response = await agent.chat(
        "тест",
        tools=BOBA_TOOLS_SCHEMA,
        tool_executor=tool_executor,
        max_tool_iterations=3,
    )

    # После 3 итераций срабатывает защита
    assert "⚠️" in response.text or "слишком много" in response.text.lower()
    assert agent.client.messages.create.call_count == 3
