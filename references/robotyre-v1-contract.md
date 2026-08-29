# Машинный контракт Robotyre v1

Skill принимает только manifest с `contract: "robotyre-stock/v1"` и `schema_version: "1"`. Иная строковая версия возвращает `contract_unsupported`; ослаблять проверку нельзя.

## Manifest

Канонический URL:

```text
https://office.papa-shin.ru/robotyre-stock/v1/manifest.json
```

Идентификатор поколения находится в `content_generation_id` и должен совпадать с каждой строкой обоих JSONL. В `files` обязательны точно три ключа:

- `products.jsonl`;
- `offers.jsonl`;
- `archive.zip`.

Для каждого файла проверяются канонический путь, media type, `bytes`, lowercase `sha256`, `etag` и `last_modified`. Пути полезной нагрузки берутся из manifest и не задаются в env. Загрузка разрешена только по HTTPS с того же origin. `archive.zip` проверяется в manifest, но для поиска не загружается.

## JSONL и публичная проекция

Каждая строка `products.jsonl` и `offers.jsonl` должна иметь точную schema v1 без неизвестных или пропущенных полей. Связь товара и предложения всегда строится по `robotyre_product_id`.

В публичную выдачу проектируются только семь характеристик:

- `season`;
- `all_season`;
- `spikes`;
- `run_flat`;
- `disk_type`;
- `truck_axis` из машинного `truck_tire_axis`;
- `truck_construction` из машинного `truck_tire_construction`.

Статусы `unknown` и `missing` выносятся в `unknown_characteristics`; `source_value` не публикуется. Типоразмер не входит в контракт: `--size` возвращает `query_unsupported`.

Цена покупателя берётся только из `price_sale`. `price_input` не используется ни как цена, ни как fallback. Для `price_input_source` и непустого `price_sale_source` допустимы `json_integer`, `json_decimal_string` и `json_numeric_lexeme`.

Предложение без `supplier_name` или с `price_sale: null` в публичную выдачу не попадает. Нулевая или отрицательная `price_sale` нарушает контракт и отклоняет строку с `manifest_invalid`.

## Свежесть

Manifest объявляет `generated_at`, `checked_at` и фиксированный `stale_after_seconds`. Если срок истёк, в `warnings` добавляется `source_stale`. Массив warnings комбинирует этот код с причиной fallback; не своди его к одному значению.
