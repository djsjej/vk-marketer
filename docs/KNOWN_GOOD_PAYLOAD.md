# Known-Good Payload — золотой эталон работающего создания рекламы

> ⚠️ **НЕ ТРОГАТЬ.** Этот payload подтверждённо работает в production
> (commit `f7664bc`, 11 мая 2026). Кампания `20865519` была создана
> через этот payload с 6 группами и 6 банерами, прошла модерацию VK
> и стала транслироваться.

## Контекст

- VK Ads API (новый кабинет, `https://ads.vk.com/api/v2/`)
- Аккаунт: `account_id = 30591625`
- OAuth client: `client_id = v74PNUWtK63egdHO`
- Package: `package_id = 3122` (Вступить в сообщество)
- Сообщество: `vk.com/pomolimsy`
- Баланс кабинета: 1000₽
- Дневной бюджет ad_plan: 420₽

## Архитектура workaround

Из-за того что `package_id 3122` в нашем аккаунте имеет ограниченный
набор разрешённых patterns ([486, 422, 525, 527, 400, 401, 530, 338,
529, 339, 150, 145, 537]), и пустых package settings для нового
создания через `POST /ad_plans.json` — мы работаем через
**template-кампанию**:

1. **Один раз вручную** (через UI кабинета) создана кампания `20865519`
2. **Бот при каждом создании** добавляет в неё новые ad_groups через
   `POST /ad_groups.json` с **nested banners** внутри
3. Старые 6 ручных групп от Vizit'a (137881410..137893763) **не
   используются** — они были нужны только для инициализации package
   через UI-flow

## Endpoint и формат

- **Endpoint:** `POST https://ads.vk.com/api/v2/ad_groups.json`
- **Headers:** `Authorization: Bearer <access_token>`, `Content-Type: application/json`
- **Body:** flat object (БЕЗ обёртки `{"ad_groups": [...]}`!)
  VK на обёртку отвечает `unknown_resource_field: "Unknown fields: ad_groups"`

## Payload одной ad_group с banner

```json
{
  "ad_plan_id": 20865519,
  "name": "41-43",
  "status": "active",
  "budget_limit_day": 420.0,
  "budget_limit": null,
  "max_price": 0,
  "package_id": 3122,
  "age_restrictions": "0+",
  "targetings": {
    "geo": {"regions": [188]},
    "sex": ["female"],
    "age": {"age_list": [0, 41, 42, 43]}
  },
  "banners": [
    {
      "name": "bot-test | 41-43 | Молимся о воинах",
      "urls": {"primary": {"id": <internal_url_id>}},
      "content": {
        "icon_256x256": {"id": <upload_id_256>},
        "image_600x600": {"id": <upload_id_600>}
      },
      "textblocks": {
        "title_40_vkads": {"text": "Заголовок до 40 символов"},
        "text_2000": {"text": "Текст объявления до 2000 символов"},
        "about_company_115": {"text": "О компании, до 115 символов"},
        "cta_community_vk": {"text": "signUp"}
      }
    }
  ]
}
```

## Критические правила

### Banner content
- **`icon_256x256` ОБЯЗАТЕЛЕН.** Без него VK не может подобрать pattern
  из 13 разрешённых для package 3122 → ошибка `"At least one pattern
  must be in package's settings"`. Это была корневая причина 25
  итераций фиксов 11 мая 2026.
- **`image_600x600` строго 600×600 квадрат.** Не больше, не меньше.
- Бот делает PIL smart-crop в квадрат и resize обеих версий из любой
  входной картинки.

### Textblocks
- Имена полей **строго по официальному гайду VK Ads** (раздел
  "Быстрый старт"):
  - `title_40_vkads` — заголовок до 40 символов
  - `text_2000` — основной текст до 2000 символов
  - `about_company_115` — о компании до 115 символов
  - `cta_community_vk` — call-to-action **со значением `"signUp"`**
    (фиксированное для package_id 3122)
- Для package_id 3127 (Написать в сообщество) `cta_community_vk` =
  `"contactUs"`.

### Age targeting
- `age_list` **всегда начинается с 0** (показывать тем, чей возраст
  не определён). Без 0 VK может отбраковать payload или сузить охват.
- Возрастные окна: `(41,43)`, `(44,46)`, `(47,49)`, `(50,52)`, `(53,55)`,
  `(56,58)` — для православной общины (DEFAULT_AGE_SPLITS_ORTHODOX).

### Targetings
- **НЕ передаём `pads`** — VK в auto-mode сам подбирает площадки.
  При явных pads VK переходит в manual-mode и требует patterns
  pre-настроенные в settings package.
- **НЕ передаём `group_members`** — лишнее.

### Budget
- `budget_limit_day` группы = `budget_limit_day` ad_plan
  (для нашей кампании 20865519 это 420.0).
- Бот динамически берёт это значение через `GET /ad_plans.json`.
- `budget_limit: null` обязательно явный null.

## Endpoints — что работает, что нет

| Endpoint | Метод | Работает? |
|---|---|---|
| `POST /ad_plans.json` | создание кампании | ❌ patterns ошибка (через UI работает, через API — нет) |
| `POST /ad_groups.json` | создание группы | ✅ flat payload, с nested banners |
| `POST /banners.json` | создание banner | ❌ 405, supported_methods: ["GET"] |
| `GET /ad_plans.json` | список кампаний | ✅ |
| `GET /ad_groups.json` | список групп | ✅ |
| `GET /banners.json` | список банеров | ✅ |
| `GET /api/v1/urls/?url=...` | регистрация URL | ✅ |
| `POST /content/static.json` | upload картинки | ✅ multipart |

## Связь с поддержкой VK

Адрес: `ads_api@vk.team`

Шаблон тикета (когда понадобится):
```
x-request-id: <id из заголовка ответа>
Время: <Date заголовок>
HTTP-статус: <код>
Endpoint: <method + path>
Описание: ...
```

x-request-id извлекается из response headers через метод
`VKAdsAPIError.diag_summary()`. Telegram-сообщение об ошибке его
показывает блоком "Для тикета в VK".
