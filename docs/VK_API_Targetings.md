# VK Ads API — объект `Targetings`

**Источник:** https://ads.vk.com/doc/api/object/Targetings (получено 14.05.2026).
**Раздел в оглавлении:** Объекты → Рекламные объекты → `Targetings`.

> Таргетинги. Доступные таргетинги описаны в объекте пакета, в рамках
> которого создаётся кампания.
>
> Используется в объектах: `AdGroup`

То есть `targetings` — это поле объекта `AdGroup` (= `ad_group.targetings` в нашем
payload). НЕ поле `AdPlan` / кампании. Структуру конкретных вложенных объектов
смотри по ссылкам в разделе «Объекты → Рекламные объекты» (`AgeTargeting`,
`GeoTargeting`, `FulltimeTargeting`, `LocalGeoTargeting` и т.д.).

## Поля

| Поле | Тип | Описание |
|---|---|---|
| `age` | `AgeTargeting` | Возраст (список возрастов). `0` — показывать тем, чей возраст не определён. |
| `birthday` | `BirthdayTargeting` | День рождения. |
| `fulltime` | `FulltimeTargeting` | Время (дни и часы). |
| `geo` | `GeoTargeting` | Общий таргетинг географии: объединяет геолокации (`local_geo`) и регионы. **Возможна установка только одного из:** `local_geo` ИЛИ `regions`. |
| `group_members` | `string` | Вхождение в группу OK/VK. Choices: `all`, `group_member` (в группе), `not_group_member` (не в группе). |
| `interests` | `list of integer` | Интересы пользователей (плоский список ID из дерева `interests` в `targetings_tree.json`). `max_value=2147483647`. |
| `interests_soc_dem` | `list of integer` | Социально-демографические интересы (например, `27137` — «Духовный рост и самовыражение»). |
| `interests_stable` | `list of integer` | Долгосрочные интересы пользователей. |
| `local_geo` | `LocalGeoTargeting` | Таргетинг на конкретные геолокации (круги вокруг точек). |
| `mobile_apps` | `string` | Установленность приложений. Choices: `never_installed`, `now`, `deleted`. |
| `mobile_operation_systems` | `list of integer` | Мобильные ОС. |
| `mobile_operators` | `list of integer` | Операторы мобильной связи. |
| `mobile_prefix` | `list of string` | Префиксы. Доступные: `mts`, `beeline`, `megafon`. |
| `mobile_types` | `list of string` | Типы мобильных устройств. |
| `mobile_vendors` | `list of integer` | Производители мобильных устройств. |
| `mobile_operation_systems_sk_ad_network` | `MobileOperatingSystemsSkAdNetworkTargeting` | Таргетинг на iOS 14.5+ через SkAdNetwork. |
| `pad_category` | `PadCategoryTargeting` | Таргетинг на категорию приложения. |
| `pads` | `list of id` | Рекламные площадки. Доступные площадки определены в пакете кампании (см. `pads_tree`). |
| `regions` | `list of integer` | Регионы (список ID). |
| **`segments`** | **`list of integer`** | **Вхождение в аудиторные сегменты.** ⬅ это про подписчиков сообществ / CSV-списки от TargetHunter / ремаркетинг. |
| `sex` | `list of string` | Пол. Сочетания: `'male'`, `'female'`. |
| `device_types` | `list of string` | Таргетинг по девайсам: `'desktop'` (id 1), `'mobile'` (id 2). |

## Ключевые выводы для бота

### Что сейчас передаём в `targetings` (после Phase 4 коммита)

```json
{
  "geo": {"regions": [188]},
  "sex": ["female"],
  "age": {"age_list": [0, 41, 42, 43]},
  "interests_soc_dem": [27137, 10186, 12920],
  "segments": [<из env VK_AUDIENCE_SEGMENT_IDS>]  // опционально
}
```

**Логика `interests_soc_dem` — OR между ID:**
пользователь подходит если у него **хотя бы один** из сигналов.
Это намеренное расширение для oCPM-обучения VK — алгоритму нужно
больше «материала» чтобы найти лучших конверсионных.

| ID | Категория | Зачем |
|---|---|---|
| 27137 | Духовный рост и самовыражение | Главный прямой сигнал к нашей нише |
| 10186 | Женаты, замужем | Наша ЦА — замужние женщины 41-58 |
| 12920 | Есть дети в семье | Молятся за детей, типичный мотив |

Сужение происходит **между полями** (AND): женщина 41-58 в России И с
любым из этих сигналов. После первых данных по CTR/CPL можно уточнять.

### Чего НЕТ в API (важно для бизнес-понимания)

- **Прямого поля «таргет на подписчиков групп X, Y, Z»** — нет. Только через
  `segments`, где сегмент создан в UI кабинета («Аудитории → Создать сегмент
  → На основе подписок на сообщества») или через CSV-загрузку (TargetHunter).
- **Прямого поля «активные комментирующие»** — нет. Только через сегмент
  из TargetHunter (он парсит активных и выгружает CSV).
- **Интерес «Религия» / «Православие»** — в VK Ads интересы нет. Самое
  близкое — `interests_soc_dem: [27137]` «Духовный рост и самовыражение»
  (из `targetings_tree.json`).

### Что не используем, но могло бы быть полезным позже

- `fulltime` — расписание показа (по словам Vizit'а из OLD_CHATS, лучшее
  время для православной аудитории — суббота/воскресенье 19:00-22:00).
- `birthday` — поздравления с именинами (молитвы в день ангела).
- `device_types` — exclude desktop если хотим только мобайл (на iPhone
  легче нажать «Написать»).

### Эти поля точно НЕ передаём

- `pads` — auto-mode подбирает площадки сам по `package_id`. Если задать
  явно — VK переходит в manual-mode и требует `patterns` в settings пакета.
- `local_geo` — мы используем `regions: [188]` (Россия целиком).
