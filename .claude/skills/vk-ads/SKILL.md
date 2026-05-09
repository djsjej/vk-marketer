---
name: vk-ads
description: "Used when implementing or debugging VK Ads API (new ads.vk.com API) integration. Covers multipart image upload (the gotcha where most integrations break), 3-step ad creation flow, authentication, and known endpoint quirks. Trigger when working on src/vk_ads/, when adding new VK API calls, when debugging campaign creation failures, when ads create but objects don't, when image uploads return errors, or when working with banners/creatives in VK Реклама."
---

# VK Ads API — критические заметки

## Контекст

Используем **новый** VK Ads API (ads.vk.com / VK Реклама), не старый vk.com/ads.
API на стадии беты, документация неполная, поэтому здесь собираем накопленные знания.

## База

- **Base URL:** `https://ads.vk.com/api/v2`
- **Auth:** `Authorization: Bearer <VK_ADS_TOKEN>`
- **Content-Type:** `application/json` для большинства запросов, **multipart/form-data** для загрузки медиа

## Главная боль: создание объявления — это 3 шага

Новички (включая прошлые попытки автора через n8n) ломаются именно здесь.
Кампания создаётся одним запросом, но **объявление с картинкой — три**:

### Шаг 1: получить upload_url

```
POST /api/v2/banners/upload_url
{
  "type": "image"  // или другой тип медиа
}
```

Возвращает что-то вроде:
```json
{
  "upload_url": "https://upload.vk.com/...",
  "expires_at": "..."
}
```

### Шаг 2: загрузить файл multipart на upload_url

**ЭТО ТО МЕСТО, ГДЕ ЛОМАЕТСЯ N8N.** Стандартный HTTP Request node плохо работает с
multipart binary uploads. Поэтому в нашем проекте используем `httpx`, который
умеет multipart нативно.

```python
async with httpx.AsyncClient() as client:
    with open(image_path, 'rb') as f:
        files = {'file': (filename, f, mime_type)}
        response = await client.post(upload_url, files=files)
```

Ответ — JSON с `image_id` (или `photo_hash`, в зависимости от эндпоинта).
**Скопируй этот ID — он нужен в шаге 3.**

### Шаг 3: создать banner с этим image_id

```
POST /api/v2/banners
{
  "ad_group_id": <ID группы>,
  "title": "Заголовок",
  "description": "Описание",
  "image_id": "<тот самый ID из шага 2>",
  "url": "https://...",
  ...
}
```

Возвращает `banner_id`. На этом объявление создано.

## Если кампании создаются, а объявления нет

**Самая частая точка отказа:** шаг 2 (multipart upload) или шаг 3 (передаём
неправильный `image_id`). Симптомы:
- Кампания создалась успешно
- Группа объявлений (adgroup) создалась
- Сами объявления — пустые или с ошибкой

**Диагностика:**
1. Проверь, что в логах есть успешный ответ от шага 2 с непустым `image_id`
2. Проверь, что в шаге 3 этот же ID передаётся (а не пустая строка / None)
3. Проверь Content-Type запросов — должен быть `multipart/form-data` для шага 2,
   `application/json` для шагов 1 и 3
4. Проверь, что URL картинки актуален (не expired)

## Получение токена

1. В кабинете VK Рекламы (ads.vk.com) → Настройки → API
2. Сгенерировать новый токен
3. Скопировать в env как `VK_ADS_TOKEN`
4. Если работа от имени агентства — также `VK_ADS_CLIENT_ID`

## Эндпоинты, которые мы используем

| Что | Метод | Путь |
|-----|-------|------|
| Список ad_plans (кампаний) | GET | `/api/v2/ad_plans` |
| Создать кампанию | POST | `/api/v2/ad_plans` |
| Список adgroups | GET | `/api/v2/ad_groups` |
| Создать adgroup | POST | `/api/v2/ad_groups` |
| Получить upload_url | POST | `/api/v2/banners/upload_url` |
| Создать banner | POST | `/api/v2/banners` |
| Статистика | GET | `/api/v2/statistics/...` |
| Pause/resume | POST | `/api/v2/ad_groups/{id}` (PATCH-стиль) |

(Список не исчерпывающий — заполняем по мере имплементации.)

## Бюджеты

VK Ads API оперирует **копейками**, не рублями. То есть `daily_budget=20000` —
это 200 ₽, а не 20 000 ₽. Обязательно умножать/делить при чтении и записи!

В нашем коде договариваемся: в БД и в Pydantic-моделях храним в **рублях** (как
`daily_budget_rub`), а конверсию делаем только в момент вызова API.

## Targeting

Сегменты аудиторий, регионы, интересы — отдельная подсистема. Когда будем
реализовывать таргетинг, добавим сюда.

## Rate limits

Документация неточна. Ориентир — **не более 20 запросов в секунду** на токен.
Если упрёмся — добавим простой rate-limiter (asyncio.Semaphore + sleep).

## Что НЕ делать

- ❌ Не пытаться создать banner без upload_url + загрузки картинки. Картинка обязательна.
- ❌ Не передавать `image_id` как число — он всегда строка.
- ❌ Не использовать `aiohttp` для multipart — там есть тонкости с boundary.
  Только `httpx`.
- ❌ Не хранить токены в коде, только в env.

## Полезные ссылки

- Официальная справка VK Рекламы: https://ads.vk.com/help
- API reference (если есть): см. в кабинете → Настройки → API → Документация
