# ТЗ

## 1. Назначение

Создать публичный Codex Skill `papa-shin-stock-search` для менеджеров «Папы Шин». Skill должен безопасно получать текущее поколение закрытых машинных остатков, локально и потоково искать товары и предложения поставщиков, а затем возвращать Codex компактный структурированный результат для ответа на русском языке.

Репозиторий является только read-only клиентом. Он не изменяет production, не отправляет данные во внешние сервисы и не содержит коммерческих данных, credentials, реальных URL или внутренних названий источников.

## 2. Пользовательские сценарии

Skill должен поддерживать запросы следующих типов:

- «Найди летние шины 225/45R18»;
- «Покажи зимние 205/55R16 с остатком от 4 штук»;
- «Найди нешипованные шины не дороже 8 000 рублей»;
- «Покажи диски R17 заданного типа»;
- «Покажи предложения конкретного поставщика со сроком доставки до 5 дней»;
- «Какие характеристики отсутствуют или не распознаны?»;
- «Когда обновлялись остатки?».

Если менеджер указывает только автомобиль без размера, skill не подбирает совместимость по внешним источникам и просит уточнить типоразмер и сезон.

## 3. Публичные и приватные части

### 3.1. Публичный репозиторий

Публично хранятся:

- инструкции Codex Skill;
- Python-скрипты без сторонних зависимостей;
- нейтральное описание входного и выходного контрактов;
- synthetic fixtures;
- автоматические тесты;
- инструкция установки и настройки;
- Apache License 2.0.

### 3.2. Приватная конфигурация

По умолчанию используется файл:

```text
~/.codex/secrets/papa-shin-stock.env
```

Путь может быть переопределён переменной:

```text
PAPA_SHIN_STOCK_CONFIG
```

Приватная конфигурация содержит:

- URL manifest текущего закрытого endpoint;
- Basic Auth username/password;
- JSON-пути или имена полей текущего машинного контракта, которые требуется преобразовать в нейтральный выходной контракт;
- необязательный путь локального кэша.

Credentials и фактические значения mapping запрещено передавать через аргументы командной строки, хранить в Git или выводить в логах.

## 4. Технические ограничения

- Python 3.11 или новее.
- Только Python Standard Library.
- Поддержка macOS, Linux и нативного Windows.
- Корень репозитория является корнем устанавливаемого skill.
- Автоматическое обнаружение skill включено.
- Все сетевые запросы только по HTTPS с обычной проверкой TLS.
- Authorization разрешено передавать только исходному host; redirect на другой host запрещён.
- Реальные JSONL не загружаются целиком в память или контекст модели.
- Активным считается только одно проверенное поколение.
- При неудачном обновлении используется предыдущее подтверждённое поколение с явным предупреждением об устаревании.
- Обновление кэша выполняется атомарно.
- Все пользовательские ответы по умолчанию формируются на русском языке.

## 5. Структура репозитория

```text
stock-search-codex-skill/
├── .codex/tasks/
├── agents/openai.yaml
├── scripts/
│   ├── fetch_stock.py
│   ├── search_stock.py
│   └── papa_shin_stock/
│       ├── __init__.py
│       ├── cache.py
│       ├── config.py
│       ├── errors.py
│       ├── http_client.py
│       ├── query.py
│       └── schema.py
├── references/
│   ├── configuration.md
│   ├── data-contract.md
│   └── manager-queries.md
├── tests/
│   ├── fixtures/
│   │   ├── manifest.json
│   │   ├── products.jsonl
│   │   └── offers.jsonl
│   ├── test_cache.py
│   ├── test_config.py
│   ├── test_fetch.py
│   └── test_search.py
├── .env.example
├── .gitignore
├── LICENSE
├── README.md
└── SKILL.md
```

## 6. Загрузка и кэш

`fetch_stock.py` должен:

1. безопасно прочитать приватный env-файл без выполнения shell-кода и без подстановки команд;
2. отправить условный запрос manifest с `If-None-Match` и `If-Modified-Since`, если локальное состояние уже существует;
3. при HTTP 304 вернуть статус `not_modified`;
4. при новом поколении проверить обязательные поля manifest;
5. разрешить только относительные URL файлов или абсолютные HTTPS URL того же host;
6. потоково скачать `products.jsonl` и `offers.jsonl` во временный каталог;
7. проверить заявленные размер и SHA-256 каждого файла;
8. записать локальный `state.json` без credentials и приватных URL;
9. атомарно активировать новое поколение;
10. удалить неактивное поколение после успешной активации;
11. при любой ошибке сохранить предыдущий активный кэш.

Параллельные обновления защищаются lock-файлом с ограниченным сроком жизни. Активное поколение определяется содержимым атомарно заменяемого `current.json`, а не symlink, чтобы сохранить поддержку Windows.

## 7. Поиск

`search_stock.py` получает только несекретные параметры через `argparse`:

- `--product-type`;
- `--size`;
- `--season`;
- `--spikes`;
- `--run-flat`;
- `--disk-type`;
- `--truck-axis`;
- `--truck-construction`;
- `--supplier`;
- `--min-total-quantity`;
- `--max-price`;
- `--max-delivery-days`;
- `--limit`;
- `--offers-limit`.

Правила:

- варианты `205/55 16`, `205/55R16` и `205 55 R16` нормализуются в одно значение;
- product и offer ID читаются по mapping из приватной конфигурации, но наружу всегда возвращаются как `product_id`;
- сначала потоково отбираются товары, затем одним проходом выбираются предложения только для найденных ID;
- записи другого `content_generation_id` приводят к fail-closed ошибке поколения;
- значения характеристик со статусами `unknown` или `missing` сохраняются в отдельном массиве предупреждений;
- по умолчанию применяется `min_total_quantity=4`, `limit=10`, `offers_limit=5`;
- сортировка товаров: минимальная цена по возрастанию, затем суммарный остаток по убыванию;
- сортировка предложений: цена продажи, срок доставки, затем остаток;
- отсутствие результатов является успешным ответом с перечнем применённых фильтров.

## 8. Выходной контракт

Скрипты пишут в stdout только один JSON-документ UTF-8. Диагностика пишется в stderr без credentials, приватных URL и строк коммерческих данных.

Успешный результат поиска содержит:

```json
{
  "status": "ok",
  "generation": {
    "id": "synthetic-generation",
    "generated_at": "2026-08-27T13:03:53+05:00",
    "checked_at": "2026-08-27T14:03:40+05:00",
    "stale": false
  },
  "filters": {
    "product_type": "Шины",
    "size": "225/45R18",
    "season": "Лето"
  },
  "summary": {
    "sku_count": 1,
    "total_quantity": 12
  },
  "products": [
    {
      "product_id": "synthetic-product-1",
      "name": "Synthetic Tire 225/45R18 95W",
      "article": "SYN-001",
      "product_type": "Шины",
      "characteristics": {},
      "total_quantity": 12,
      "minimum_price": "7500",
      "offers": []
    }
  ],
  "unknown_characteristics": [],
  "warnings": []
}
```

`sku_count` означает число найденных товарных позиций. `total_quantity` означает суммарный физический остаток.

## 9. Обработка ошибок

Предусмотреть стабильные коды:

- `config_missing`;
- `config_invalid`;
- `auth_failed`;
- `network_error`;
- `manifest_invalid`;
- `download_integrity_failed`;
- `generation_mismatch`;
- `cache_unavailable`;
- `query_invalid`;
- `cache_locked`.

HTTP body, полный URL, Authorization, username/password и содержимое исходной строки JSONL не входят в сообщение ошибки.

## 10. Документация и установка

README должен описывать:

- назначение и read-only границы;
- установку клонированием в каталог Codex Skills;
- настройку приватного конфигурационного файла администратором;
- проверку установки на synthetic fixtures;
- примеры запросов менеджера;
- границы автомобильной совместимости;
- лицензионную оговорку для кода, данных и товарных знаков.

`SKILL.md` должен быть коротким: определять условия активации, обязательное обновление кэша, преобразование запроса в безопасные CLI-фильтры, правила ограничения результата и формат ответа менеджеру.

## 11. Acceptance Criteria

- [ ] Skill валиден штатным `quick_validate.py`.
- [ ] В репозитории отсутствуют реальные URL, credentials, коммерческие данные и внутренние названия источников.
- [ ] Все тесты проходят командой `python -m unittest discover -s tests -v`.
- [ ] Скрипты работают без сторонних Python-пакетов.
- [ ] Условное обновление корректно обрабатывает HTTP 304.
- [ ] Redirect на другой host блокируется до передачи Authorization.
- [ ] Повреждённый файл не становится активным поколением.
- [ ] Ошибка обновления не повреждает предыдущий кэш.
- [ ] Поиск читает JSONL потоково и связывает предложения только для найденных товаров.
- [ ] Несовпадение generation завершается fail-closed ошибкой.
- [ ] Нормализация размеров и фильтры подтверждены тестами.
- [ ] Ответ различает число SKU и физический остаток.
- [ ] Реальные данные не попадают в fixtures, test output, Git history или контекст Codex.
- [ ] Skill не выполняет mutation в production и внешних системах.

# Implementation Plan

## Papa Shin Stock Search Codex Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Создать устанавливаемый read-only Codex Skill для безопасного и быстрого поиска по закрытым машинным остаткам «Папы Шин».

**Architecture:** Python Standard Library клиент получает manifest и два JSONL-файла в атомарный локальный кэш. Поисковый CLI потоково фильтрует товары, затем предложения, возвращает нейтральный JSON, который Codex преобразует в ответ менеджеру.

**Tech Stack:** Python 3.11+, `urllib.request`, `hashlib`, `json`, `pathlib`, `argparse`, `unittest`, Codex Skills.

**Spec:** `.codex/tasks/270826_papa-shin-stock-search-codex-skill.md`

## Global Constraints

- Использовать только Python Standard Library.
- Поддерживать macOS, Linux и нативный Windows.
- Не хранить и не выводить credentials, реальные URL и коммерческие данные.
- Не использовать symlink как обязательную часть cache protocol.
- Не менять production и не отправлять данные во внешние сервисы.
- Не фиксировать внутренние названия источников в публичных файлах.
- Коммиты оформлять по Conventional Commits на русском языке.

---

### Task 1: Конфигурационный контракт и безопасный parser

**Files:**

- Create: `.env.example`
- Create: `.gitignore`
- Create: `scripts/papa_shin_stock/__init__.py`
- Create: `scripts/papa_shin_stock/config.py`
- Create: `scripts/papa_shin_stock/errors.py`
- Create: `tests/test_config.py`

**Interfaces:**

- Produces: `StockConfig.load(path: Path | None = None) -> StockConfig`.
- Produces: `StockConfig.resolve_product_id(row: dict[str, object]) -> str`.
- Produces: `StockError(code: str, safe_message: str, exit_code: int)`.
- Consumes: приватный env-файл с нейтральными ключами `PAPA_SHIN_STOCK_*`.

- [ ] **Step 1: Добавить failing tests parser**

```python
def test_load_does_not_expand_shell_syntax(self):
    config_path = self.write_env('PAPA_SHIN_STOCK_USERNAME="$(unsafe)"\n')
    config = StockConfig.load(config_path)
    self.assertEqual(config.username, "$(unsafe)")

def test_missing_required_mapping_is_safe_error(self):
    with self.assertRaisesRegex(StockError, "config_invalid"):
        StockConfig.load(self.write_env("PAPA_SHIN_STOCK_MANIFEST_URL=https://example.test/manifest.json\n"))
```

- [ ] **Step 2: Подтвердить RED**

Run: `python -m unittest tests.test_config -v`

Expected: `ImportError` для отсутствующего `papa_shin_stock.config`.

- [ ] **Step 3: Реализовать immutable config и безопасный env parser**

```python
@dataclass(frozen=True, slots=True)
class StockConfig:
    manifest_url: str
    username: str
    password: str
    product_id_field: str
    offer_product_id_field: str
    cache_dir: Path

    @classmethod
    def load(cls, path: Path | None = None) -> "StockConfig":
        config_path = path or Path(
            os.environ.get(
                "PAPA_SHIN_STOCK_CONFIG",
                Path.home() / ".codex" / "secrets" / "papa-shin-stock.env",
            )
        )
        values = parse_env_file(config_path)
        return cls(
            manifest_url=require_value(values, "PAPA_SHIN_STOCK_MANIFEST_URL"),
            username=require_value(values, "PAPA_SHIN_STOCK_USERNAME"),
            password=require_value(values, "PAPA_SHIN_STOCK_PASSWORD"),
            product_id_field=require_value(values, "PAPA_SHIN_STOCK_PRODUCT_ID_FIELD"),
            offer_product_id_field=require_value(values, "PAPA_SHIN_STOCK_OFFER_PRODUCT_ID_FIELD"),
            cache_dir=Path(values.get(
                "PAPA_SHIN_STOCK_CACHE_DIR",
                Path.home() / ".codex" / "cache" / "papa-shin-stock",
            )),
        )

    def resolve_product_id(self, row: dict[str, object]) -> str:
        value = row.get(self.product_id_field)
        if not isinstance(value, (str, int)) or str(value) == "":
            raise StockError("query_invalid", "У товара отсутствует идентификатор", 4)
        return str(value)
```

`parse_env_file()` принимает только строки `KEY=VALUE`, комментарии и одинарные/двойные кавычки. `require_value()` возвращает непустую строку либо `config_invalid`. Parser не поддерживает `source`, interpolation, command substitution или shell escape execution.

- [ ] **Step 4: Подтвердить GREEN и отсутствие секретов в выводе**

Run: `python -m unittest tests.test_config -v`

Expected: все tests `OK`; значения username/password отсутствуют в exception messages.

- [ ] **Step 5: Commit**

```bash
git add .env.example .gitignore scripts/papa_shin_stock tests/test_config.py
git commit -m "feat(config): добавлена безопасная настройка доступа к остаткам"
```

### Task 2: Защищённый HTTP client и проверка origin

**Files:**

- Create: `scripts/papa_shin_stock/http_client.py`
- Create: `tests/test_fetch.py`

**Interfaces:**

- Consumes: `StockConfig` из Task 1.
- Produces: `SafeHttpClient.get_manifest(state: CacheState | None) -> HttpResponse`.
- Produces: `SafeHttpClient.download(url: str, destination: Path, expected_bytes: int, expected_sha256: str) -> DownloadReceipt`.
- Produces: `assert_allowed_download_url(manifest_url: str, candidate_url: str) -> str`.

- [ ] **Step 1: Добавить failing tests TLS, redirect и integrity**

```python
def test_cross_origin_download_is_rejected(self):
    with self.assertRaisesRegex(StockError, "manifest_invalid"):
        assert_allowed_download_url(
            "https://stock.example.test/manifest.json",
            "https://other.example.test/products.jsonl",
        )

def test_http_url_is_rejected(self):
    with self.assertRaisesRegex(StockError, "config_invalid"):
        SafeHttpClient.for_config(self.http_config)
```

- [ ] **Step 2: Подтвердить RED**

Run: `python -m unittest tests.test_fetch.SafeHttpSecurityTest -v`

Expected: отсутствуют `SafeHttpClient` и `assert_allowed_download_url`.

- [ ] **Step 3: Реализовать origin-bound Basic Auth**

```python
class RejectCrossOriginRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_origin: tuple[str, str, int | None]):
        self.allowed_origin = allowed_origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if normalized_origin(newurl) != self.allowed_origin:
            raise StockError("network_error", "Перенаправление на другой сервер запрещено", 3)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

class SafeHttpClient:
    @classmethod
    def for_config(cls, config: StockConfig) -> "SafeHttpClient":
        origin = normalized_origin(config.manifest_url)
        if origin[0] != "https":
            raise StockError("config_invalid", "Для загрузки требуется HTTPS", 2)
        redirect_handler = RejectCrossOriginRedirect(origin)
        return cls(config=config, opener=urllib.request.build_opener(redirect_handler))

    def get_manifest(self, etag: str | None, last_modified: str | None) -> HttpResponse:
        headers = self._conditional_headers(etag, last_modified)
        return self._open_same_origin(self.config.manifest_url, headers)

    def download(self, url: str, destination: Path, expected_bytes: int,
                 expected_sha256: str) -> DownloadReceipt:
        resolved = assert_allowed_download_url(self.config.manifest_url, url)
        digest = hashlib.sha256()
        received = 0
        with self._open_same_origin(resolved, {}) as response, destination.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                received += len(chunk)
        if received != expected_bytes or digest.hexdigest() != expected_sha256:
            destination.unlink(missing_ok=True)
            raise StockError("download_integrity_failed", "Проверка загруженного файла не пройдена", 5)
        return DownloadReceipt(bytes=received, sha256=digest.hexdigest())
```

`normalized_origin()`, `_conditional_headers()` и `_open_same_origin()` получают отдельные tests. Authorization добавляется `_open_same_origin()` только запросам с той же парой scheme/host/port, что и manifest. HTTP body и URL не включаются в `StockError`.

- [ ] **Step 4: Подтвердить GREEN**

Run: `python -m unittest tests.test_fetch.SafeHttpSecurityTest -v`

Expected: все tests `OK`, включая simulated 304 и cross-origin redirect.

- [ ] **Step 5: Commit**

```bash
git add scripts/papa_shin_stock/http_client.py tests/test_fetch.py
git commit -m "feat(http): добавлена защищённая загрузка машинных данных"
```

### Task 3: Атомарный cache protocol и CLI обновления

**Files:**

- Create: `scripts/papa_shin_stock/cache.py`
- Create: `scripts/fetch_stock.py`
- Create: `tests/test_cache.py`
- Modify: `tests/test_fetch.py`

**Interfaces:**

- Consumes: `StockConfig`, `SafeHttpClient`, `StockError`.
- Produces: `CacheState.load(cache_dir: Path) -> CacheState | None`.
- Produces: `StockCache.refresh(config: StockConfig) -> RefreshResult`.
- Produces: `StockCache.current_generation() -> GenerationFiles`.
- Produces: CLI JSON statuses `updated`, `not_modified`, `stale_cache`, `error`.

- [ ] **Step 1: Добавить failing tests rollback и activation**

```python
def test_corrupt_download_does_not_replace_current_generation(self):
    cache = self.cache_with_generation("generation-a")
    client = FakeHttpClient(corrupt_sha256=True)
    with self.assertRaisesRegex(StockError, "download_integrity_failed"):
        StockCache(cache.root, client).refresh(self.config)
    self.assertEqual(cache.current_generation_id(), "generation-a")

def test_current_pointer_is_plain_json_not_symlink(self):
    self.assertFalse((self.cache_root / "current.json").is_symlink())
```

- [ ] **Step 2: Подтвердить RED**

Run: `python -m unittest tests.test_cache -v`

Expected: отсутствует `papa_shin_stock.cache`.

- [ ] **Step 3: Реализовать cache state machine**

```python
@dataclass(frozen=True, slots=True)
class GenerationFiles:
    generation_id: str
    manifest: Path
    products: Path
    offers: Path

class StockCache:
    def refresh(self, config: StockConfig) -> RefreshResult:
        with CacheLock.acquire(self.root):
            previous = CacheState.load(self.root)
            response = self.client.get_manifest(
                previous.manifest_etag if previous else None,
                previous.manifest_last_modified if previous else None,
            )
            if response.not_modified:
                return RefreshResult.not_modified(previous)
            manifest = Manifest.parse(response.body)
            staged = self._download_generation(manifest)
            self._verify_generation(staged, manifest)
            self._activate(staged, manifest, response)
            self._cleanup_inactive_generations(staged.generation_id)
            return RefreshResult.updated(manifest)

    def current_generation(self) -> GenerationFiles:
        pointer = CurrentPointer.load(self.root / "current.json")
        generation = self.root / "generations" / pointer.directory_name
        files = GenerationFiles.from_directory(pointer.generation_id, generation)
        files.assert_readable()
        return files
```

Состояния обновления: `idle -> locked -> downloading -> verified -> activated -> cleaned`. `current.json` записывается во временный файл в том же каталоге, `fsync`-ится и заменяется через `os.replace`.

- [ ] **Step 4: Реализовать CLI envelope**

```python
def main() -> int:
    try:
        result = StockCache.from_default_config().refresh()
        print(json.dumps(result.to_public_dict(), ensure_ascii=False))
        return 0
    except StockError as error:
        print(json.dumps(error.to_public_dict(), ensure_ascii=False))
        return error.exit_code
```

- [ ] **Step 5: Подтвердить GREEN на cache matrix**

Run: `python -m unittest tests.test_cache tests.test_fetch -v`

Expected: проходят success, 304, checksum failure, interrupted download, stale lock и fallback tests.

- [ ] **Step 6: Commit**

```bash
git add scripts/papa_shin_stock/cache.py scripts/fetch_stock.py tests/test_cache.py tests/test_fetch.py
git commit -m "feat(cache): добавлено атомарное обновление поколения остатков"
```

### Task 4: Потоковый поиск товаров и предложений

**Files:**

- Create: `scripts/papa_shin_stock/schema.py`
- Create: `scripts/papa_shin_stock/query.py`
- Create: `scripts/search_stock.py`
- Create: `tests/fixtures/manifest.json`
- Create: `tests/fixtures/products.jsonl`
- Create: `tests/fixtures/offers.jsonl`
- Create: `tests/test_search.py`

**Interfaces:**

- Consumes: `GenerationFiles`, `StockConfig` и нейтральные CLI filters.
- Produces: `SearchQuery.from_args(namespace: argparse.Namespace) -> SearchQuery`.
- Produces: `normalize_tire_size(value: str) -> str`.
- Produces: `StockSearcher.search(query: SearchQuery) -> SearchResult`.
- Produces: stdout JSON, не содержащий исходные строки JSONL.

- [ ] **Step 1: Добавить synthetic fixtures**

Fixtures содержат минимум: летнюю и зимнюю шину одного размера, товар с unknown характеристикой, товар с остатком меньше четырёх, несколько предложений и одну запись другого synthetic generation.

- [ ] **Step 2: Добавить failing tests нормализации и фильтров**

```python
def test_size_variants_are_equivalent(self):
    self.assertEqual(normalize_tire_size("205/55 16"), "205/55R16")
    self.assertEqual(normalize_tire_size("205 55 R16"), "205/55R16")

def test_search_distinguishes_sku_and_quantity(self):
    result = self.search(size="205/55R16", season="Лето")
    self.assertEqual(result.summary.sku_count, 2)
    self.assertEqual(result.summary.total_quantity, 24)
```

- [ ] **Step 3: Подтвердить RED**

Run: `python -m unittest tests.test_search -v`

Expected: отсутствуют `schema`, `query` и `search_stock.py`.

- [ ] **Step 4: Реализовать product streaming pass**

```python
class StockSearcher:
    def search(self, query: SearchQuery) -> SearchResult:
        candidates = self._read_matching_products(query)
        offers = self._read_matching_offers(set(candidates))
        return self._build_result(query, candidates, offers)
```

Каждая строка декодируется отдельно. В памяти хранятся только выбранные товары, их ID и ограниченные списки предложений.

- [ ] **Step 5: Реализовать generation guard и bounded offers**

```python
def assert_generation(row: dict[str, object], expected: str) -> None:
    if row.get("content_generation_id") != expected:
        raise StockError("generation_mismatch", "Поколение данных не согласовано", 5)
```

Для каждого товара сохраняется не более `offers_limit` лучших предложений; общий список товаров ограничивается после сортировки.

- [ ] **Step 6: Подтвердить GREEN и bounded output**

Run: `python -m unittest tests.test_search -v`

Expected: проходят normalization, filters, sorting, zero results, unknown/missing, generation mismatch и bounded output tests.

- [ ] **Step 7: Commit**

```bash
git add scripts/papa_shin_stock/schema.py scripts/papa_shin_stock/query.py scripts/search_stock.py tests/fixtures tests/test_search.py
git commit -m "feat(search): добавлен потоковый поиск по товарам и предложениям"
```

### Task 5: Codex Skill, UI metadata и документация менеджеров

**Files:**

- Create: `SKILL.md`
- Create: `agents/openai.yaml`
- Create: `references/configuration.md`
- Create: `references/data-contract.md`
- Create: `references/manager-queries.md`
- Create: `README.md`

**Interfaces:**

- Consumes: `scripts/fetch_stock.py` и `scripts/search_stock.py`.
- Produces: автоматическое обнаружение `$papa-shin-stock-search`.
- Produces: установка репозитория как одного skill package.

- [ ] **Step 1: Создать короткий SKILL.md**

```yaml
---
name: papa-shin-stock-search
description: Ищет товары в актуальных машинных остатках «Папы Шин» по типу, размеру, характеристикам, цене, наличию, поставщикам и срокам доставки. Использовать для подбора товаров без изменения production.
---
```

Workflow в `SKILL.md`: безопасно обновить кэш, уточнить размер при автомобильном запросе, преобразовать запрос в CLI flags, выполнить поиск, ограничить результат и отобразить freshness/unknown characteristics.

- [ ] **Step 2: Создать agents/openai.yaml**

Перед созданием прочитать `skill-creator/references/openai_yaml.md` и сгенерировать metadata штатным helper. Не отключать implicit invocation.

- [ ] **Step 3: Написать references без приватных значений**

`configuration.md` описывает только имена нейтральных переменных и передачу готового приватного файла администратором. `data-contract.md` документирует только публичный JSON результата. `manager-queries.md` содержит русские примеры запросов и ожидаемые уточнения.

- [ ] **Step 4: Написать README**

README содержит установку для macOS/Linux и Windows, `python --version`, запуск synthetic test suite, read-only границы, поддержку, лицензию и оговорку о данных/товарных знаках.

- [ ] **Step 5: Проверить инструкции вручную**

Run: `python scripts/fetch_stock.py --help`

Run: `python scripts/search_stock.py --help`

Expected: help не требует credentials и не выводит приватную конфигурацию.

- [ ] **Step 6: Commit**

```bash
git add SKILL.md agents references README.md
git commit -m "docs(skill): добавлены инструкции поиска по остаткам Папы Шин"
```

### Task 6: Полная верификация и подготовка Pull Request

**Files:**

- Modify: только файлы с подтверждёнными findings текущего diff.

**Interfaces:**

- Consumes: весь реализованный skill.
- Produces: проверенный diff без actionable findings.

- [ ] **Step 1: Выполнить полный test suite**

Run: `python -m unittest discover -s tests -v`

Expected: все tests `OK` на доступных платформах; platform-specific skips явно перечислены.

- [ ] **Step 2: Провести validation skill package**

Run: `python /Users/batyukov/.codex/skills/.system/skill-creator/scripts/quick_validate.py .`

Expected: `Skill is valid!`

- [ ] **Step 3: Проверить публичность содержимого**

Run: `git grep -nE 'https?://|PASSWORD|USERNAME|AUTHORIZATION' -- ':!LICENSE'`

Expected: только документированные synthetic/example значения; реальные endpoint и credentials отсутствуют. Отдельно локально проверить отсутствие закрытого списка внутренних терминов, не фиксируя сам список в Git.

- [ ] **Step 4: Проверить bounded performance**

На локальной synthetic или обезличенной большой выборке измерить отдельно refresh и search. Не коммитить исходный dataset или полный output. Зафиксировать в PR только время, размер входа и peak memory без коммерческих значений.

- [ ] **Step 5: Выполнить Code Review Loop**

Провести независимый review на correctness, security, cross-platform paths, cache atomicity и generation consistency. Исправить подтверждённые findings и повторять scoped re-review, пока actionable findings не останется.

- [ ] **Step 6: Проверить итоговый diff**

Run: `git status --short`

Run: `git diff --check origin/main...HEAD`

Run: `git diff --stat origin/main...HEAD`

Expected: только файлы skill, документация, tests и task-файл.

- [ ] **Step 7: Создать Pull Request**

Заголовок: `Добавлен Codex Skill для поиска по остаткам Папы Шин`

Описание содержит `## Что сделано`, `## Зачем`, `## Что проверить`, ограничения платформенной проверки и остаточные риски. После создания PR выполнить новый review итогового PR diff и исправить подтверждённые findings отдельными commits.

## Review Notes

- Task-файл не разрешает production mutation или публикацию реальных данных.
- Фактический приватный mapping выдаётся менеджерам отдельно и не является частью GitHub-репозитория.
- Нативная Windows-совместимость должна подтверждаться CI или явно оставаться непроверенным риском; успешные macOS/Linux tests не считаются Windows proof.
- Автомобильная совместимость находится вне scope первой версии.
- Публикация через OpenAI Skills API находится вне scope первой версии; GitHub-репозиторий является устанавливаемым source package.

### Task 6 — Fix-wave Cluster C, 28.08.2026

- Строгий контракт help для `search_stock.py` синхронизирован с fetch: только одиночные точные `-h` и `--help` возвращают обычную справку с exit 0; attached, abbreviated, mixed, duplicate и `--`-формы возвращают один безопасный `query_invalid` JSON без stderr.
- Машинные `total_quantity`, `delivery_days` и `quantity` принимают только неотрицательные `int` и канонические целочисленные строки; bool, float, Decimal, отрицательные и неканонические строки отклоняются без усечения.
- Добавлены публичные границы поиска: 2 MiB на JSONL-запись, 5 млн строк товаров, 50 млн строк предложений, 8 GiB/55 млн записей SQLite spool. Сортировка на выборке свыше 10 000 товаров осталась точной.
- Очистка SQLite spool выполняет три bounded fallback-попытки. Неудалённый каталог регистрируется для повторной очистки при завершении процесса; primary error сохраняется, успешная операция с cleanup failure возвращает безопасный `cache_unavailable`, приватный путь не раскрывается.
- TDD RED подтверждён отдельным прогоном до реализации: новые проверки integer, JSONL/row/spool budget, cleanup registry и строгого help завершались ожидаемыми failures/errors.
- GREEN подтверждён focused-прогоном `tests.test_search` и полным suite: Python 3.11 и 3.12 — по 202 tests, `OK`. Нативная Windows остаётся непроверенной платформой.

### Task 6 — Fix-wave Cluster C, fix round 1, 28.08.2026

- Добавлены covering index предложений `(id, price COLLATE decimal, delivery_days, quantity DESC)` и индекс кандидатов по наличию предложения, минимальной цене, остатку и ID.
- Production SQL проверяется через trace + `EXPLAIN QUERY PLAN`: повторные `SCAN o`, correlated full scan и `USE TEMP B-TREE` отсутствуют; trim использует covering index. Bounded scaling подтверждён числом SQLite VM steps на 20 и 2 000 нерелевантных строках без зависимости от wall time.
- Page budget 8 GiB применяется и проверяется отдельно для `main` и инициализированной `temp` schema. Рост temp-таблицы сверх уменьшенного тестового budget завершается fail-closed; потенциально неограниченный sorter temp-файл исключён устранением всех `USE TEMP B-TREE` в фактических планах поиска.
- Atexit cleanup больше не использует warnings: callback очищает bounded registry до 64 записей, перехватывает любые terminal failures и пишет через bounded `os.write` ровно одну фиксированную безопасную строку без пути, исходной ошибки и traceback. Subprocess с двумя failures и `PYTHONWARNINGS=error` завершается с кодом 0; закрытый stderr также не приводит к исключению.
- Документация уточняет отдельные main/temp budgets, суммарный SQLite disk envelope, риск накопления orphan-каталогов и безопасную operator guidance без destructive-команд.
- TDD RED подтверждён до реализации: планы содержали повторные `SCAN o` и два temp B-tree, VM steps росли линейно, `temp.max_page_count` оставался системным default, registry был неограничен, subprocess сохранял две записи без диагностики.
- GREEN: focused `tests.test_search` — 66 tests; полный suite Python 3.11 и 3.12 — по 208 tests, `OK`; `compileall` на обеих версиях — `OK`. Нативная Windows остаётся непроверенной платформой.
