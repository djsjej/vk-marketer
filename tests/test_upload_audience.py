"""Тесты Phase 5.8 — загрузка готового списка ID из TargetHunter в VK Ads."""

from __future__ import annotations

import pytest

from src.telegram_bot.handlers import _parse_audience_file


# ============================================================
# Тесты парсера файла
# ============================================================


def test_parse_clean_ids():
    """Чистые числа на строках — все принимаются."""
    raw = "123456\n789012\n345678\n"
    ids, stats = _parse_audience_file(raw)
    assert ids == [123456, 789012, 345678]
    assert stats["valid"] == 3
    assert stats["invalid_lines"] == 0


def test_parse_with_id_prefix():
    """Префиксы id123456, user_X, vk.com/id обрабатываются."""
    raw = "id111\nuser_222\nvk.com/id333\nhttps://vk.com/id444\n555\n"
    ids, _ = _parse_audience_file(raw)
    assert ids == [111, 222, 333, 444, 555]


def test_parse_strips_whitespace():
    """Пробелы по краям игнорируются."""
    raw = "  100  \n\t200\t\n  300\n"
    ids, _ = _parse_audience_file(raw)
    assert ids == [100, 200, 300]


def test_parse_skips_empty_and_comments():
    """Пустые строки и комментарии (#) пропускаются."""
    raw = "100\n\n# это комментарий\n200\n   \n# ещё один\n300\n"
    ids, stats = _parse_audience_file(raw)
    assert ids == [100, 200, 300]
    assert stats["invalid_lines"] == 0  # комментарии не считаются невалидными


def test_parse_invalid_lines():
    """Невалидные строки считаются в invalid_lines."""
    raw = "100\nabc\n200\nxxx123\n300\n"
    ids, stats = _parse_audience_file(raw)
    assert ids == [100, 200, 300]
    assert stats["invalid_lines"] == 2  # "abc" и "xxx123"


def test_parse_deduplicates():
    """Дубли убираются."""
    raw = "100\n200\n100\n300\n200\n100\n"
    ids, _ = _parse_audience_file(raw)
    assert ids == [100, 200, 300]


def test_parse_rejects_negative_and_zero():
    """Отрицательные и нулевые ID отбрасываются."""
    raw = "100\n-5\n0\n200\n"
    ids, stats = _parse_audience_file(raw)
    assert ids == [100, 200]
    # 0 проходит isdigit() но не > 0, попадает в invalid
    # -5 не проходит isdigit() из-за минуса
    assert stats["invalid_lines"] == 2


def test_parse_empty_input():
    """Пустой файл → пустой список."""
    ids, stats = _parse_audience_file("")
    assert ids == []
    assert stats["valid"] == 0


def test_parse_handles_targethunter_format():
    """Стандартный формат экспорта TargetHunter — числа на строках."""
    raw = "\n".join(str(i) for i in range(1000, 1100))
    ids, stats = _parse_audience_file(raw)
    assert len(ids) == 100
    assert ids[0] == 1000
    assert ids[-1] == 1099


def test_parse_large_dataset():
    """50 000 ID парсится быстро."""
    raw = "\n".join(str(i) for i in range(1_000_000, 1_050_000))
    ids, stats = _parse_audience_file(raw)
    assert len(ids) == 50_000


# ============================================================
# Тесты команды /upload_audience
# ============================================================


@pytest.mark.asyncio
async def test_upload_audience_command_sets_waiting_flag():
    """Команда выставляет флаг ожидания файла в user_data."""
    from unittest.mock import AsyncMock, MagicMock

    from src.telegram_bot.handlers import upload_audience_command

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = []
    context.user_data = {}

    await upload_audience_command(update, context)

    assert context.user_data.get("awaiting_audience_upload") is True
    assert context.user_data.get("audience_segment_name") is None
    update.message.reply_text.assert_called_once()


@pytest.mark.asyncio
async def test_upload_audience_command_with_custom_name():
    """Команда с аргументом сохраняет название сегмента."""
    from unittest.mock import AsyncMock, MagicMock

    from src.telegram_bot.handlers import upload_audience_command

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["Православные", "СЗФО"]
    context.user_data = {}

    await upload_audience_command(update, context)

    assert context.user_data.get("audience_segment_name") == "Православные СЗФО"


@pytest.mark.asyncio
async def test_handle_document_ignores_without_flag():
    """Если флаг ожидания не выставлен — document handler ничего не делает."""
    from unittest.mock import AsyncMock, MagicMock

    from src.telegram_bot.handlers import handle_audience_document

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = {}  # без флага

    await handle_audience_document(update, context)

    # Не должно отправляться никаких сообщений
    update.message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_handle_document_rejects_non_txt():
    """Если файл не txt/csv — ошибка, флаг снимается."""
    from unittest.mock import AsyncMock, MagicMock

    from src.telegram_bot.handlers import handle_audience_document

    update = MagicMock()
    update.message.document.file_name = "audience.pdf"
    update.message.document.file_size = 1000
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = {"awaiting_audience_upload": True}

    await handle_audience_document(update, context)

    update.message.reply_text.assert_called_once()
    args, kwargs = update.message.reply_text.call_args
    assert "txt" in args[0].lower()
    assert context.user_data["awaiting_audience_upload"] is False
