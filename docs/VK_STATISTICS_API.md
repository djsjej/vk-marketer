# VK Ads Statistics API — Reference

> Источник: `https://ads.vk.com/doc/api/resource/Statistics`
> Сохранено: 12 мая 2026, после получения скрина от Vizit'а.

## Endpoints

| Endpoint | Назначение |
|---|---|
| `GET /api/v2/statistics/{banners\|ad_groups\|ad_plans\|users}/{day\|summary}.json` | Общая статистика v2 |
| `GET /api/v3/statistics/{banners\|ad_groups\|ad_plans\|users}/day.json` | **v3 с пагинацией — использовать это** |
| `GET /api/v2/statistics/goals/{...}/day.json` | Статистика по целям (только если есть Top@Mail или mobile install цели) |
| `GET /api/v3/statistics/faststat/{...}.json` | Real-time за последние 60 мин |
| `GET /api/v2/statistics/offline_conversions/{...}.json` | Оффлайн конверсии |

## Параметры v3 endpoint

| Параметр | Формат | Описание |
|---|---|---|
| `date_from` | YYYY-MM-DD | **обязательный** |
| `date_to` | YYYY-MM-DD | по умолчанию текущая дата |
| `id` | список через запятую | ID объектов (макс 200) |
| `fields` | через запятую | `all`, `base`, `events`, `uniques`, `uniques_video`, `video`, `carousel`, `tps`, `social_network`, `romi`, `playable`, `custom_event` |
| `attribution` | `conversion` (default) или `impression` | |
| `sort_by` | напр. `base.clicks` | |
| `d` | `asc` / `desc` | |
| `limit`, `offset` | пагинация | макс 250 |
| `ad_group_status`, `banner_status` | фильтр статусов | `active`, `blocked`, `deleted` |

## Главные метрики для нашего use case (православное сообщество, package_id 3122)

### `base` — основа
```
shows       — показы
clicks      — клики
ctr         — % кликов от показов (главный показатель качества креатива)
spent       — потрачено (string, рубли)
cpm         — средняя цена за 1000 показов
cpc         — средняя цена за клик
cpa         — средняя цена за достижение цели
cr          — % конверсий от кликов
vk.goals    — кол-во достижений целей VK
vk.cpa      — средняя цена за цель VK
vk.cr       — % VK-конверсий от кликов
```

### `events` — социальные действия (для socialengagement!)
```
joinings              — ВСТУПЛЕНИЯ В ГРУППУ (наша главная цель)
moving_into_group     — переходы на страницу группы
likes                 — лайки
shares                — репосты
comments              — комментарии
votings               — голосования
opening_post          — открытия поста
clicks_on_external_url — клики по ссылке в посте
launching_video       — запуски видео
```

### `social_network` — VK-специфичные
```
vk_join         — вступления в сообщества VK (та же joinings, для VK)
vk_subscribe    — подписки на пользователя VK
vk_message      — написания сообщений в сообщество
```

### `uniques` — охват
```
total           — уникальные пользователи (с начала кампании)
increment       — прирост за выбранный период
initial_total   — было до начала периода
frequency       — суточная частота показа
```

## Расчётные метрики (считаем сами)

| Метрика | Формула |
|---|---|
| **CPL** (Cost Per Lead = стоимость вступления) | `spent / joinings` |
| **CPR** (Cost Per Reach) | `spent / uniques.total` |
| **Reach rate** | `uniques.total / shows` |
| **Conversion rate (от показа в вступление)** | `joinings / shows` |
| **Click-to-join rate** | `joinings / clicks` |

## Лимиты

- Не более **366 дней** назад
- Не более **200 объектов** в одном запросе
- Пауза в трансляции > 90 дней — статистика по uniques невалидна

## Структура ответа v3

```json
{
  "items": [
    {
      "id": 137881410,
      "user_id": 55555,
      "total": {
        "base": {"shows": 100, "clicks": 5, "ctr": 5.0, ...},
        "events": {"joinings": 1, "likes": 3, ...},
        "uniques": {"total": 85, "frequency": 1.2, ...}
      }
    }
  ],
  "total": {  // суммарно по всем
    "base": {...}, "events": {...}
  },
  "limit": 20, "offset": 0, "count": 1
}
```

## Использование в нашем боте

**`/analyze` команда:**
```
1. GET /api/v3/statistics/ad_plans/day.json?id=20865519
   &date_from=<7 дней назад>&date_to=<сегодня>
   &fields=base,events,uniques,social_network
   → общая статистика кампании
2. GET /api/v3/statistics/ad_groups/day.json?id=<все group ids>
   &date_from=...&date_to=...
   &fields=base,events,uniques,social_network
   → разбивка по группам (возрастам)
3. Передаём данные Claude → анализ человеческим языком
4. Шлём в Telegram
```

**Утренний отчёт (Phase 4 scheduler):**
```
date_from=вчера, date_to=вчера
Структурированный отчёт + инсайты от Claude
```

**Auto-pause логика:**
```
для каждой ad_group:
  если shows > 1000 И ctr < 0.3% → status=blocked
  если ctr > 1.5% И joinings > 5 → budget_limit_day *= 1.5
```

**Faststat для real-time проверок:**
```
GET /api/v3/statistics/faststat/ad_plans.json?id=20865519
→ массивы поминутно за последние 60 минут
```
