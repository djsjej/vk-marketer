# VK Ads API — AdPlans (создание кампании)

**Источник:** `https://ads.vk.com/doc/api/resource/AdPlans` (PDF от Vizit'а, 13 мая 2026)

## Endpoint

```
POST /api/v2/ad_plans.json
```

## Пример payload из официальной документации

```json
{
    "name": "Моя новая кампания",
    "status": "active",
    "date_start": "2022-04-01 00:00:00",
    "date_end": "2022-04-15 00:00:00",
    "autobidding_mode": "max_goals",
    "budget_limit_day": "1000",
    "budget_limit": "5000",
    "enable_utm": "False",
    "enable_offline_goals": "False",
    "objective": "playersengagement",
    "ad_groups": []
}
```

## ⚠️ Расхождения с нашим текущим кодом

| Поле | В документации | В нашем коде | Действие |
|---|---|---|---|
| `budget_limit_day` | string `"1000"` | int `600` | TODO: проверить нужен ли string. Workaround работает с int, может VK принимает оба |
| `budget_limit` | string `"5000"` | `None` | TODO: может быть обязательное |
| `date_start` | `"YYYY-MM-DD HH:MM:SS"` | `"YYYY-MM-DD"` | Проверить, но workaround работал без времени |
| `enable_utm` | string `"False"` | не передаём | Проверить — возможно обязательное |
| `enable_offline_goals` | string `"False"` | не передаём | Проверить |
| `objective` | `"playersengagement"` (пример) | `"socialengagement"` | НАШ корректен — это для подписок в сообщества |
| `ad_groups` | пустой массив `[]` | nested с группами | ✅ Документация подтверждает nested работает |

## Доступные статусы

```
active, blocked, deleted
```

Если статус не передан → устанавливается `active`.

## Пагинация и фильтры GET

```
GET /api/v2/ad_plans.json?limit=10&offset=15
GET /api/v2/ad_plans.json?_id=6617841
GET /api/v2/ad_plans.json?_id__in=6617841,6711647
GET /api/v2/ad_plans.json?_status=active
GET /api/v2/ad_plans.json?_status__in=active,blocked
GET /api/v2/ad_plans.json?sorting=id           # по возрастанию
GET /api/v2/ad_plans.json?sorting=-id          # по убыванию
GET /api/v2/ad_plans.json?sorting=status,name,-id   # по нескольким
```

## Возможные коды ошибок (полный каталог из документации)

| Код | Значение |
|---|---|
| `pricelist_not_found` | для pricelist_id не найдено ни одного прайслиста |
| `permission_required` | недостаточно прав для изменения поля |
| `required` | поле обязательно |
| `max_value` | значение больше максимального |
| `min_value` | значение меньше минимального |
| `bad_value` | неправильный формат или тип значения |
| `bad_items` | в списке присутствуют неправильные значения |
| `read_only_field` | поле только для чтения |
| `duplicate_value` | значения повторяются |
| `required_value` | ожидаются обязательные значения |
| `required_one_of_value` | ожидается одно из обязательных значений |
| `unallowed_value` | значение не входит в список доступных |
| `unallowed_field` | поле недоступно |

## Структура ошибки

```json
{
    "error": {
        "fields": {
            "<field_name>": {
                "message": "<error_message>",
                "code": "<error_code>"
            }
        },
        "message": "Validation failed",
        "code": "validation_failed"
    }
}
```

## Важные замечания из документации

> В ответе всегда присутствуют поля `id` и `ad_groups` (если кампания
> создается с группами).

→ **Подтверждено**: nested создание `ad_groups` в POST /ad_plans.json работает.

> Важно! `ad_groups` не поддерживается в `fields` и возвращается ошибка.

→ При GET запросе нельзя запрашивать ad_groups через параметр fields.
