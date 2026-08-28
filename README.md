# Papa Shin Stock Search

Codex Skill для read-only поиска товаров и предложений в подготовленных машинных остатках. Корень этого репозитория является корнем skill package: после установки `SKILL.md` должен лежать непосредственно в каталоге `papa-shin-stock-search`, без дополнительного вложенного каталога.

## Требования

- Python 3.11 или новее;
- подготовленный администратором приватный конфигурационный файл;
- Codex с поддержкой skills.

## Установка в macOS и Linux

Выберите установленный Python 3.11+ и сохраните launcher в отдельной переменной. Во всех последующих командах используйте только её:

```bash
PAPA_SHIN_PYTHON='python3.11'
"$PAPA_SHIN_PYTHON" --version
"$PAPA_SHIN_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Требуется Python 3.11+")'
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

Разместите полученный от администратора приватный файл отдельно от пакета согласно [references/configuration.md](references/configuration.md). Не переносите его в репозиторий.

Если задаётся собственный каталог кэша, используйте новый отсутствующий или пустой leaf-каталог. Непустой legacy-кэш без служебного marker автоматически не принимается: выберите новый путь и повторно загрузите воспроизводимые данные по штатной команде.

## Установка в Windows

> Нативный refresh реализован через изолированный Win32 handle backend. Он проверяется теми же end-to-end тестами в Windows Server 2022 с Python 3.11/3.12 и отдельными mock race/fault-тестами на всех ОС. До зелёного Windows CI для конкретного commit нативный результат остаётся неподтверждённым для этого commit; macOS/Linux tests не заменяют такую проверку.

В PowerShell выберите launcher. Для Python Launcher используйте `py -3.11`; если Python 3.11+ доступен командой `python`, задайте `$PapaShinPython = "python"` и `$PapaShinPythonArgs = @()`:

```powershell
$PapaShinPython = "py"
$PapaShinPythonArgs = @("-3.11")
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

Приватный конфигурационный файл храните в профиле пользователя вне пакета; точное размещение согласуйте с администратором.

После копирования перезапустите Codex или начните новую задачу, чтобы пакет обнаружился. Автоматическое подключение разрешено; явный вызов доступен как `$papa-shin-stock-search`.

## Проверка

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

## Примеры запросов менеджера

- «Найди летние шины 205/55R16, минимум четыре штуки, до 8 000 за единицу».
- «Покажи предложения по шинам 205/55R16 с доставкой не дольше трёх дней».
- «Подбери шины на мой автомобиль» — сначала skill запрашивает точный типоразмер и сезон. Он не просит VIN, не обращается к внешнему сервису совместимости и не придумывает размер.

Другие сценарии и ожидаемые уточнения приведены в [references/manager-queries.md](references/manager-queries.md).

## Границы безопасности

Пакет только читает удалённые машинные данные, проверяет поколение и контрольные суммы, атомарно обновляет локальный кэш и выполняет локальный поиск. Он не изменяет товары, цены, остатки, предложения или production-системы. Не добавляйте в запросы, логи и обращения в поддержку приватный env-файл, содержимое кэша или коммерческие выгрузки.

## Поддержка

Передайте сопровождающему пакет версию Python, операционную систему, выполненную команду и безопасные поля `status`, `error.code` либо `warnings.code`. Не прикладывайте приватные значения или полный машинный результат.

## Лицензия и данные

Код распространяется по лицензии Apache-2.0; полный текст находится в `LICENSE`. Пакет не включает закрытые данные: доступ и данные предоставляет их владелец отдельно. Названия продуктов и товарные знаки принадлежат их правообладателям; их упоминание не означает аффилированность или одобрение.
