# Papa Shin Stock Search

Codex Skill для read-only поиска товаров и предложений в подготовленных машинных остатках. Корень этого репозитория является корнем skill package: после установки `SKILL.md` должен лежать непосредственно в каталоге `papa-shin-stock-search`, без дополнительного вложенного каталога.

## Возможности

Skill обновляет проверенный локальный snapshot остатков и помогает менеджеру искать товары по подтверждённым условиям:

- тип товара и типоразмер;
- сезон, шипы и RunFlat;
- параметры дисков, ось и конструкция грузовых шин;
- поставщик, минимальное количество, максимальная цена и срок доставки;
- ограничение числа товаров и предложений в ответе.

Ответ отдельно показывает количество SKU и физический остаток, свежесть поколения, предложения поставщиков и неизвестные характеристики. Повреждённые поколения не активируются, а семантически несогласованные поколения не используются для успешного поиска; при допустимом fallback устаревание отмечается как `stale_cache`.

Skill работает только с подготовленным read-only snapshot. Он не подбирает совместимость по VIN или модели автомобиля, не обращается к внешним источникам, не изменяет production, товары, цены или остатки и не публикует приватную конфигурацию.

## Требования

- Python 3.11 или новее;
- подготовленный администратором приватный конфигурационный файл;
- Codex с поддержкой skills.

## Установка в macOS и Linux

Выберите установленный Python 3.11+ и сохраните launcher в отдельной переменной. Команда ниже выбирает первую доступную поддерживаемую версию; во всех последующих командах используйте только найденный launcher:

```bash
PAPA_SHIN_PYTHON=''
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && \
    "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    PAPA_SHIN_PYTHON="$candidate"
    break
  fi
done
test -n "$PAPA_SHIN_PYTHON" || { echo 'Требуется Python 3.11+' >&2; exit 1; }
"$PAPA_SHIN_PYTHON" --version
```

Клонируйте публичный репозиторий непосредственно в каталог skills. Команда безопасно завершится ошибкой, если целевой каталог уже существует:

```bash
PAPA_SHIN_CODEX_ROOT="${CODEX_HOME:-$HOME/.codex}"
PAPA_SHIN_SKILLS_DIR="$PAPA_SHIN_CODEX_ROOT/skills"
PAPA_SHIN_SKILL_DIR="$PAPA_SHIN_SKILLS_DIR/papa-shin-stock-search"
mkdir -p -- "$PAPA_SHIN_SKILLS_DIR"
git clone -- 'https://github.com/papa-shin/stock-search-codex-skill.git' "$PAPA_SHIN_SKILL_DIR"
cd -- "$PAPA_SHIN_SKILL_DIR"
```

Разместите полученный от администратора готовый приватный файл отдельно от пакета согласно разделу [«Приватная конфигурация»](#приватная-конфигурация). Не переносите его в репозиторий.

Если задаётся собственный каталог кэша, используйте новый отсутствующий или пустой leaf-каталог. Непустой legacy-кэш без служебного marker автоматически не принимается: выберите новый путь и повторно загрузите воспроизводимые данные по штатной команде.

## Установка в Windows

> Нативный refresh реализован через изолированный Win32 handle backend. Он проверяется теми же end-to-end тестами в Windows Server 2022 с Python 3.11/3.12 и отдельными mock race/fault-тестами на всех ОС. До зелёного Windows CI для конкретного commit нативный результат остаётся неподтверждённым для этого commit; macOS/Linux tests не заменяют такую проверку.

В PowerShell сначала посмотрите установленные версии через Python Launcher, затем выберите имеющуюся версию 3.11 или новее. В примере установлена 3.12; замените `-3.12` на значение из вывода `py -0p`. Если Python 3.11+ доступен командой `python`, задайте `$PapaShinPython = "python"` и `$PapaShinPythonArgs = @()`:

```powershell
py -0p
$PapaShinPython = "py"
$PapaShinPythonArgs = @("-3.12")
& $PapaShinPython @PapaShinPythonArgs --version
& $PapaShinPython @PapaShinPythonArgs -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Требуется Python 3.11+")'
```

Клонируйте репозиторий непосредственно в каталог skills:

```powershell
$PapaShinCodexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
$PapaShinSkillsDir = Join-Path $PapaShinCodexRoot "skills"
$PapaShinSkillDir = Join-Path $PapaShinSkillsDir "papa-shin-stock-search"
New-Item -ItemType Directory -Force -Path $PapaShinSkillsDir | Out-Null
git clone -- "https://github.com/papa-shin/stock-search-codex-skill.git" $PapaShinSkillDir
Set-Location -LiteralPath $PapaShinSkillDir
```

Приватный конфигурационный файл храните в профиле пользователя вне пакета согласно разделу [«Приватная конфигурация»](#приватная-конфигурация).

После копирования перезапустите Codex или начните новую задачу, чтобы пакет обнаружился. Автоматическое подключение разрешено; явный вызов доступен как `$papa-shin-stock-search`.

## Приватная конфигурация

Конфигурацию готовит администратор. Не вводите её значения вручную, если вам не передан утверждённый файл, и никогда не вставляйте его содержимое в чат, issue, аргументы команд или логи.

Путь по умолчанию:

- macOS/Linux: `~/.codex/secrets/papa-shin-stock.env`;
- Windows: `%USERPROFILE%\.codex\secrets\papa-shin-stock.env`.

В macOS/Linux подготовьте закрытый каталог, после чего попросите администратора поместить туда готовый файл:

```bash
PAPA_SHIN_SECRETS_DIR="$HOME/.codex/secrets"
mkdir -p -- "$PAPA_SHIN_SECRETS_DIR"
chmod 700 -- "$PAPA_SHIN_SECRETS_DIR"
test ! -f "$PAPA_SHIN_SECRETS_DIR/papa-shin-stock.env" || \
  chmod 600 -- "$PAPA_SHIN_SECRETS_DIR/papa-shin-stock.env"
```

Последняя команда безопасно пропускается до появления файла. После установки файла повторите её, чтобы ограничить доступ. В Windows PowerShell подготовьте каталог с ACL только для текущего пользователя:

```powershell
$PapaShinSecretsDir = Join-Path $env:USERPROFILE ".codex\secrets"
New-Item -ItemType Directory -Force -Path $PapaShinSecretsDir | Out-Null
icacls $PapaShinSecretsDir /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F"
$PapaShinConfig = Join-Path $PapaShinSecretsDir "papa-shin-stock.env"
```

После установки файла администратором повторно примените ACL к `$PapaShinConfig`:

```powershell
icacls $PapaShinConfig /inheritance:r /grant:r "${env:USERNAME}:F"
```

Default config path не зависит от `CODEX_HOME`. Если администратор выбрал другой абсолютный путь, передавайте только путь через `PAPA_SHIN_STOCK_CONFIG` в окружении процесса Codex, не содержимое файла:

```bash
export PAPA_SHIN_STOCK_CONFIG='/absolute/path/to/papa-shin-stock.env'
```

```powershell
$env:PAPA_SHIN_STOCK_CONFIG = "C:\private\papa-shin-stock.env"
```

Полный контракт параметров и требования к отдельному cache leaf описаны в [references/configuration.md](references/configuration.md).

## Проверка

Сначала убедитесь, что пакет установлен без лишнего уровня вложенности. В macOS/Linux используйте вычисленный при установке каталог:

```bash
test -f "$PAPA_SHIN_SKILL_DIR/SKILL.md"
```

В Windows PowerShell:

```powershell
Test-Path -LiteralPath (Join-Path $PapaShinSkillDir "SKILL.md")
```

В macOS/Linux из корня пакета используйте ранее проверенный launcher:

```bash
"$PAPA_SHIN_PYTHON" -m unittest discover -s tests -v
"$PAPA_SHIN_PYTHON" scripts/fetch_stock.py --help
"$PAPA_SHIN_PYTHON" scripts/search_stock.py --help
```

В PowerShell повторно используйте выбранный launcher и его аргументы:

```powershell
& $PapaShinPython @PapaShinPythonArgs -m unittest discover -s tests -v
& $PapaShinPython @PapaShinPythonArgs scripts/fetch_stock.py --help
& $PapaShinPython @PapaShinPythonArgs scripts/search_stock.py --help
```

Обе команды справки должны завершиться без приватной конфигурации и не выводить её значения.

После перезапуска Codex или открытия новой задачи выполните пробный явный запрос:

```text
$papa-shin-stock-search Найди летние шины 205/55R16, минимум четыре штуки
```

Codex должен активировать Skill, сначала обновить локальный кэш, затем выполнить поиск. Если приватный файл ещё не установлен, ожидается безопасная ошибка `config_missing` без вывода значений конфигурации.

## Как пользоваться

Обычный способ — описать задачу менеджера естественным языком, при необходимости явно указав Skill:

```text
$papa-shin-stock-search Покажи летние шины 205/55R16: минимум 4 штуки, цена до 8000, доставка до 3 дней
```

Skill выполняет последовательность `refresh → search`, сообщает `generated_at`, `checked_at`, признак `stale`, количество SKU и единиц, затем показывает ограниченный список товаров и предложений. Если не указан обязательный для выбора параметр, Codex задаёт уточняющий вопрос и не придумывает значение.

Для диагностики или ручного read-only запуска из корня установленного пакета используйте тот же проверенный Python. macOS/Linux:

```bash
"$PAPA_SHIN_PYTHON" scripts/fetch_stock.py
"$PAPA_SHIN_PYTHON" scripts/search_stock.py \
  --product-type 'Шины' \
  --size '205/55R16' \
  --season 'Лето' \
  --min-total-quantity 4 \
  --max-price 8000 \
  --max-delivery-days 3 \
  --limit 10 \
  --offers-limit 5
```

Windows PowerShell:

```powershell
& $PapaShinPython @PapaShinPythonArgs scripts/fetch_stock.py
& $PapaShinPython @PapaShinPythonArgs scripts/search_stock.py `
  --product-type "Шины" `
  --size "205/55R16" `
  --season "Лето" `
  --min-total-quantity 4 `
  --max-price 8000 `
  --max-delivery-days 3 `
  --limit 10 `
  --offers-limit 5
```

Обе команды возвращают один JSON-объект. Поиск запускайте после refresh; при `stale_cache` данные доступны, но ответ обязан явно сообщать об устаревании.

### Параметры CLI

| Параметр | Допустимое значение |
|---|---|
| `--product-type`, `--season`, `--spikes`, `--run-flat`, `--disk-type`, `--truck-axis`, `--truck-construction`, `--supplier` | Текстовое значение, совпадающее со значением в машинных данных. |
| `--size` | Типоразмер с ASCII-цифрами, например `205/55R16`, `205/55 16` или `205 55 R16`; результат нормализуется в `205/55R16`. |
| `--min-total-quantity` | Целое число от 0; по умолчанию `4`. |
| `--max-price` | Конечное десятичное число от 0. |
| `--max-delivery-days` | Целое число дней от 0. |
| `--limit` | Целое число от 1 до 100; по умолчанию `10`. |
| `--offers-limit` | Целое число от 1 до 25; по умолчанию `5`. |

Каждый параметр и его значение передавайте отдельными аргументами. При `query_invalid` сначала сверяйтесь с этой таблицей и `scripts/search_stock.py --help`.

## Примеры запросов менеджера

- «Найди летние шины 205/55R16, минимум четыре штуки, до 8 000 за единицу».
- «Покажи предложения по шинам 205/55R16 с доставкой не дольше трёх дней».
- «Подбери шины на мой автомобиль» — сначала skill запрашивает точный типоразмер и сезон. Он не просит VIN, не обращается к внешнему сервису совместимости и не придумывает размер.

Другие сценарии и ожидаемые уточнения приведены в [references/manager-queries.md](references/manager-queries.md).

## Решение проблем

| Симптом или код | Безопасное действие |
|---|---|
| Skill не обнаружен | Повторите `test -f "$PAPA_SHIN_SKILL_DIR/SKILL.md"` либо Windows-команду `Test-Path` из раздела «Проверка», исключите лишний вложенный каталог и перезапустите Codex либо откройте новую задачу. |
| Python младше 3.11 | Выберите установленный Python 3.11+ и повторите все команды тем же launcher. |
| `config_missing` | Попросите администратора установить готовый env-файл в default path либо проверить `PAPA_SHIN_STOCK_CONFIG`. Не отправляйте содержимое файла в чат или issue. |
| `config_invalid` | Попросите администратора проверить формат, абсолютные пути и права файла. Не печатайте и не пересоздавайте секреты самостоятельно. |
| `auth_failed` | Сообщите администратору только код ошибки; не передавайте username/password в чат, логи или командную строку. |
| `network_error` | Проверьте доступность сети и повторите позже; Skill не раскрывает приватный endpoint. |
| `cache_locked` | Дождитесь завершения другого refresh. Не удаляйте lock-файлы вручную во время работающих процессов. |
| `cache_unavailable` | Проверьте свободное место и корректность отдельного cache leaf; не удаляйте кэш массово по шаблону. |
| `query_invalid` | Сверьте фильтры с таблицей «Параметры CLI» и `scripts/search_stock.py --help`, уменьшите лимиты и передавайте каждое значение отдельным аргументом. |
| `stale_cache` | Поиск разрешён по предыдущему проверенному поколению, но в ответе нужно указать `generated_at`, `checked_at` и предупреждение. |

## Границы безопасности

Пакет только читает удалённые машинные данные, проверяет поколение и контрольные суммы, атомарно обновляет локальный кэш и выполняет локальный поиск. Он не изменяет товары, цены, остатки, предложения или production-системы. Не добавляйте в запросы, логи и обращения в поддержку приватный env-файл, содержимое кэша или коммерческие выгрузки.

## Поддержка

Передайте сопровождающему пакет версию Python, операционную систему, выполненную команду и безопасные поля `status`, `error.code` либо `warnings.code`. Не прикладывайте приватные значения или полный машинный результат.

## Лицензия и данные

Код распространяется по лицензии Apache-2.0; полный текст находится в `LICENSE`. Пакет не включает закрытые данные: доступ и данные предоставляет их владелец отдельно. Названия продуктов и товарные знаки принадлежат их правообладателям; их упоминание не означает аффилированность или одобрение.
