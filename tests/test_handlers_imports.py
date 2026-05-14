"""Регрессионный тест: импорты telegram_bot не должны разваливаться.

В коммите 32d534a `feat(biba)` я случайно удалил `async def help_command`
из handlers.py, но импорт `help_command` в bot.py остался. Worker на
Railway крашился на старте с `ImportError: cannot import name
'help_command'`. Этот тест воспроизводит ровно тот же путь импорта что
выполняет `python -m src.main` на проде — и упал бы локально до push.

Если придёт регресс на другой символ — тест укажет точное имя.
"""

import importlib


def test_handlers_exports_all_names_imported_by_bot():
    """Все имена, которые `bot.py` импортирует из `handlers`, должны существовать.

    Тест намеренно не хардкодит список ожидаемых имён — он парсит сам
    `bot.py` и проверяет именно то, что там написано. Так его не нужно
    обновлять при добавлении новых команд.
    """
    import ast
    from pathlib import Path

    bot_src = Path(__file__).resolve().parent.parent / "src" / "telegram_bot" / "bot.py"
    tree = ast.parse(bot_src.read_text(encoding="utf-8"))

    expected_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "src.telegram_bot.handlers":
            for alias in node.names:
                expected_names.add(alias.name)

    assert expected_names, "bot.py не импортирует ничего из handlers — тест устарел?"

    handlers = importlib.import_module("src.telegram_bot.handlers")
    missing = [name for name in expected_names if not hasattr(handlers, name)]
    assert not missing, (
        f"bot.py импортирует из handlers имена, которых там нет: {missing}. "
        f"Worker на Railway упадёт с ImportError при старте."
    )


def test_build_bot_module_imports_cleanly():
    """`from src.telegram_bot.bot import build_bot` не должен бросать.

    Это самая ранняя точка отказа на Railway — `src/main.py` строка 10.
    Если этот тест падает — worker не стартует.
    """
    # Перезагружаем модуль чтобы поймать свежий код после правок
    if "src.telegram_bot.bot" in list(importlib.sys.modules):
        importlib.reload(importlib.sys.modules["src.telegram_bot.bot"])
    from src.telegram_bot.bot import build_bot  # noqa: F401


def test_help_command_exists_and_is_async():
    """help_command не должна снова исчезнуть."""
    import inspect
    from src.telegram_bot.handlers import help_command

    assert inspect.iscoroutinefunction(help_command), (
        "help_command должна быть async — это Telegram CommandHandler"
    )
