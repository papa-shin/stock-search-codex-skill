# Публичный контракт данных

Оба CLI печатают один JSON-объект в stdout. Обычный успешный refresh содержит:

```json
{
  "status": "updated",
  "generation": {
    "id": "synthetic-generation",
    "generated_at": "2030-01-01T00:00:00Z",
    "checked_at": "2030-01-01T00:01:00Z",
    "stale": false
  },
  "warnings": []
}
```

`status` refresh может быть `updated`, `not_modified` или `stale_cache`. При `stale_cache` поле `generation.stale` равно `true`, а `warnings` объясняет причину использования предыдущего поколения.

Успешный поиск содержит:

| Поле | Смысл |
|---|---|
| `status` | `ok` |
| `generation` | `id`, `generated_at`, `checked_at`, `stale` |
| `filters` | Фактически применённые публичные фильтры |
| `summary.sku_count` | Число найденных товарных позиций до ограничения выдачи |
| `summary.total_quantity` | Сумма единиц найденных товарных позиций |
| `products` | Ограниченная выдача товаров |
| `unknown_characteristics` | Неизвестные или отсутствующие свойства возвращённых товаров |
| `warnings` | Предупреждения о свежести или ограничении вывода |

Элемент `products` содержит `product_id`, `name`, `article`, `product_type`, известные `characteristics`, `total_quantity`, `minimum_price` и `offers`. Предложение содержит `supplier`, строковое `price`, `delivery_days` и `quantity`. `minimum_price` может быть `null`, если подходящих предложений нет.

Элемент `unknown_characteristics` содержит `product_id`, `characteristic` и `status`: `unknown` означает «неизвестно», `missing` — «нет данных». Не восстанавливай эти значения по названию или внешним источникам.

`warnings` — массив объектов `code` и `message`. Код `output_truncated` означает, что `summary` остаётся полным, но `products` ограничен; менеджеру нужно предложить более узкие фильтры.

Ошибка имеет безопасную форму:

```json
{
  "status": "error",
  "error": {
    "code": "config_invalid",
    "message": "Не удалось прочитать конфигурацию"
  }
}
```

Не дополняй ошибку приватными путями, сетевыми адресами или содержимым исходных файлов.
