# vk-marketer

🤖 AI-маркетолог для VK Рекламы. Управляется через Telegram, работает 24/7 на Railway.

## Что делает

- Создаёт A/B тесты в VK Рекламе по картинке + теме от пользователя
- Мониторит метрики (CTR, CPM, CPL) и автоматически отключает слабые объявления
- Масштабирует выигравших, контролирует бюджет
- Утренние отчёты в Telegram, оперативные алерты при аномалиях

## Текущая ниша

Православный контент: Свято-Троицкий Зеленецкий монастырь, Святой Спиридон Тримифунтский.

## Стек

Python 3.11 · python-telegram-bot · httpx · APScheduler · SQLAlchemy · anthropic SDK · Railway

## Запуск

```bash
pip install -r requirements.txt
cp .env.example .env  # заполни ключами
python -m src.main
```

## Деплой

Push в `main` → Railway автоматически билдит и поднимает.

## Документация

- [CLAUDE.md](./CLAUDE.md) — инструкции для Claude Code (архитектура, конвенции, TODO)
- [.claude/skills/vk-ads/SKILL.md](./.claude/skills/vk-ads/SKILL.md) — особенности VK Ads API

## Лицензия

Personal project, all rights reserved.
