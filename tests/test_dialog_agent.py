"""Тесты общего DialogAgent (Phase 6)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.dialog import AgentResponse, DialogAgent


def _make_mock_response(text: str) -> MagicMock:
    """Подделка ответа Anthropic API — один text block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    return response


def test_dialog_agent_validates_persona():
    """Слишком короткая персона → ValueError (защита от типичной ошибки)."""
    with pytest.raises(ValueError, match="слишком короткая"):
        DialogAgent(persona="короткая")

    with pytest.raises(ValueError, match="слишком короткая"):
        DialogAgent(persona="")


def test_dialog_agent_accepts_valid_persona():
    """Полноценная персона (500+ символов) — принимается без ошибок."""
    long_persona = "Ты опытный стратег. " * 50  # ~1000 символов
    agent = DialogAgent(persona=long_persona)
    assert agent.persona == long_persona


@pytest.mark.asyncio
async def test_chat_returns_agent_response():
    """chat() возвращает AgentResponse с текстом и обновлённой историей."""
    long_persona = "Ты опытный стратег. " * 50
    agent = DialogAgent(persona=long_persona)
    agent.client.messages.create = AsyncMock(
        return_value=_make_mock_response("Понял задачу, вот мой совет.")
    )

    response = await agent.chat("Какая стратегия лучше?")

    assert isinstance(response, AgentResponse)
    assert response.text == "Понял задачу, вот мой совет."
    # История: user + assistant (assistant content теперь список блоков)
    assert len(response.updated_history) == 2
    assert response.updated_history[0]["role"] == "user"
    assert response.updated_history[0]["content"] == "Какая стратегия лучше?"
    assert response.updated_history[1]["role"] == "assistant"
    # content — список блоков с type=text
    assistant_content = response.updated_history[1]["content"]
    assert isinstance(assistant_content, list)
    assert assistant_content[0]["type"] == "text"
    assert assistant_content[0]["text"] == "Понял задачу, вот мой совет."


@pytest.mark.asyncio
async def test_chat_uses_persona_as_system_prompt():
    """API вызывается с переданной персоной в поле system."""
    long_persona = "Ты Боба, жёсткий критик. " * 30
    agent = DialogAgent(persona=long_persona)
    agent.client.messages.create = AsyncMock(
        return_value=_make_mock_response("ОК")
    )

    await agent.chat("тест")

    call_kwargs = agent.client.messages.create.call_args.kwargs
    assert call_kwargs["system"] == long_persona


@pytest.mark.asyncio
async def test_chat_continues_history():
    """История из предыдущих turn'ов передаётся в API."""
    long_persona = "Ты эксперт. " * 50
    agent = DialogAgent(persona=long_persona)
    agent.client.messages.create = AsyncMock(
        return_value=_make_mock_response("Продолжаю")
    )

    prev_history = [
        {"role": "user", "content": "Привет"},
        {"role": "assistant", "content": "Здравствуй"},
    ]
    response = await agent.chat("Что дальше?", history=prev_history)

    # В API ушло 3 сообщения (2 истории + новое)
    call_kwargs = agent.client.messages.create.call_args.kwargs
    assert len(call_kwargs["messages"]) == 3
    assert call_kwargs["messages"][-1]["content"] == "Что дальше?"

    # Обновлённая история — 4 сообщения
    assert len(response.updated_history) == 4


@pytest.mark.asyncio
async def test_chat_truncates_long_history():
    """История длиннее MAX_HISTORY_MESSAGES обрезается с конца (старые забываются)."""
    long_persona = "Ты эксперт. " * 50
    agent = DialogAgent(persona=long_persona)
    agent.client.messages.create = AsyncMock(
        return_value=_make_mock_response("ОК")
    )

    # Делаем историю из 50 сообщений
    huge_history = []
    for i in range(25):
        huge_history.append({"role": "user", "content": f"u{i}"})
        huge_history.append({"role": "assistant", "content": f"a{i}"})

    await agent.chat("новое сообщение", history=huge_history)

    call_kwargs = agent.client.messages.create.call_args.kwargs
    sent_messages = call_kwargs["messages"]

    # Должно быть ровно MAX_HISTORY_MESSAGES
    assert len(sent_messages) == DialogAgent.MAX_HISTORY_MESSAGES
    # Последнее — наше новое сообщение
    assert sent_messages[-1]["content"] == "новое сообщение"


@pytest.mark.asyncio
async def test_chat_works_with_boba_persona():
    """Интеграция с реальной BOBA_PERSONA — без падения на валидации."""
    from src.agents import BOBA_PERSONA

    agent = DialogAgent(persona=BOBA_PERSONA)
    agent.client.messages.create = AsyncMock(
        return_value=_make_mock_response("Слушай, что у тебя?")
    )

    response = await agent.chat("Какую стратегию выберешь?")

    assert response.text == "Слушай, что у тебя?"
    call_kwargs = agent.client.messages.create.call_args.kwargs
    assert call_kwargs["system"] == BOBA_PERSONA
