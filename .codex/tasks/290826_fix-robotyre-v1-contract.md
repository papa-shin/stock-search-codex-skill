# ТЗ

## 1. Название

Поддержка канонического контракта `robotyre-stock/v1` в Papa Shin Stock Search

## 2. Контекст и проблема

Skill `papa-shin-stock-search` должен читать защищённую машинную публикацию Robotyre по адресу, который администратор задаёт в `PAPA_SHIN_STOCK_MANIFEST_URL`.

Канонические endpoint:

- base URL: `https://office.papa-shin.ru`;
- manifest: `/robotyre-stock/v1/manifest.json`;
- товары: `/robotyre-stock/v1/products.jsonl`;
- предложения: `/robotyre-stock/v1/offers.jsonl`;
- архив: `/robotyre-stock/v1/archive.zip`.

В env задаётся только полный URL manifest:

```text
PAPA_SHIN_STOCK_MANIFEST_URL=https://office.papa-shin.ru/robotyre-stock/v1/manifest.json
```

URL `products.jsonl`, `offers.jsonl` и `archive.zip` отдельно не настраиваются. Клиент получает их из manifest.

Текущая реализация была построена на синтетическом формате и несовместима с реальной публикацией:

- ожидает `generation_id` вместо `content_generation_id`;
- ожидает `files.products` и `files.offers` вместо `files["products.jsonl"]` и `files["offers.jsonl"]`;
- ожидает нормализованные поля товара и предложения, которых нет в JSONL канонического контракта;
- поэтому исправления только parser manifest недостаточно: `refresh` сможет скачать файлы, но `search` затем отклонит строки либо вернёт неверный результат.

## 3. Цель

Обеспечить полный read-only цикл `refresh -> atomic cache activation -> search` на синтетических fixtures, точно повторяющих структуру `robotyre-stock/v1`, без ослабления проверок безопасности и целостности.

## 4. Границы задачи

В scope входят:

- разбор и валидация publication manifest `robotyre-stock/v1`;
- загрузка `products.jsonl` и `offers.jsonl` по metadata из manifest;
- адаптация канонических строк JSONL к внутренней поисковой модели skill;
- точные и безопасные ошибки несовместимого контракта;
- обновление synthetic fixtures, unit/integration tests, README и reference-документации;
- независимый Code Review Loop итогового diff.

Вне scope:

- изменения publisher и endpoint в `office.papa-shin.ru`;
- скачивание или распаковка `archive.zip` для поиска;
- изменения production, БД, очередей, `/supplier-items`, parse pipeline, `SupplierItem -> Product`, `warehouse_items.pid` или naming rules `ProductType`;
- подбор совместимости автомобиля по VIN/марке/модели;
- сохранение credentials или реальных коммерческих строк Robotyre в репозитории;
- попытка восстановить отсутствующие в v1 структурированные данные эвристическим разбором `name`.

## 5. Требования к manifest

Клиент обязан:

1. Принять только:
   - `contract == "robotyre-stock/v1"`;
   - `schema_version == "1"` именно строкового типа;
   - `content_generation_id` как lowercase SHA-256 из 64 hex-символов.
2. Использовать `content_generation_id` как внутренний идентификатор поколения. Внутренние cache pointer, ownership receipt и runtime state могут сохранить имя `generation_id`, но в них записывается значение внешнего `content_generation_id`.
3. Читать metadata только из:
   - `files["products.jsonl"]`;
   - `files["offers.jsonl"]`.
4. Валидировать весь publication manifest, а не только используемое подмножество:
   - обязательные top-level поля `report_date`, `timezone`, `product_type_sku_counts`, `offer_count`, `warnings`, `generated_at`, `checked_at`, `stale_after_seconds`, `files`;
   - `timezone == "Asia/Yekaterinburg"` и `stale_after_seconds == 5400`;
   - точные типы, форматы, диапазоны и отсутствие неизвестных top-level полей, кроме предусмотренного source schema необязательного `linked_xlsx`;
   - точный набор `files`: `products.jsonl`, `offers.jsonl`, `archive.zip` без пропусков и лишних entries;
   - для каждого entry — обязательные `url`, `media_type`, `bytes`, `sha256`, `etag`, `last_modified` и отсутствие неизвестных полей;
   - ожидаемые media type и canonical URL path для каждого filename.
5. Для `products.jsonl` и `offers.jsonl` дополнительно проверить допустимые пределы `bytes`; metadata не подменяет проверку скачанного содержимого по `bytes` и `sha256`.
6. Проверить HTTPS и same-origin для URL всех трёх entries, сохранив существующую защиту от userinfo, downgrade, cross-origin и небезопасных URL.
7. Не строить URL файлов из фиксированных строк и не принимать отдельные env-переменные для них. Canonical path используется для валидации, а запрос выполняется по проверенному `url` из manifest.
8. Не скачивать `files["archive.zip"]` в штатном `refresh`; корректная metadata архива обязательна, но архив не является runtime dependency поиска.
9. Сохранить текущие ограничения размера manifest и payload, socket timeout, потоковую загрузку, hash/size verification, staging и атомарную активацию поколения.
10. Отличать неподдерживаемую версию от повреждённого документа:
   - другой корректно прочитанный `contract` или `schema_version` — новый стабильный код `contract_unsupported`, exit code `3`, безопасное сообщение без URL и тела ответа;
   - отсутствующие, неверного типа или некорректные обязательные поля заявленного v1 — `manifest_invalid`.
11. Не добавлять поддержку старого безверсионного синтетического manifest. Legacy-формат должен отклоняться fail-closed, а fixtures должны быть переведены на v1.

### 5.1. Freshness policy

- `generated_at` и `checked_at` валидируются как timezone-aware ISO 8601; `generated_at <= checked_at`.
- `checked_at` не может находиться более чем на 5 минут в будущем относительно локальных UTC-часов клиента; большее отклонение — `manifest_invalid`.
- Source считается свежим, только пока `now <= checked_at + stale_after_seconds`.
- Локальное время успешного HTTP-запроса хранится отдельно как время проверки кэша и не подменяет source `checked_at`.
- Свежий HTTP 200 или 304 не делает просроченный source свежим: после каждого 200/304 stale пересчитывается по сохранённому manifest `checked_at`.
- Просроченный, но криптографически и структурно валидный source разрешено активировать/использовать с `generation.stale=true` и стабильным warning `source_stale`; он не выдаётся как свежий.
- При fallback к предыдущему поколению итоговый `stale` равен логическому OR source staleness и transport/cache fallback staleness; warnings не должны терять обе независимые причины.

## 6. Требования к JSONL и поколению

Каждая строка `products.jsonl` и `offers.jsonl` обязана:

- быть JSON object без duplicate keys и недопустимых Unicode/числовых значений;
- содержать `schema_version == "1"`;
- содержать `content_generation_id`, равный manifest;
- содержать валидный `robotyre_product_id`, используемый для связи товара и предложения;
- содержать точный набор обязательных semantic fields соответствующего типа записи без неизвестных полей;
- проходить существующие лимиты строки, текста, количества строк и потоковой обработки.

Товарная строка строго валидируется по полям `entity_type`, `product_type_id`, `brand`, `model`, `name`, `brand_articul`, `articul_robotyre`, `characteristics`, `offer_count`, `total_quantity`, `min_price_input`, `min_price_input_source`, `min_price_sale`, `min_price_sale_source`, `suppliers_all_updated_at`, `suppliers_all_checked_at`, `snapshot_source` с типами, форматами, диапазонами и связанными ограничениями source schema v1.

Строка предложения строго валидируется по полям `supplier_article`, `supplier_article_source`, `supplier_name`, `warehouse_name`, `quantity`, `price_input`, `price_input_source`, `price_sale`, `price_sale_source`, `is_sale`, `delivery_days`, `delivery_date`, `organization_supplier_id`, `warehouse_external_id`, `supplier_id`, `price_last_updated_at`, `modified_at`, `snapshot_source` с типами, форматами, диапазонами и связанными ограничениями source schema v1.

`characteristics` содержит ровно семь ключей: `season`, `all_season`, `spikes`, `run_flat`, `disk_type`, `truck_tire_axis`, `truck_tire_construction`. Каждый объект соответствует одной и только одной комбинации:

- `known`: `normalized_value` входит в domain поля, `source_value` не `null`;
- `missing`: `normalized_value == null` и `source_value == null`;
- `unknown`: `normalized_value == null` и `source_value` не `null`.

Неизвестные characteristic keys, неправильный domain, лишние поля и противоречивые комбинации возвращают `manifest_invalid`.

Несовпадение поколения возвращает `generation_mismatch`. В JSONL нет собственного поля `contract`, поэтому любая строка с `schema_version != "1"` является повреждённой строкой уже выбранной публикации v1 и возвращает `manifest_invalid`, а не `contract_unsupported`. Содержимое строки и приватные исходные значения не попадают в stdout/stderr.

Параметры `PAPA_SHIN_STOCK_PRODUCT_ID_FIELD` и `PAPA_SHIN_STOCK_OFFER_PRODUCT_ID_FIELD` больше не обязательны. При отсутствии каждого параметра используется фиксированное значение `robotyre_product_id`. Для мягкого перехода явно заданное значение `robotyre_product_id` принимается, любое другое значение возвращает `config_invalid`. В README и основном примере конфигурации эти параметры не требуются.

## 7. Проекция товара

Каноническая строка товара преобразуется в текущую публичную модель следующим образом:

| Публичное/внутреннее поле | Источник v1 | Правило |
|---|---|---|
| `product_id` | `robotyre_product_id` | Строковый ID без преобразования смысла |
| `name` | `name`, затем `brand` + `model` | Если все три значения `null`, вернуть детерминированное `Товар Robotyre #<robotyre_product_id>`; display name не разбирать |
| `article` | `brand_articul` | При `null` возвращать пустую строку, сохраняя строковый публичный контракт; не подменять `supplier_article` и не использовать `articul_robotyre` как fallback |
| `product_type` | `product_type_id` | Явная таблица: `172 -> Шины`, `173 -> Диски`, `12371 -> Грузовые шины`, `12372 -> Грузовые диски`, `12373 -> Шины для квадроциклов`; неизвестный ID — `manifest_invalid` |
| `total_quantity` | `total_quantity` | Положительное целое (`>= 1`) в пределах текущих лимитов |
| сезон | `characteristics.season` и `characteristics.all_season` | Известные `Лето/Зима` сохраняются как season; `all_season` хранится отдельно как `Да/Нет`; запрос `--season Всесезонная` совпадает по `all_season=true`, не перезаписывая известный season |
| шипы | `characteristics.spikes` | `true -> Да`, `false -> Нет` |
| RunFlat | `characteristics.run_flat` | `true -> Да`, `false -> Нет` |
| тип диска | `characteristics.disk_type` | Из `normalized_value` |
| ось грузовой шины | `characteristics.truck_tire_axis` | Проецировать в текущий `truck_axis` |
| конструкция грузовой шины | `characteristics.truck_tire_construction` | Проецировать в текущий `truck_construction` |

Объект каждой характеристики валидируется по тройке:

- `normalized_value`;
- `normalization_status` в `known | missing | unknown`;
- `source_value`.

В поиск и публичный ответ попадает только `normalized_value` со статусом `known`. Публичный объект `characteristics` может содержать только ключи `season`, `all_season`, `spikes`, `run_flat`, `disk_type`, `truck_axis`, `truck_construction`; отсутствующие и неизвестные значения в него не включаются. Boolean-поля выводятся как `Да/Нет`, остальные — как нормализованные строки. Если одновременно известны `season` и `all_season=true`, публично присутствуют оба ключа без потери. Для `missing` и `unknown` формируется существующая запись `unknown_characteristics`; сырой `source_value` наружу не выводится. Точный набор и значения публичных keys фиксируются snapshot-тестом CLI JSON.

Канонический v1 не содержит структурированного типоразмера, индекса нагрузки и индекса скорости. Поэтому в рамках этой задачи запрещено извлекать их из `name` регулярными выражениями или объявлять достоверными. При использовании `--size` клиент должен вернуть новый явный безопасный код `query_unsupported` с объяснением, что текущая версия источника не публикует структурированный размер. README должен обозначить это ограничение. Возврат пустого результата вместо ошибки запрещён, потому что он создаёт ложное впечатление отсутствия товара.

## 8. Проекция предложения

Каноническая строка предложения преобразуется так:

| Публичное/внутреннее поле | Источник v1 | Правило |
|---|---|---|
| связь с товаром | `robotyre_product_id` | Точное совпадение с ID товара |
| `supplier` | `supplier_name` | Предложение без публичного имени не выводить |
| `price` | `price_sale` | Использовать только цену продажи; точная decimal-строка без float |
| `delivery_days` | `delivery_days` | `null` допустим в источнике; сортировать после известных сроков и не считать совпадением для `--max-delivery-days` |
| `quantity` | `quantity` | Положительное целое |

`price_input` является закупочной ценой и не должна автоматически публиковаться или использоваться как fallback для `price_sale`. Предложение с `price_sale == null` не включается в публичный список ценовых предложений и не участвует в `minimum_price` или `--max-price`. `minimum_price` вычисляется по доступным `price_sale`; если их нет, остаётся `null`.

Если `price_sale` не `null`, обязательны одновременно:

- canonical decimal-строка без exponent и leading plus;
- значение строго больше нуля и в пределах существующего безопасного decimal budget клиента;
- `price_input_source` и непустой `price_sale_source` в `json_integer | json_decimal_string | json_numeric_lexeme` согласно канонической producer schema.

Нулевая, отрицательная, слишком длинная/экстремальная цена, неправильный source или нарушение пары `price_sale/price_sale_source` делают строку заявленного v1 семантически непригодной и возвращают `manifest_invalid` fail-closed. При `price_sale == null` значение `price_sale_source` также обязано быть `null`.

`supplier_article` допустим только с `supplier_article_source == "product_supplier_articul"`, но в текущий публичный контракт не выводится. Значения `brand_articul` и `articul_robotyre` не записываются в supplier article и не смешиваются с ним.

## 9. Совместимость кэша

- Новое поколение хранит исходный канонический manifest без переписывания его в legacy-формат.
- Проверка уже активированного поколения читает `content_generation_id`, `contract` и `schema_version` из сохранённого manifest.
- Старое активированное поколение без v1 metadata не используется как успешный fallback после обновления клиента.
- При несовместимом legacy-кэше клиент завершает работу безопасной ошибкой и предлагает выполнить штатный `refresh`; он не удаляет и не мигрирует каталог автоматически.
- Все существующие ownership, symlink/reparse, race, rollback, durability и cleanup гарантии сохраняются.

## 10. Документация

Обновить:

- `README.md`: точный manifest URL, указание, что остальные URL берутся автоматически, сокращённый env-пример, поддерживаемый контракт, ограничение `--size`, troubleshooting для `contract_unsupported`;
- `references/configuration.md`: обязательные и необязательные переменные, v1 contract policy;
- `references/data-contract.md`: происхождение публичных полей, price-sale policy, unknown/missing semantics;
- при необходимости добавить отдельный `references/robotyre-v1-contract.md` с таблицей source-to-public mapping, не копируя закрытые данные.

Fixtures должны быть полностью синтетическими. Запрещено коммитить реальные логин/пароль, приватные URL с credentials, реальные товары, поставщиков, закупочные цены или сырой production manifest.

## 11. Тестирование

Обязательные тесты:

1. RED-тест, воспроизводящий текущий отказ на каноническом publication manifest.
2. Успешный parse manifest с `contract`, строковой `schema_version`, `content_generation_id` и filename keys.
3. Полная валидация обязательных top-level полей, exact file inventory и metadata всех трёх files, получение URL товаров и предложений из manifest и отсутствие запроса к archive.
4. Сохранение HTTPS/same-origin/userinfo/hash/bytes/size-limit защит.
5. `contract_unsupported` для другого contract/version и `manifest_invalid` для повреждённого v1.
6. Явное отклонение старого unversioned manifest.
7. Строки товара и предложения реальной структуры v1 с синтетическими значениями.
8. Совпадение `schema_version` и `content_generation_id` во всех строках; неверная версия строки классифицируется как `manifest_invalid`.
9. Проекция пяти допустимых `product_type_id`.
10. Полная exact-row validation: обязательные/лишние product и offer fields, source domains, timestamp/ID/decimal constraints и связанные nullable/source pairs.
11. Проекция `known`, `missing`, `unknown`, boolean и enum характеристик без утечки `source_value`; negative tests для всех противоречивых status triples и лишних characteristic keys.
12. Snapshot публичного JSON с точными known characteristic keys и отсутствием `source_value`.
13. Проекция `supplier_name`, точной положительной decimal `price_sale`, nullable delivery и quantity.
14. Парные случаи `price_sale/price_sale_source`, а также `null`, zero, negative, oversized и extreme значения; отсутствие fallback с `price_sale` на `price_input`.
15. Отсутствие fallback `supplier_article` с `brand_articul`/`articul_robotyre` и проверка provenance.
16. Явная ошибка `query_unsupported` для `--size`.
17. End-to-end: v1 manifest -> refresh -> atomic activation -> search -> публичный JSON.
18. Source freshness: fresh, expired, future `checked_at`, неверный порядок timestamps и HTTP 304 после истечения TTL.
19. Повторный refresh того же поколения, сочетание source-stale и fallback-stale warnings, повреждение payload и generation mismatch.
20. ID config: обе переменные отсутствуют, обе заданы как `robotyre_product_id`, одна или обе заданы иным значением.
21. Null fallback для name/article и одновременные `season` + `all_season=true`.
22. Полный существующий test suite на Python 3.11 и 3.12.
23. CI matrix macOS, Linux и Windows. Локальные mock-тесты не заменяют зелёный native Windows job для итогового commit.

## 12. Критерии готовности

Задача готова, когда:

- корректный `PAPA_SHIN_STOCK_MANIFEST_URL=https://office.papa-shin.ru/robotyre-stock/v1/manifest.json` не требует настройки URL payload;
- полный цикл работает на точной синтетической копии структуры publication manifest и JSONL v1;
- manifest, JSONL и cache generation используют один `content_generation_id`;
- весь publication manifest соответствует v1 schema, а source freshness не подменяется временем HTTP-проверки;
- результаты поиска используют цену продажи и не раскрывают закупочную цену или `source_value`;
- недоступный структурированный размер не маскируется пустой выдачей и не извлекается эвристически;
- существующие security/integrity/cache гарантии не ослаблены;
- README и references соответствуют фактическому поведению;
- полный test suite и CI matrix зелёные;
- независимый reviewer не оставил actionable findings после повторного review.

# Implementation Plan

## Этап 1. Зафиксировать v1 fixtures и RED-контракт

Затронуть:

- `tests/fixtures/manifest.json`;
- `tests/fixtures/products.jsonl`;
- `tests/fixtures/offers.jsonl`;
- `tests/test_cache.py`;
- `tests/test_fetch.py`;
- `tests/test_search.py`.

Действия:

1. Заменить legacy fixtures синтетическими документами точной структуры v1.
2. Добавить минимальный failing end-to-end тест реального shape manifest и строк JSONL.
3. Отдельно зафиксировать unsupported contract/version и запрет legacy manifest.
4. Запустить узкие тесты и сохранить RED evidence до изменения production-кода.

## Этап 2. Реализовать строгий parser publication manifest

Затронуть:

- `scripts/papa_shin_stock/cache.py`;
- `scripts/papa_shin_stock/errors.py` при необходимости централизованного кода ошибки;
- тесты manifest/fetch/cache.

Действия:

1. Проверять `contract`, `schema_version` и SHA-256 `content_generation_id`.
2. Читать `files["products.jsonl"]` и `files["offers.jsonl"]`.
3. Сохранить безопасное разрешение URL, same-origin, size/hash и streaming checks.
4. Не загружать archive.
5. Разделить `contract_unsupported` и malformed-v1 `manifest_invalid`.
6. Перевести проверку сохранённого manifest на v1 без изменения внутреннего cache pointer schema.
7. Расширить внутренние `Manifest`, `GenerationIntegrity`, `RuntimeStatus` и `GenerationSnapshot`: отдельно хранить source `generated_at`/`checked_at` и локальный `verified_at`; не записывать локальное время поверх source timestamp.
8. Заменить одиночный `warning_code` на детерминированный список `warning_codes`, способный одновременно сохранить `source_stale` и причину fallback; обновить serialization, validation и публичную сборку warnings.
9. На HTTP 200 и 304 пересчитывать source stale по текущему UTC-времени и сохранённому manifest, не по времени запроса. Проверить future skew и порядок timestamps до активации.
10. Обновить внутреннюю schema runtime state атомарно; legacy runtime state не должно позволять использовать legacy manifest как v1. Cache pointer schema менять только если без этого невозможно однозначно связать новую runtime metadata с generation.
11. Запустить узкие тесты до GREEN.

## Этап 3. Добавить потоковый адаптер строк v1

Затронуть:

- новый модуль наподобие `scripts/papa_shin_stock/robotyre_v1.py` либо компактные функции в `schema.py`;
- `scripts/papa_shin_stock/schema.py`;
- `scripts/papa_shin_stock/config.py`;
- тесты schema/search/config.

Действия:

1. Валидировать envelope каждой строки и поколение.
2. Валидировать полный exact набор product/offer fields, все source domains и связанные инварианты v1, включая строгие characteristic status triples.
3. Реализовать явные таблицы product type и characteristic mapping.
4. Преобразовывать по одной строке без materialization всего файла.
5. Сохранить unknown/missing warnings, сформировать точный публичный набор characteristics и исключить `source_value` из ответа.
6. Реализовать sale-price policy без fallback на `price_input`.
7. Обработать nullable supplier/delivery и точные decimal-строки.
8. Упростить ID configuration под фиксированный `robotyre_product_id` с безопасным переходом.
9. Добавить `query_unsupported` для `--size` до появления структурированного поля в source contract.
10. Запустить узкие тесты до GREEN.

## Этап 4. Обновить публичные контракты и документацию

Затронуть:

- `README.md`;
- `SKILL.md`, если в нём перечислены устаревшие возможности/ошибки;
- `references/configuration.md`;
- `references/data-contract.md`;
- при необходимости `references/robotyre-v1-contract.md`;
- тесты документационных инвариантов.

Действия:

1. Зафиксировать единственный настраиваемый URL и автоматическое получение payload URLs.
2. Описать поддерживаемую версию, mapping, price policy и ограничения.
3. Обновить команды проверки и troubleshooting.
4. Проверить, что документация не содержит секретов или production-данных.

## Этап 5. Полная верификация

1. Запустить formatter/linter, если они определены проектом.
2. Запустить полный `unittest` suite на доступных Python 3.11 и 3.12.
3. Проверить CLI stdout/stderr snapshots и отсутствие секретов/сырого source data.
4. Проверить diff на regressions в POSIX/Win32 cache paths, atomic activation и fallback.
5. Отправить ветку и дождаться зелёной CI matrix macOS/Linux/Windows.

## Этап 6. Code Review Loop

1. Передать итоговый diff независимому SubAgent-reviewer с фокусом на contract correctness, security, exact decimal, cache compatibility и Windows regressions.
2. Исправить все подтверждённые actionable findings через тест сначала.
3. Запустить повторную полную верификацию.
4. Передать новый diff другому независимому reviewer либо выполнить независимый re-review тем же reviewer без опоры на предыдущий вывод.
5. Повторять цикл до отсутствия actionable findings.

## Этап 7. Подготовка PR

1. Выполнить senior self-review итогового diff.
2. Создать Conventional Commit с русским описанием выполненного изменения.
3. Создать PR с русскими разделами `Что сделано`, `Почему`, `Что затронуто`, `Риски`, `Что проверить`, `Self-review`.
4. После создания PR повторить review по итоговому remote diff и проверить CI.
5. Merge выполнять только после зелёных обязательных проверок и отсутствия actionable findings.

# Review Notes

- Главный риск — исправить только ключи manifest и оставить несовместимую модель JSONL. Acceptance требует end-to-end теста, поэтому такое частичное исправление не принимается.
- `price_input` не является публичной ценой продажи и не используется как fallback.
- Канонический v1 не гарантирует структурированный размер. Эвристический разбор `name` сознательно исключён как источник ложных результатов.
- `source_value` может содержать необработанные коммерческие значения и не должен попадать в публичный результат или диагностику.
- Внутреннее имя `generation_id` допустимо сохранить только как implementation detail; внешний источник истины — `content_generation_id`.
- Изменения ограничены read-only клиентом skill и не затрагивают canonical production-flow `/supplier-items`.
- Post-merge review PR #2 выявил несовместимость с producer provenance `json_numeric_lexeme` и нерабочий legacy `.env.example`; follow-up обязан покрыть оба случая RED→GREEN и повторным review loop.
