# VK Ads API — AdGroups (создание рекламной группы)

**Источник:** `https://ads.vk.com/doc/api/resource/AdGroups` (PDF от Vizit'а, 13 мая 2026)

## Endpoint

```
POST /api/v2/ad_groups.json
```

## Полный пример payload из официальной документации

```json
{
    "name": "Моя новая группа",
    "status": "active",
    "date_start": "2022-04-01 00:00:00",
    "date_end": "2022-04-15 00:00:00",
    "autobidding_mode": "second_price",
    "budget_limit_day": "1000",
    "budget_limit": "5000",
    "mixing": "fastest",
    "price": "642.12",
    "age_restrictions": "18+",
    "banner_uniq_shows_limit": 2130,
    "uniq_shows_period": "week",
    "uniq_shows_limit": 100,
    "audit_viewability": "moat",
    "enable_utm": "False",
    "package_id": 449,
    "objective": "playersengagement",
    "banners": [...],
    "targetings": {...}
}
```

## 🚨 Критическое замечание

> Доступные поля, таргетинги и прочие настройки рекламной группы
> **описываются в объекте пакета**, в рамках которого создаётся группа.

→ **Минимум `budget_limit_day`, шаг, доступные стратегии — всё зависит от `package_id`.**

Чтобы узнать минимум для package 3122 — нужна страница `/resource/Package`.

## Структура banner в группе (формат документации)

```json
{
    "content": {
        "primary": {"id": 32433493}
    },
    "urls": {
        "primary": {"id": 98574325}
    },
    "textblocks": {
        "primary": {
            "title": "Этот товар нужен всем!",
            "text": "Для счастья нужно только..."
        }
    }
}
```

⚠️ Это **формат для общих package** (например 449). Для package **3122 (Вступить)** структура другая (подтверждено inspect 20865519):

```json
{
    "content": {
        "icon_256x256": {"id": ...},
        "image_600x600": {"id": ...}
    },
    "urls": {
        "primary": {"id": ...}
    },
    "textblocks": {
        "title_40_vkads": {"text": "..."},
        "text_2000": {"text": "..."},
        "about_company_115": {"text": "..."},
        "cta_community_vk": {"text": "Вступить"}
    }
}
```

→ Структура **зависит от package_id**.

## Все коды ошибок для AdGroups (полный каталог)

**Специфичные для AdGroups:**

| Код | Значение |
|---|---|
| `invalid_package` | Пакет недоступен данному пользователю |
| `can_not_set` | Нельзя переключиться на указанный пакет |
| `step` | Неверный шаг бюджета. Должен быть кратен `budget_limit_step` из `/api/v2/currencies.json` |
| `not_allowed_for_package` | Изменения недоступны в этом пакете |
| `pricelist_not_found` | Для `pricelist_id` не найдено прайслиста |
| `permission_required` | Недостаточно прав для изменения поля |
| `audit_pixel_invalid_roles` | Неправильные роли аудит-пикселя |
| `audit_pixel_max_count` | Превышен лимит на количество аудит-пикселей |
| `audit_pixel_must_be_unique` | Аудит-пиксели должны быть уникальны |
| `audit_pixel_invalid_urls` | У аудит-пикселя неправильный URL |
| `min_translation_hours` | Минимум 8 часов в fulltime таргетинге |

**Общие (как в AdPlans):**

| Код | Значение |
|---|---|
| `required` | Поле обязательно |
| `max_value` | Значение больше максимального |
| `min_value` | **Значение меньше минимального** ← наша текущая ошибка! |
| `bad_value` | Неправильный формат или тип |
| `bad_items` | В списке неправильные значения |
| `read_only_field` | Поле только для чтения |
| `duplicate_value` | Значения повторяются |
| `required_value` | Ожидаются обязательные значения |
| `required_one_of_value` | Ожидается одно из обязательных |
| `unallowed_value` | Значение не в списке допустимых |
| `unallowed_field` | Поле недоступно |

## Все доступные таргетинги (с примерами)

### age — возраст
```json
{"age": {"age_list": [0, 12, 13, 14, 22, 23, 24]}}
```
(0 = показывать тем чей возраст не указан)

### birthday — день рождения
```json
{"birthday": {"days_after": 5, "days_before": 10}}
```

### browser — браузеры
```json
{"browser": ["edge", "internet_explorer", "opera"]}
```

### fulltime — расписание показов
```json
{
    "fulltime": {
        "tue": [2],
        "wed": [2, 3],
        "thu": [2, 3, 4],
        "fri": [2, 3, 4, 5],
        "sat": [2, 3, 4, 5, 6],
        "sun": [2, 3, 4, 5, 6, 7],
        "flags": ["use_holidays_moving", "cross_timezone"]
    }
}
```

### geo — гео (regions ИЛИ local_geo, НЕ оба!)
```json
{"geo": {"regions": [56, 97, 100, 188, -70]}}
```
или
```json
{
    "geo": {
        "local_geo": {
            "visit_type": "usual",
            "loc_type": ["home", "work"],
            "locations": [{
                "lat": 55.75583,
                "lng": 37.6173,
                "radius": 3000,
                "label": "Центр Москвы",
                "address": "Точный адрес"
            }]
        }
    }
}
```
**Нельзя одновременно передавать `geo.regions` и `geo.local_geo`!**

### group_members — членство в группе
```json
{"group_members": "not_group_member"}
```

### interests — интересы
```json
{"interests": [9413, 9414, 9415]}
```

### interests_soc_dem — соц-дем интересы
```json
{"interests_soc_dem": [0, 12, 13, 14, 22, 23, 24]}
```

### mobile_*
```json
{"mobile_apps": "deleted"}
{"mobile_operation_systems": [37, 38, 39]}
{"mobile_operators": [3, 5, 6]}
{"mobile_prefix": ["mts", "beeline", "megafon"]}
{"mobile_types": ["smartphones", "tablets"]}
{"mobile_vendors": [14, 41, 49]}
```

### pad_category — категории площадок
```json
{
    "pad_category": {
        "iOS": [12, 13, 41],
        "Android": [7, 9, 83]
    }
}
```

### pads — конкретные площадки
```json
{"pads": [9863, 9872]}
```

### regions (без обёртки geo)
```json
{"regions": [56, 97, 100]}
```

### segments — пользовательские сегменты
```json
{"segments": [22679, 22728]}
```

### sex — пол
```json
{"sex": ["male", "female"]}
```

## Что лежит на нашем пути сейчас

Текущая ошибка: `min_value` на `budget_limit_day` группы при значении 100.

В примере документации: `"budget_limit_day": "1000"`.

→ **Гипотеза:** для package 3122 (или со стратегией `max_goals`) минимум выше 100₽. Подтверждать через страницу `/resource/Package`.

## Что в нашем коде не соответствует документации

⚠️ **НЕ ТРОГАЕМ пока не подтверждено документацией для package 3122.**

| Поле | Документация AdGroups | Наш код | Заметка |
|---|---|---|---|
| `budget_limit_day` | string `"1000"` | int 100 | Возможно VK принимает оба, но **минимум** выше |
| `budget_limit` | string `"5000"` | None | Возможно обязательное |
| `package_id` | **в payload группы** | передаём на уровне ad_plan, не группы | Может быть нужно дублировать |
| `autobidding_mode` | в группе (`second_price`) | в кампании (`max_goals`) | Возможно нужно в обоих местах |
| `objective` | в группе тоже | только в кампании | Проверить |
| `banners[].textblocks` | `primary` с `title`/`text` | `title_40_vkads`/`text_2000`/etc. | **Наш правильный для package 3122** (inspect 20865519) |
| `banners[].content` | `primary` с одним id | `icon_256x256` + `image_600x600` | **Наш правильный для package 3122** |

## Доступные статусы группы

```
active, blocked, deleted
```

Если не указан — устанавливается `active`.

## Пагинация и фильтры GET

```
GET /api/v2/ad_groups.json?limit=10&offset=15
GET /api/v2/ad_groups.json?_id=6617841
GET /api/v2/ad_groups.json?_status=active
GET /api/v2/ad_groups.json?_last_updated__gt=2022-01-01 00:00:00
GET /api/v2/ad_groups.json?sorting=status,name,-id
```
