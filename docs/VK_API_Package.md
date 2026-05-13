# VK Ads API — Package (объект пакета рекламных услуг)

**Источник:** `https://ads.vk.com/doc/api/object/Package` (PDF от Vizit'а, 13 мая 2026)

## Назначение

Объект описывающий пакет рекламных услуг (3122 «Вступить», 3127 «Написать», 3194 «Вовлеченность», и т.д.). Используется в методах `Packages`.

## 🎯 Главное открытие — где лежат лимиты бюджета

Поле **`options`** содержит:

> Списки доступных таргетингов (targetings) и настроек (settings) рекламной кампании, созданной на основе пакета. Могут содержать поля **"defaults"** и **"values"**, хранящие таргетинговые значения по умолчанию и список всех возможных для использования значений соответственно.

→ **`options.settings.budget_limit_day.values.min_value`** — точный минимум для package.

Поле `options` — `readable` (не `default_field`), значит нужно явно запросить:

```
GET /api/v2/packages.json?_id=3122&fields=id,name,objective,options
```

## Все поля объекта Package

| Поле | Тип | Conditions | Описание |
|---|---|---|---|
| `id` | id | readable, default_field, 1..2147483647 | ID пакета (3122, 3127, ...) |
| `name` | string | readable, default_field | Название пакета |
| `description` | string | readable, default_field | Описание |
| `banner_url_get_params` | string | readable | GET-параметры к URL объявлений |
| `created` | datetime | readable, default_field | Время создания |
| `updated` | datetime | readable, default_field | Время последнего обновления |
| `max_price_per_unit` | price_per_unit | readable, default_field | Макс. стоимость кампании в рамках пакета |
| `max_uniq_shows_limit` | integer | readable, default_field, max=2147483647 | Макс. показов на пользователя |
| `objective` | list of string | readable, default_field | **Возможные цели рекламных кампаний** (см. ниже) |
| `options` | object | readable | **Targetings + settings (defaults и values)** |
| `package_request` | string | readable | Статус доступности (allowed/requested/can_be_requested) |
| `pads_tree_id` | id | readable, default_field | ID дерева площадок (0 если плоское) |
| `paid_event_type` | integer | readable, default_field | Тип события за которое оплата (см. ниже) |
| `price` | price_per_unit | readable, default_field | Стоимость кампании по умолчанию |
| `priced_event_type` | integer | readable, default_field | Тип события для оптимизации (см. ниже) |
| `related_package_ids` | list of id | readable, default_field | ID пакетов для переключения |
| `status` | string | readable, writable, default_field | active / deleted / blocked |
| `url_types` | object | readable, default_field | Разрешённые типы ссылок |

## Все возможные `objective` (цели кампании) — полный enum

| objective | Перевод |
|---|---|
| `reach` | Охват |
| `traffic` | Трафик |
| `appinstalls` | Установки приложений |
| `reengagement` | Ремаркетинг в приложение |
| `playersengagement` | Привлечение игроков игр соц. сетей |
| `videoviews` | Просмотр видео |
| `storeproductssales` | Покупки в интернет-магазине |
| `engagement` | Конверсии |
| `articleviews` | Просмотр статей |
| `audiolistening` | Аудиореклама |
| **`socialengagement`** | **Действия в социальных сетях** ← наш! |
| `storevisits` | Посещение точек продаж |
| `premium_reach` | Охват в премиальной сети |
| `general_ttm` | Медийные размещения «Продукты Mail.Ru Group» |

## `paid_event_type` — за что оплата

| Код | Что |
|---|---|
| 0 | 1000 показов |
| 1 | Клик |
| 7 | Конверсия |
| 1013 | Просмотр всего видео |
| 1017 | Просмотр 10 секунд видео |

## `priced_event_type` — по чему оптимизация (`max_goals`)

| Код | Что |
|---|---|
| 0 | 1000 показов |
| 1 | Клик |
| 7 | Конверсия |
| 30 | Установки |
| 41 | **События в Сообществах ВКонтакте** ← наш для socialengagement |
| 43 | In-app события в VK Mini Apps |
| 51 | Лид-формы |
| 1013 | Просмотр всего видео |
| 1017 | Просмотр 10 секунд видео |

## `package_request` — статус доступности пакета

- `allowed` — пакет доступен
- `requested` — пакет запрошен, но ещё недоступен
- `can_be_requested` — пакет может быть запрошен пользователем в службе поддержки VK Ads: `support_target@corp.my.com`

## Что критично для нашей задачи

**Чтобы узнать минимум `budget_limit_day` для package 3122:**

1. Запросить `GET /api/v2/packages.json?_id=3122&fields=id,name,options`
2. Прочитать `options.settings.budget_limit_day.values.min_value`

Это даст точный минимум, не нужно угадывать.

**Гипотеза:** у Vizit'а минимум для package 3122 **выше 100₽**, поэтому VK возвращает `min_value` на наш payload. Без явного запроса `options` — гадать.
