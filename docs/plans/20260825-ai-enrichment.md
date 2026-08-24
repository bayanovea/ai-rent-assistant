# AI-обогащение листингов — итерация 2

## Overview

Итерация 1 собрала pipeline `URL → SQLite → CSV/HTML`. Все поля извлекаются регекспами; `extractors.py` прямым текстом говорит, что AI-обогащение — следующая итерация, а `raw_json` сохраняется «для будущего AI».

Эта итерация делает то самое обогащение: инструмент, который можно натравить на ещё не обработанные листинги, чтобы AI вытащил из описания и остальных полей значимые факты, заполнил колонки таблицы и написал короткий вердикт по каждому объекту. Цель — вести поиск жилья по одной HTML-странице, а не по 40 вкладкам Facebook.

Три вещи определяют архитектуру:

1. **AI здесь — это Claude Code (Opus, high thinking effort)**, а не API-клиент. Поэтому «инструмент» — это контракт «batch наружу / результат внутрь» плюс skill, который гоняет цикл. Никаких новых runtime-зависимостей, никакого `anthropic` SDK.
2. **Схема должна эволюционировать.** Когда AI замечает повторяющийся полезный сигнал, для которого нет колонки, он спрашивает: важно ли это, и переанализировать ли старые листинги или оставить им `unknown` отдельным цветом.
3. **Текущий текст из скрапера непригоден для анализа.** У 10 из 21 строк `description` вообще NULL; у большинства остальных туда попал сайдбар «Выбор дня» вместо самого объявления. `title` = `"Недавно опубликовано"` у 100% строк, `published_at` NULL у 100%. Скормить это AI — получить уверенную чушь, поэтому чинится первым.

Ничего из этого не является ценным само по себе: парсинг и анализ можно перезапустить с нуля в любой момент. Незаменимы только собственные решения пользователя (принял/отклонил) — они живут вне БД.

## Критерии поиска, которые кодируются

Пара + маленький небольшая собака, переезд 1 декабря 2026, контракт на год, 15 000–30 000 THB/мес. Дом или таунхаус в хорошем охраняемом посёлке (типа Pruksa Ville), полностью меблированный, ≥1 спальня + гостиная, не крошечная кухня, зелёная зона рядом для прогулок, парковка и хороший интернет. Туристическая зона и близость к пляжу не требуются.

**Разрешение на животных — самый важный критерий.**

Всё это живёт в новом редактируемом `config/requirements.md` (текст письма агенту дословно + чеклист критериев), который целиком вставляется в каждый batch. Поменять приоритеты позже = отредактировать один markdown-файл, а не код.

## Context (from discovery)

- **Репозиторий**: `~/AI/fb-rent/scrapper` (git root), рантайм-данные в соседнем `~/AI/fb-rent/data/` (`FB_RENT_HOME`, gitignored).
- **Стек**: Python 3.11+, click CLI, Playwright, SQLite. Пакет `fb-rent` v0.1.0, `src/`-layout, entry point `fb-rent = fb_rent.cli:main`. Тесты — pytest, 67+ штук, `pythonpath = ["src"]`.
- **Существующие модули**: `config.py`, `url_builder.py`, `extractors.py` (чистые функции + table-driven тесты), `storage.py` (`init_db`/`upsert_listing`/`export_csv`/`export_html`), `browser.py`, `scraper_list.py`, `scraper_detail.py`, `cli.py`.
- **БД**: `data/rentals.db`, одна таблица `listings` (`fb_id` PK, `url`, `title`, `price_thb`, `price_raw`, `district`, `bedrooms`, `property_type`, `description`, `published_at`, `first_seen_at`, `last_seen_at`, `raw_json`). 21 строка.
- **`raw_json`** — `{"url", "final_url", "title", "text_lines": [...]}`, где `text_lines` = первые 200 непустых строк detail-страницы. Это самый богатый и самый надёжный материал для AI, богаче колонки `description`.
- **HTML-экспорт**: `storage.export_html` (`storage.py:207-341`) — self-contained файл, данные инлайном в `const DATA`, рендер через Grid.js с unpkg CDN, экранирование `</` и `<!--` плюс хелпер `esc()`.
- **AI-кода нет вообще**: `grep -riE "anthropic|openai|claude|llm"` по `src/` пуст. Зависимости — только playwright, click, python-dateutil.

## Development Approach

- **testing approach**: Regular (код сначала, тесты после). Unit-тесты обязательны для всех чистых функций: экстракторы, реестр полей, правила решений, слой БД, сборка/приём batch.
- complete each task fully before moving to the next
- make small, focused changes
- **CRITICAL: every task MUST include new/updated tests** для чистых функций. Для Playwright-кода и для CLI-обёрток тесты пропускаем осознанно (помечено `DESIGN:`).
- **CRITICAL: all tests must pass before starting next task** — no exceptions
- **CRITICAL: update this plan file when scope changes during implementation**
- run tests after each change
- обратная совместимость: существующие 67 тестов должны остаться зелёными, `fb-rent scrape/export/login` не меняют сигнатуры

## Autonomous Execution Notes (for ralphex / agent runs)

Конвенция та же, что в итерации 1:

- Чекбоксы с префиксом `MANUAL:` — агент **пропускает и помечает** `[x] MANUAL: ... (skipped — requires live FB / human action)`. Не пытается выполнить, не блокирует прогресс.
- Чекбоксы с префиксом `DESIGN:` — фиксация проектного решения (например, «не пишем unit-тесты для Playwright-кода осознанно»). Агент **помечает** `[x] DESIGN: ...` без действий.
- Всё остальное автоматизируется как обычно: код + unit-тесты + `pytest`.
- Одна задача за итерацию; после задачи — прогон полного `pytest`, отметка чекбоксов, коммит.

**Специфика этой итерации — что агент выполнить не может:**

1. `fb-rent scrape --refresh` требует живой залогиненной сессии FB → `MANUAL:`.
2. **Сам прогон AI-анализа выполнить автономно нельзя**: он требует интерактивной сессии Claude Code (шаг с `AskUserQuestion` про новые поля) и свежих данных из FB. Агент строит **инструментарий**, а не выполняет анализ → `MANUAL:`.

Поэтому каждая задача обязана быть проверяемой **без живого Facebook и без человека**: вместо живых данных используются зафиксированные фикстуры (Task 1) и синтетические словари в тестах. Ни один автоматизируемый чекбокс в этом плане не должен требовать сети.

Все `MANUAL:` пункты продублированы в Post-Completion — оттуда человек забирает список ручных действий после автономного прогона.

## Testing Strategy

- **unit tests**: обязательны для `description_from_text_lines`, `clean_fb_title`, RU-парсинга дат, `ai_schema` (загрузка/валидация реестра, `source_hash`), `rules.decide`, `storage` (новые таблицы, `fetch_enriched`), `decisions`, `ai_batch` (выборка pending, приём результатов, отказ на неизвестные ключи).
- **fixture-based tests**: реальные `text_lines` двух листингов, зафиксированные в `tests/fixtures/` на Task 1 — на них проверяются экстракторы. Это замена интеграционным тестам с живым FB.
- **integration tests**: НЕ пишем для Playwright-скраперов и для CLI-обёрток (осознанно, помечено `DESIGN:`).
- **e2e**: не применимо, UI нет — HTML-экспорт проверяется тестом на присутствие ожидаемых CSS-классов в сгенерированной строке.
- **manual verification**: полный цикл `scrape --refresh → ai-batch → анализ в сессии → ai-apply → export` после Task 13.

## Progress Tracking

- mark completed items with `[x]` immediately when done
- add newly discovered tasks with ➕ prefix
- document issues/blockers with ⚠️ prefix
- update plan if implementation deviates from original scope
- keep plan in sync with actual work done

## Solution Overview

```
fb-rent scrape --refresh        →  rentals.db (listings)
fb-rent ai-batch                →  data/ai/batch-<ts>.json   ← pending + реестр полей + requirements
     Claude Code (Opus, high effort) читает, думает, пишет:
                                   data/ai/result-<ts>.json
fb-rent ai-apply <file>         →  rentals.db (ai_analysis)
fb-rent export                  →  data/rentals.html
```

Весь цикл запускается одной командой через skill `/analyze`.

Ключевые решения:

1. **AI извлекает факты, код применяет политику.** AI никогда не пишет `status` и `reason` — он пишет значения полей с уверенностью и цитатой-доказательством. `rules.py` детерминированно выводит active/rejected. Поэтому авторежекты воспроизводимы, объяснимы и меняются глобально при смене правил — без переанализа.
2. **Разделение персистентности.** Результаты AI лежат в SQLite (регенерируемы — можно стирать смело). Решения пользователя лежат в `data/decisions.json` (нерегенерируемы — их не трогают ни scrape, ни анализ) и являются source of truth, зеркалируются в БД при экспорте. Решение пользователя всегда перекрывает вердикт AI.
3. **Реестр полей вместо миграций.** Новое поле = запись в `config/ai_fields.json`. Никаких ALTER TABLE: значения лежат в `data_json`.
4. **`added_in` versioning** — именно то, что позволяет отличить «поля не было на момент анализа» (отдельный цвет) от «поле было, но AI не нашёл информацию» (обычный серый прочерк).
5. **`source_hash`** — детектор устаревания. Пересканирование, улучшившее текст листинга, автоматически возвращает его в очередь на анализ.
6. **Починка экстракторов идёт первой**, потому что качество анализа целиком зависит от качества входного текста.

## Technical Details

### Реестр полей — `config/ai_fields.json`

```json
{
  "schema_version": 1,
  "fields": [
    { "key": "pets", "label": "Pets", "group": "core", "type": "enum",
      "values": ["allowed", "not_allowed", "unclear"],
      "highlight": "pets", "added_in": 1,
      "prompt": "Разрешены ли животные/собаки? 'allowed' ТОЛЬКО при явном подтверждении (pets allowed, pet friendly, dogs ok, สัตว์เลี้ยงได้). 'not_allowed' ТОЛЬКО при явном запрете. Во всех остальных случаях 'unclear'. Обязательно цитату в ev." }
  ]
}
```

Поля v1 (все четыре группы), `added_in: 1` у всех:

| group | key | type | комментарий |
|---|---|---|---|
| core | `pets` | enum allowed/not_allowed/unclear | главный критерий |
| core | `pets_evidence` | text | цитата-основание |
| core | `price_month_thb` | int | цена, которую AI считает реальной |
| core | `price_12m_thb` | int | цена при годовом контракте, если объявление даёт тарифную сетку |
| core | `deposit` | text | «2 месяца депозит + 1 предоплата» |
| core | `bedrooms` | int | |
| core | `bathrooms` | int | |
| core | `extra_rooms` | text | кабинет, вторая гостиная и т.п. |
| core | `living_room` | enum yes/no/unclear | отсутствие упоминания = `unclear`, НЕ повод для реджекта |
| core | `furnished` | enum full/partial/none/unclear | |
| property | `property_type` | enum house/townhouse/villa/condo/apartment/studio/unknown | |
| property | `project_name` | text | посёлок / название проекта |
| property | `area` | text | нормализованный район |
| property | `size_sqm` | int | |
| property | `floors` | int | |
| property | `condition` | enum new/good/worn/unclear | |
| amenity | `kitchen` | enum full/kitchenette/none/unclear | |
| amenity | `parking` | enum yes/no/unclear | |
| amenity | `internet` | text | скорость/лимит, если указаны |
| amenity | `aircon` | text | количество |
| amenity | `pool_gym` | text | |
| amenity | `green_area` | enum yes/no/unclear | для выгула собаки |
| amenity | `security` | enum yes/no/unclear | охрана/КПП |
| meta | `contract_min_months` | int | |
| meta | `available_from` | date | нужно к 01.12.2026 |
| meta | `posted_at` | date | |
| meta | `poster_type` | enum agent/owner/unclear | |
| meta | `contact` | text | часто `[hidden information]` |

Плюс служебное `data_quality` (`good` / `sidebar_noise` / `missing`) — хранится отдельной колонкой, не в реестре.

### Новые таблицы

```sql
CREATE TABLE IF NOT EXISTS ai_analysis (
    fb_id          TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    analyzed_at    TEXT NOT NULL,
    source_hash    TEXT NOT NULL,   -- детектор устаревания
    model          TEXT,            -- "claude-opus-5 (claude-code)"
    verdict        TEXT NOT NULL,   -- короткий вердикт свободным текстом
    score          INTEGER,         -- 0-100 соответствие критериям
    data_quality   TEXT,            -- good | sidebar_noise | missing
    data_json      TEXT NOT NULL    -- {key: {"v":..., "conf":"high|med|low", "ev":"цитата"}}
);

CREATE TABLE IF NOT EXISTS decisions (   -- зеркало data/decisions.json
    fb_id      TEXT PRIMARY KEY,
    status     TEXT NOT NULL,        -- rejected | active | shortlist
    reason     TEXT,
    decided_at TEXT NOT NULL
);
```

`source_hash` = sha256 по тому, что AI реально видел: `title | price_raw | district | description_clean | len(text_lines)`.

Листинг считается **pending**, если: нет строки в `ai_analysis`, ИЛИ его `source_hash` отличается, ИЛИ его `schema_version` ниже версии реестра, поднятой с `--backfill`.

### Формат batch — `data/ai/batch-<ts>.json`

```json
{ "schema_version": 1,
  "generated_at": "2026-08-25T10:00:00Z",
  "requirements_md": "<содержимое config/requirements.md>",
  "fields": [ ...реестр целиком... ],
  "items": [
    { "fb_id": "...", "url": "...", "price_thb": 16000, "price_raw": "16 000 ฿/месяц",
      "district": "Kathu, Phuket, Thailand", "listing_title": "1 Bed 1 Bath - Apartment",
      "description": "<очищенный текст>", "text_lines": [ "..." ],
      "source_hash": "abc123..." }
  ] }
```

### Формат результата — `data/ai/result-<ts>.json`

```json
{ "schema_version": 1,
  "model": "claude-opus-5 (claude-code)",
  "results": [
    { "fb_id": "...", "source_hash": "abc123...",
      "verdict": "Новый кондо-студия у Kathu, всё есть, но про животных ни слова и это не дом в посёлке.",
      "score": 62, "data_quality": "good",
      "fields": { "pets": {"v": "unclear", "conf": "high", "ev": ""},
                  "price_12m_thb": {"v": 16000, "conf": "high", "ev": "12 месяцев: 16,000 THB/month"} } } ],
  "proposed_new_fields": [
    { "key": "electricity_rate", "label": "Свет ฿/kWh", "type": "text",
      "why": "встречается в 3 из 8 листингов, разброс 4–8 ฿/kWh — это ±2000 THB/мес",
      "examples": ["Electricity 7 THB/unit", "ไฟฟ้า 5 บาท/หน่วย"] } ] }
```

`ai-apply` **отвергает с ошибкой** любой ключ в `fields`, которого нет в реестре, перечисляя такие ключи. `proposed_new_fields` собираются и печатаются — это и есть триггер разговора о расширении схемы.

### Правила решений (`rules.py`)

Авторежект ровно три штуки:

| Условие | Reason |
|---|---|
| `pets == "not_allowed"` | Животные запрещены |
| `price > 35 000` | Сильно вне бюджета |
| явная студия (`property_type == "studio"` или `bedrooms == 0`) | Студия |

Всё остальное — флажок, но не реджект. Явно:

- **Больше комнат, чем просили, — никогда не минус.** Лишние комнаты повышают score.
- **Неупомянутая гостиная — не повод для реджекта.** Большинство объявлений просто не пишут слово «living room». Это жёлтый флажок.
- Кондо/апартаменты, только краткосрочная аренда, нет парковки, слабый интернет, нет зелёной зоны → флажок + отражение в вердикте, листинг остаётся активным.
- 30 001–35 000 THB → активен, ячейка цены оранжевая.
- `data_quality != "good"` → **никогда не реджект**, помечается как «нужен re-scrape».

Цветовые классы (возвращает `rules.cell_class`):

| Состояние | Класс / цвет |
|---|---|
| `pets = allowed` | `c-good` — зелёный |
| `pets = unclear` / не упомянуто | `c-warn` — жёлтый |
| `pets = not_allowed` | `c-bad` — красный + строка авторежектнута |
| цена ≤30k / 30–35k / >35k | обычный / `c-warn` / `c-bad` |
| поле пустое, добавлено **после** анализа этой строки (`added_in > row.schema_version`) | `c-stale` — **отдельный четвёртый цвет** (штриховка сине-серым) |
| поле пустое, но существовало на момент анализа | `c-none` — обычный серый прочерк |
| строка отклонена | `row-rejected` — приглушение, зачёркивание, красная левая граница |

### CLI

```sh
fb-rent ai-status                    # analyzed / pending / stale / no-raw-data
fb-rent ai-batch [--limit 25] [--all] [--ids ID...] [--out PATH]
fb-rent ai-apply PATH [--dry-run]
fb-rent ai-field list
fb-rent ai-field add --key K --label L --type enum --values a,b,c [--group G] [--after K] [--backfill]
fb-rent reject ID --reason "..."     # → data/decisions.json, перекрывает AI
fb-rent unreject ID
fb-rent shortlist ID [--reason "..."]
fb-rent export                       # имя прежнее, рендерит полную таблицу
```

`--backfill` поднимает `schema_version` и помечает существующие строки устаревшими, чтобы `ai-batch` их подобрал; без флага старые строки просто получают цвет `c-stale`. Этот флаг и есть ответ на вопрос «переанализировать или оставить unknown», вшитый прямо в CLI.

### HTML

Переписывается `export_html()` (`storage.py:207-341`), существующие приёмы сохраняются: self-contained файл, инлайн `const DATA`, Grid.js с unpkg, экранирование `</` / `<!--`, хелпер `esc()`.

- **Все колонки видимы, горизонтальный скролл**, sticky-заголовок и sticky первая колонка, чтобы строка не терялась при скролле вправо.
- Вкладки-фильтры: **Active** (по умолчанию) / Shortlist / Rejected / All со счётчиками.
- Отклонённые строки несут свою причину в отдельной колонке **Reason** и визуально отбиты (приглушение + зачёркивание + красная граница), с пометкой, кто отклонил: `AI` или `вы`.
- Вердикт — обрезанная ячейка, полный текст по hover/expand. Цитаты-доказательства (`ev`) выводятся как тултипы ячеек, чтобы любое утверждение AI прослеживалось до исходного текста.
- Легенда цветов сверху страницы, покрывающая все состояния из таблицы выше.

Замечание: Grid.js грузится с CDN, странице нужен интернет. Это существующее поведение; вендорить — при необходимости, не в этой итерации.

## What Goes Where

- **Implementation Steps** (`[ ]` checkboxes): весь код обогащения, реестр, правила, CLI, HTML, skill, тесты — всё, что делается в этом репо без сети.
- **Post-Completion** (no checkboxes): пересканирование живого FB, первый реальный прогон AI-анализа, визуальная проверка HTML, планы на итерацию 3.

## Implementation Steps

### Task 1: Фикстуры реальных данных для тестов экстракторов

**Files:**
- Create: `tests/fixtures/__init__.py`
- Create: `tests/fixtures/text_lines_good.json`
- Create: `tests/fixtures/text_lines_sidebar.json`
- Create: `tests/fixtures/README.md`

- [x] вытащить `raw_json` двух реальных листингов из `data/rentals.db` через `sqlite3`/python и сохранить как фикстуры: «хороший» `fb_id=9000000000000001` (The Origin Kathu, настоящее описание после маркера `Описание`) и «плохой» `fb_id=9000000000000002` (в `description` попал сайдбар «Выбор дня», настоящий текст — только в `text_lines`)
- [x] если этих `fb_id` в БД нет — взять любые два: один с непустым `description` длиной НЕ 1288 и НЕ 962 символа, второй ровно с такой длиной (это сигнатура сайдбарного мусора); зафиксировать в `tests/fixtures/README.md`, какие именно взяты
- [x] если `data/rentals.db` отсутствует — собрать фикстуры руками по образцам структуры из раздела Technical Details: `text_lines` содержит `"Описание"`, затем строки объявления, затем `"Информация о продавце"`
- [x] в `tests/fixtures/README.md` описать происхождение фикстур и что `data/` в gitignore, поэтому фикстуры коммитятся
- [x] написать `tests/test_fixtures.py`: обе фикстуры парсятся как JSON, содержат ключи `title` и `text_lines`, `text_lines` непустой список строк
- [x] прогнать `pytest tests/ -q` — все зелёные

### Task 2: Экстракторы описания, заголовка и даты публикации

**Files:**
- Modify: `src/fb_rent/extractors.py`
- Modify: `tests/test_extractors.py`

- [x] `description_from_text_lines(text_lines: list[str]) -> str | None` — вырезает срез между маркером `Описание`/`Description` и маркером `Информация о продавце`/`Seller information`; отбрасывает хвостовое `Ещё`/`See more`; возвращает `None`, если маркера нет
- [x] `clean_fb_title(raw_title: str) -> str | None` — из `"(6) Marketplace — 1 Bed 1 Bath - Apartment | Facebook"` делает `"1 Bed 1 Bath - Apartment"`: снимает префикс `(N) Marketplace — ` (и `Marketplace | `) и суффикс ` | Facebook`; возвращает `None` для мусора вроде `"Недавно опубликовано"`
- [x] расширить `_PUBLISHED_RE` русскими шаблонами `Опубликовано N минут/часов/дней/недель/месяцев назад` рядом с существующими английскими `... ago`; вернуть ISO-дату относительно переданного `now`
- [x] написать тесты на `description_from_text_lines` на обеих фикстурах Task 1: на «хорошей» вернуть текст The Origin Kathu, на «плохой» — реальный текст объявления, а НЕ сайдбар «Выбор дня»
- [x] написать тесты на `clean_fb_title` (нормальный заголовок, тайский заголовок, `"Недавно опубликовано"` → None, пустая строка) и на RU/EN парсинг дат (часы, дни, недели, неизвестный формат → None)
- [x] прогнать `pytest tests/ -q` — все зелёные, существующие 67 тестов не сломаны

### Task 3: Подключение исправленных экстракторов к скраперам

**Files:**
- Modify: `src/fb_rent/scraper_detail.py`
- Modify: `src/fb_rent/scraper_list.py`

- [ ] в `scraper_detail._extract_description()` (`scraper_detail.py:118`) сначала пробовать `description_from_text_lines(raw_json["text_lines"])`; на старую эвристику «самый длинный текстовый блок» откатываться, только если маркер не найден
- [ ] в `scraper_detail` парсить `published_at` через обновлённый экстрактор и класть ISO-дату вместо NULL
- [ ] в `scraper_list._pick_title()` использовать `clean_fb_title(raw_json["title"])`; если вернулся `None` — прежняя логика «первая не-ценовая строка»
- [ ] DESIGN: unit-тесты для Playwright-модулей не пишем осознанно (сеть + живой FB = хрупко); поведение покрыто тестами экстракторов на фикстурах в Task 2
- [ ] прогнать `pytest tests/ -q` — все зелёные
- [ ] MANUAL: `fb-rent scrape --refresh` — перезалить 21 существующую строку корректным текстом (skipped — requires live FB / human action)

### Task 4: Реестр полей и файл требований

**Files:**
- Create: `config/ai_fields.json`
- Create: `config/requirements.md`
- Create: `src/fb_rent/ai_schema.py`
- Create: `tests/test_ai_schema.py`
- Modify: `src/fb_rent/config.py`

- [ ] создать `config/ai_fields.json` со `schema_version: 1` и всеми полями из таблицы в Technical Details; у каждого поля `key`, `label`, `group`, `type`, `values` (для enum), `added_in: 1`, `prompt` — короткая инструкция для AI, что именно извлекать
- [ ] создать `config/requirements.md`: текст письма агенту дословно (пара + небольшая собака, переезд 01.12.2026, год контракта, 15–30k THB, дом/таунхаус в посёлке, меблировка, 1 спальня + гостиная, кухня, зелёная зона, парковка, интернет) плюс чеклист приоритетов, где животные помечены как критерий №1
- [ ] `src/fb_rent/ai_schema.py`: `load_registry(path) -> Registry` с валидацией (уникальность `key`, известный `type`, `values` непуст для enum, `added_in <= schema_version`), `field_keys()`, `schema_version`
- [ ] `ai_schema.source_hash(listing: dict) -> str` — sha256 по `title | price_raw | district | description | len(text_lines)`, стабильный к порядку ключей
- [ ] добавить в `config.py`: `AI_DIR = FB_RENT_HOME / "ai"`, `AI_FIELDS_PATH`, `REQUIREMENTS_PATH`, `BUDGET_HARD_MAX_THB = 35_000`
- [ ] написать `tests/test_ai_schema.py`: реальный `config/ai_fields.json` загружается и валиден, дубль `key` → ошибка, enum без `values` → ошибка, `source_hash` детерминирован и меняется при смене описания
- [ ] прогнать `pytest tests/ -q` — все зелёные

### Task 5: Правила решений и цветовые классы

**Files:**
- Create: `src/fb_rent/rules.py`
- Create: `tests/test_rules.py`

- [ ] `decide(fields: dict, price_thb: int | None) -> tuple[str, str | None]` — возвращает `("active"|"rejected", reason)`; ровно три условия реджекта: `pets == "not_allowed"`, `price > BUDGET_HARD_MAX_THB`, явная студия (`property_type == "studio"` или `bedrooms == 0`)
- [ ] `cell_class(field_key, value, field_def, row_schema_version) -> str` — реализовать таблицу цветов: `c-good`/`c-warn`/`c-bad` для `pets`, пороги цены, `c-stale` когда `field_def["added_in"] > row_schema_version`, иначе `c-none` для пустых
- [ ] `score(fields) -> int` — 0-100 соответствие критериям из `requirements.md`; лишние комнаты и наличие зелёной зоны/парковки/охраны повышают, отсутствие информации не штрафует сильно
- [ ] написать тесты на каждое условие реджекта и, отдельными явными кейсами, на то что **НЕ** реджектится: 3 спальни (больше, чем просили) → active; `living_room` = `unclear` → active; `property_type` = `condo` → active; цена 33 000 → active с `c-warn`; `data_quality` = `sidebar_noise` → active
- [ ] написать тесты на `cell_class`: `pets=allowed` → `c-good`, `pets=unclear` → `c-warn`, поле с `added_in=2` на строке со `schema_version=1` → `c-stale`, поле с `added_in=1` без значения → `c-none`
- [ ] прогнать `pytest tests/ -q` — все зелёные

### Task 6: Слой хранения для результатов AI

**Files:**
- Modify: `src/fb_rent/storage.py`
- Modify: `tests/test_storage.py`

- [ ] в `init_db()` добавить `CREATE TABLE IF NOT EXISTS ai_analysis` и `decisions` по схеме из Technical Details, идемпотентно; существующая таблица `listings` не трогается
- [ ] `upsert_ai_analysis(conn, row: dict) -> str` — insert/update по `fb_id`, возвращает `"inserted"`/`"updated"`
- [ ] `fetch_pending(conn, registry) -> list[dict]` — листинги без строки в `ai_analysis`, либо с другим `source_hash`, либо со `schema_version` ниже текущей; исключить листинги без `description` и без `raw_json` и вернуть их отдельным списком «нет сырых данных»
- [ ] `fetch_enriched(conn) -> list[dict]` — JOIN `listings` + `ai_analysis` + `decisions`, с распакованным `data_json`; решение пользователя перекрывает вердикт AI
- [ ] написать тесты на in-memory SQLite: `init_db` создаёт три таблицы и повторный вызов не падает, upsert сохраняет и обновляет, `fetch_pending` корректно относит листинг к pending по каждому из трёх условий и отсеивает те, у которых нет сырых данных, `fetch_enriched` отдаёт решение пользователя поверх AI
- [ ] прогнать `pytest tests/ -q` — все зелёные

### Task 7: Решения пользователя вне БД

**Files:**
- Create: `src/fb_rent/decisions.py`
- Create: `tests/test_decisions.py`

- [ ] `load_decisions(path) -> dict[str, dict]` / `save_decision(path, fb_id, status, reason)` — работа с `data/decisions.json`, атомарная запись через temp-файл + rename, отсутствующий файл = пустой словарь
- [ ] `sync_to_db(conn, decisions)` — заливает содержимое файла в таблицу `decisions` (файл — source of truth, таблица — зеркало для JOIN)
- [ ] валидация `status` — только `rejected` / `active` / `shortlist`, иначе `ValueError`
- [ ] написать тесты: запись и чтение по кругу, перезапись существующего решения, некорректный статус, отсутствующий файл, `sync_to_db` наполняет таблицу
- [ ] написать тест «переживает стирание БД»: сохранить решение, создать БД заново через `init_db`, вызвать `sync_to_db` — решение и его причина на месте
- [ ] прогнать `pytest tests/ -q` — все зелёные

### Task 8: Сборка batch и приём результатов

**Files:**
- Create: `src/fb_rent/ai_batch.py`
- Create: `tests/test_ai_batch.py`

- [ ] `build_batch(conn, registry, requirements_md, limit, ids=None) -> dict` — формирует JSON batch по формату из Technical Details; в каждый item кладёт `description` (через `description_from_text_lines`, если колонка испорчена) и `text_lines`, плюс `source_hash`
- [ ] `write_batch(batch, out_dir) -> Path` — пишет `data/ai/batch-<ts>.json`, создаёт директорию при необходимости
- [ ] `apply_results(conn, registry, result: dict) -> ApplyReport` — валидирует `schema_version`, отвергает с ошибкой любой ключ в `fields`, которого нет в реестре (перечислив их), считает `decide`/`score` через `rules`, вызывает `upsert_ai_analysis`; возвращает счётчики и список `proposed_new_fields`
- [ ] при несовпадении `source_hash` в результате с текущим в БД — предупреждение в отчёт, запись всё равно принимается (данные могли обновиться между batch и apply)
- [ ] написать тесты: batch содержит только pending и не больше `limit`, испорченный `description` подменяется текстом из `text_lines`, `apply_results` кладёт строку в `ai_analysis`, неизвестный ключ поля → ошибка со списком ключей, `proposed_new_fields` пробрасываются в отчёт, несовпадение `source_hash` даёт предупреждение но не ошибку
- [ ] прогнать `pytest tests/ -q` — все зелёные

### Task 9: CLI-команды

**Files:**
- Modify: `src/fb_rent/cli.py`
- Create: `tests/test_cli_ai_field.py`

- [ ] `fb-rent ai-status` — печатает analyzed / pending / stale / «нет сырых данных, нужен scrape --refresh» со счётчиками
- [ ] `fb-rent ai-batch [--limit 25] [--all] [--ids ...] [--out PATH]` — собирает и пишет batch, печатает путь и количество; при нуле pending говорит об этом и выходит с кодом 0
- [ ] `fb-rent ai-apply PATH [--dry-run]` — применяет результат, печатает отчёт: сколько принято, сколько авторежектнуто и по какой причине, какие новые поля предложены
- [ ] `fb-rent ai-field list` и `fb-rent ai-field add --key --label --type [--values] [--group] [--after] [--backfill]` — добавляет поле в `config/ai_fields.json`; с `--backfill` поднимает `schema_version` и помечает существующие строки устаревшими, без него старые строки просто получат `c-stale`
- [ ] `fb-rent reject ID --reason "..."`, `fb-rent unreject ID`, `fb-rent shortlist ID [--reason]` — пишут в `data/decisions.json` через `decisions.py`
- [ ] DESIGN: тесты на тонкие CLI-обёртки не пишем — логика покрыта юнит-тестами нижних слоёв; исключение — `ai-field add`, где есть реальная мутация реестра
- [ ] написать `tests/test_cli_ai_field.py`: `ai-field add` на копии реестра добавляет поле с корректным `added_in`, `--after` ставит поле в нужную позицию, дубликат ключа → ошибка, `--backfill` поднимает `schema_version`
- [ ] прогнать `pytest tests/ -q` и `fb-rent --help` — новые команды видны

### Task 10: Переписанный HTML-экспорт

**Files:**
- Modify: `src/fb_rent/storage.py`
- Modify: `tests/test_storage.py`

- [ ] переписать `export_html()`: строит строки через `fetch_enriched`, для каждой ячейки берёт класс из `rules.cell_class`, инлайнит данные в `const DATA` с прежним экранированием `</` и `<!--` и хелпером `esc()`
- [ ] все колонки видимы с горизонтальным скроллом, sticky-заголовок и sticky первая колонка
- [ ] вкладки-фильтры Active (по умолчанию) / Shortlist / Rejected / All со счётчиками; колонка **Reason** всегда присутствует; отклонённые строки получают `row-rejected` (приглушение, зачёркивание, красная левая граница) и пометку кто отклонил — `AI` или `вы`
- [ ] вердикт — обрезанная ячейка с полным текстом по hover; цитаты `ev` выводятся как `title`-тултипы ячеек
- [ ] легенда цветов сверху страницы, покрывающая `c-good` / `c-warn` / `c-bad` / `c-stale` / `c-none` / `row-rejected`
- [ ] написать тест на синтетическом наборе из трёх строк (pets allowed / unclear / not_allowed): сгенерированный HTML содержит `c-good`, `c-warn`, `c-bad`, `row-rejected`, текст причины реджекта и все заголовки колонок из реестра
- [ ] прогнать `pytest tests/ -q` — все зелёные

### Task 11: Skill `/analyze`

**Files:**
- Create: `.claude/skills/analyze/SKILL.md`

- [ ] frontmatter с `name: analyze` и описанием-триггером («проанализировать необработанные листинги»)
- [ ] шаги 1–3: `fb-rent ai-status`; если есть листинги без сырых данных — сказать пользователю запустить `fb-rent scrape --refresh` и остановиться; иначе `fb-rent ai-batch --limit 25` и прочитать batch вместе с `config/requirements.md`
- [ ] шаги 4–6 (правила извлечения): факты строить по `text_lines` (срез после `Описание`), а не по возможно испорченной колонке `description`; выставлять `data_quality`; ничего не выдумывать — отсутствие доказательства означает `unclear`; про животных — `allowed` только при явном подтверждении, `not_allowed` только при явном запрете, всегда с цитатой в `ev`; лишние комнаты — плюс, а не минус; неупомянутая гостиная — не дефект; написать короткий вердикт по каждому листингу с учётом всего известного, включая то, для чего колонок ещё нет
- [ ] шаг 7 (эволюция схемы): если один и тот же полезный сигнал встретился в ≥2 листингах и поля для него нет — добавить в `proposed_new_fields`
- [ ] шаги 8–10: `fb-rent ai-apply <result>`; если поля предложены — задать пользователю через `AskUserQuestion` два вопроса: (а) действительно ли это важно, (б) переанализировать старые листинги или оставить им `unknown` отдельным цветом; затем `fb-rent ai-field add [--backfill]` и возврат к шагу 2 при backfill; в конце `fb-rent export` и отчёт: счётчики, авторежекты с причинами, топ-5 по score
- [ ] DESIGN: автотестов на файл-промпт не пишем — проверяется реальным прогоном в Post-Completion
- [ ] прогнать `pytest tests/ -q` — все зелёные

### Task 12: Verify acceptance criteria

**Автоматизируемое** (агент выполняет):
- [ ] `pytest tests/ -q` — весь набор зелёный, включая 67 тестов итерации 1
- [ ] `fb-rent --help` показывает `ai-status`, `ai-batch`, `ai-apply`, `ai-field`, `reject`, `unreject`, `shortlist`
- [ ] `python -c "from fb_rent.ai_schema import load_registry; r=load_registry(); print(r.schema_version, len(r.fields))"` — реестр грузится, поля на месте
- [ ] идемпотентность: на временной БД собрать batch из 3 листингов, применить синтетический результат, повторно вызвать `fetch_pending` — эти 3 не возвращаются; изменить `description` одного из них — возвращается ровно он
- [ ] эволюция схемы: `ai-field add --key balcony --label Balcony --type enum --values yes,no,unclear` без `--backfill` → в HTML старые строки получают `c-stale`, не `c-none`; повторить с `--backfill` → строки снова в pending
- [ ] решения переживают стирание БД: `reject` на временных данных, удалить файл БД, `init_db` + `sync_to_db` — решение и причина на месте
- [ ] `fb-rent export` на БД без единой строки `ai_analysis` — валидный HTML без падения

**Требует ручных действий** (агент помечает как skipped):
- [ ] MANUAL: `fb-rent scrape --refresh` — перезалить листинги исправленными экстракторами (skipped — requires live FB / human action)
- [ ] MANUAL: проверить в sqlite3, что ни один `description` не равен сайдбарному boilerplate длиной 1288/962 символа, `title` больше нигде не `"Недавно опубликовано"`, `published_at` не NULL там, где FB показывал дату (skipped — requires live FB / human action)
- [ ] MANUAL: полный цикл `/analyze` в интерактивной сессии Claude Code (Opus, high effort) на 3–5 листингах (skipped — requires interactive AI session)
- [ ] MANUAL: открыть `data/rentals.html` — колонка Pets зелёная/жёлтая/красная как задумано, отклонённые строки визуально отличимы и с причинами, все колонки достижимы горизонтальным скроллом, легенда соответствует реальности (skipped — requires human review)
- [ ] MANUAL: проверить на реальных данных, что ни один листинг не отклонён за лишние комнаты и ни один — за неупомянутую гостиную (skipped — requires live data)

### Task 13: [Final] Update documentation

**Files:**
- Modify: `README.md`

- [ ] добавить в `README.md` раздел про AI-обогащение: цикл `ai-batch → анализ → ai-apply → export`, описание всех новых команд с примерами, где живут `config/ai_fields.json`, `config/requirements.md`, `data/decisions.json` и `data/ai/`
- [ ] описать, как расширять схему (`ai-field add`, смысл `--backfill`, что означает цвет `c-stale`) и как отклонять листинги вручную
- [ ] отметить, что это iteration 2, и линкануть на этот plan-файл
- [ ] переместить план: `mkdir -p docs/plans/completed && mv docs/plans/20260825-ai-enrichment.md docs/plans/completed/`

## Post-Completion

*Требует ручных действий или относится к будущим итерациям.*

**Что нужно сделать руками после автономного прогона** (собрано из всех `MANUAL:` пунктов выше):

1. `fb-rent scrape --refresh` — перезалить 21 существующую строку с исправленными экстракторами. Без этого шага анализировать нечего: у 10 строк нет описания вовсе, у большинства остальных туда попал сайдбар.
2. Проверить качество данных в sqlite3: сайдбарного boilerplate (1288 / 962 символа) быть не должно, `title` не должен быть `"Недавно опубликовано"`, `published_at` не должен быть NULL везде.
3. Запустить `/analyze` в интерактивной сессии Claude Code (Opus, high thinking effort) сначала на 3–5 листингах — убедиться, что извлечение адекватно и цитаты `ev` действительно подтверждают значения.
4. Открыть `data/rentals.html`, проверить цвета, вкладки, легенду и горизонтальный скролл.
5. Убедиться на живых данных, что правила не режут лишнего: никаких реджектов за количество комнат больше запрошенного и за неупомянутую гостиную.
6. Прогнать `/analyze` на полном объёме, ответить на вопросы про новые поля, если AI их предложит.

**Известные ограничения этой итерации** (fix в следующих):

- Grid.js грузится с unpkg CDN — HTML требует интернета для рендера.
- `--refresh` перекачивает detail-страницы, это медленно (3–7 сек на листинг, cap 60 за прогон) — на больших объёмах придётся гонять в несколько заходов.
- Анализ не автоматизирован end-to-end: это интерактивный цикл в сессии Claude Code, а не cron-задача. Автономный прогон невозможен, пока в цикле есть `AskUserQuestion`.
- `score` — эвристика по критериям из `requirements.md`, а не откалиброванная модель; служит для сортировки, не для решений.

**Следующие итерации** (не в скоупе, для контекста):

1. Парсинг FB-групп аренды Пхукета — второй источник в ту же БД.
2. Планировщик Hermes: расписание + diff-нотификации о новых листингах, с прогоном анализа только по свежим.
3. Неинтерактивный режим `/analyze` (новые поля копятся в очередь предложений вместо вопроса на месте) — предпосылка для автоматизации по расписанию.
4. Вендоринг Grid.js для офлайн-просмотра HTML.
5. Мониторинг протухания сессии FB + автоматический retry с алертом.
