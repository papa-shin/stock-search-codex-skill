---
name: papa-shin-stock-search
description: Use when a manager asks about Papa Shin stock availability, prices, suppliers, delivery terms, or product selection for tires, wheels, or related vehicle goods.
---

# Поиск по остаткам «Папы Шин»

Работай только на чтение через штатные скрипты пакета. Требуется Python 3.11 или новее.

## Порядок работы

1. Выбери команду Python, проверь её через `--version` и продолжай только при версии 3.11+. Во всех следующих вызовах используй тот же проверенный интерпретатор; системный `python3` может оказаться старее.
2. Если запрос содержит только автомобиль, сначала запроси точный типоразмер и сезон. Не определяй совместимость через внешние источники, не проси VIN и не придумывай подходящий размер.
3. Из корня пакета сначала запусти `scripts/fetch_stock.py` проверенным интерпретатором. Не переходи к поиску при JSON-ошибке; не придумывай конфигурацию. Статус `stale_cache` допускает поиск, но устаревание обязательно отрази в ответе.
4. Преобразуй подтверждённые условия только в поддерживаемые флаги `scripts/search_stock.py`: `--product-type`, `--size`, `--season`, `--spikes`, `--run-flat`, `--disk-type`, `--truck-axis`, `--truck-construction`, `--supplier`, `--min-total-quantity`, `--max-price`, `--max-delivery-days`, `--limit`, `--offers-limit`.
5. Передавай каждое значение отдельным аргументом процесса. Не используй `eval`, подстановку команд, ввод пользователя как путь или переменную окружения. Держи выдачу ограниченной: обычно `--limit 10 --offers-limit 5`, максимум 100 и 25 соответственно.
6. Выполни поиск и отвечай только по публичному JSON. Не достраивай отсутствующие свойства и не раскрывай конфигурацию или содержимое кэша.

## Ответ менеджеру

В начале укажи время генерации и проверки, признак `stale` и все `warnings`. Затем отдельно сообщи количество SKU (`summary.sku_count`) и суммарное количество единиц (`summary.total_quantity`), после чего перечисли ограниченный набор товаров и предложений. Значения из `unknown_characteristics` помечай как «неизвестно» или «нет данных»; не подменяй их догадкой. При `output_truncated` предложи сузить фильтры.

Для настройки читай [references/configuration.md](references/configuration.md), для полей ответа — [references/data-contract.md](references/data-contract.md), для примеров запросов и уточнений — [references/manager-queries.md](references/manager-queries.md).
