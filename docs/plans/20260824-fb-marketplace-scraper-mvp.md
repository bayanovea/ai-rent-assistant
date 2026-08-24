# FB Marketplace Phuket Rentals Scraper — MVP (iteration 1)

## Overview

Первая итерация системы для поиска долгосрочного жилья на Пхукете через FB Marketplace.

Задача: скрипт, который вручную запускается, парсит свежие листинги из [Phuket Property Rentals](https://www.facebook.com/marketplace/110729412288885/propertyrentals) в диапазоне 15 000–30 000 THB, и складывает их в локальную SQLite-базу с автоматическим CSV-экспортом. База становится «единой точкой синхронизации» для всех будущих итераций: планировщик Hermes, парсинг групп, AI-обогащение, web-артефакт для просмотра.

Первая итерация умышленно узкая — только один источник, ручной запуск, никакого AI. Цель — получить рабочий pipeline `URL → data → таблица`, на который дальше нарастим автоматизацию и анализ.

## Context (from discovery)

- **Проект**: пустая директория `~/AI/fb-rent`, только `.claude/`. Стартуем с нуля.
- **Целевой URL**: `https://www.facebook.com/marketplace/110729412288885/propertyrentals` — ID 110729412288885 закрепляет локацию за Пхукетом.
- **FB URL-параметры** (проверены на marketplace): `minPrice`, `maxPrice`, `sortBy=creation_time_descend`, `daysSinceListed=1|7|30`, `exact=false`. Используем их, чтобы не бороться с пагинацией — свежее всегда сверху.
- **Известные ограничения FB**: агрессивный антибот, публичного API для marketplace нет, listing detail страница требует авторизации.
- **Будущие итерации** (не в скоупе, но влияют на решения): планировщик Hermes, парсинг FB-групп, AI-скоринг листингов, web-viewer.

## Development Approach

- **testing approach**: Regular (код сначала, тесты после). Только unit-тесты на чистые функции — экстракторы, нормализация, БД-слой. E2E на живом FB не пишем (хрупко).
- complete each task fully before moving to the next
- make small, focused changes
- **CRITICAL: every task MUST include new/updated tests** для чистых функций (парсинг, БД, URL-builder). Для Playwright-кода тесты пропускаем осознанно — там мы тестируем руками через `--dry-run` и визуальный осмотр CSV.
- **CRITICAL: all tests must pass before starting next task** — no exceptions
- **CRITICAL: update this plan file when scope changes during implementation**
- run tests after each change

## Autonomous Execution Notes (for ralphex / agent runs)

Часть шагов требует живого залогиненного Facebook и **не может быть выполнена автономным агентом**. Конвенция:

- Чекбоксы с префиксом `MANUAL:` — агент **пропускает и помечает** `[x] MANUAL: ... (skipped — requires live FB / human action)`. Не пытается выполнить, не блокирует прогресс.
- Чекбоксы с префиксом `DESIGN:` — это фиксация проектного решения (например, «не пишем unit-тесты для Playwright-кода осознанно»). Агент **помечает** `[x] DESIGN: ...` без действий.
- Всё остальное — автоматизируется как обычно: код + unit-тесты + `pytest`.
- Список селекторов FB в Task 6/7 — best-effort guess; агент пишет по описанию, реальная проверка на живых данных остаётся человеку.

Все `MANUAL:` пункты также перечислены в разделе Post-Completion — оттуда человек забирает список того, что нужно сделать руками после автономного прогона.

## Testing Strategy

- **unit tests**: обязательны для чистых функций — `parse_price`, `extract_bedrooms`, `extract_property_type`, `build_marketplace_url`, БД insert/upsert/export.
- **integration tests**: НЕ пишем для Playwright-скраперов (сеть + живой FB = хрупко). Валидация через ручной прогон.
- **e2e**: не применимо (нет UI на этой итерации).
- **manual verification**: после Task 7 — ручной запуск `fb-rent scrape` и осмотр `rentals.csv` в Numbers.

## Progress Tracking

- mark completed items with `[x]` immediately when done
- add newly discovered tasks with ➕ prefix
- document issues/blockers with ⚠️ prefix
- update plan if implementation deviates from original scope
- keep plan in sync with actual work done

## Solution Overview

Архитектура — тонкий CLI на Python поверх Playwright с трёхслойной схемой:

```
┌─────────────────────────────────────────┐
│  CLI (click)                            │
│    fb-rent login    ─┐                  │
│    fb-rent scrape   ─┼─→ orchestrator   │
│    fb-rent export   ─┘                  │
└──────────────┬──────────────────────────┘
               │
       ┌───────┴────────┐
       ▼                ▼
┌────────────┐    ┌─────────────┐
│  scraper   │    │   storage   │
│ (playwright)│──→│ (SQLite)    │──→ rentals.csv
│  list      │    │  listings   │
│  detail    │    │  upsert     │
└────────────┘    └─────────────┘
       │                ▲
       ▼                │
┌────────────┐          │
│ extractors │──────────┘
│ (regex)    │
│ bedrooms   │
│ type       │
│ price      │
└────────────┘
```

Ключевые решения:

1. **Playwright + persistent context**. Один раз логинишься руками через `fb-rent login`, cookies/localStorage сохраняются в `~/.fb-rent/browser-profile/`. Дальше скрапер стартует headless и переиспользует сессию. Cookies FB живут 1–4 недели — при 401/redirect на логин показываем понятную ошибку с инструкцией.
2. **URL-параметры вместо пагинации**. `sortBy=creation_time_descend` + `minPrice=15000&maxPrice=30000` + `daysSinceListed=7`. Скроллим список до ~N карточек (по умолчанию 40), не гоняемся за всеми страницами. При регулярных запусках новых карточек < 10 — всё умещается в первую «страницу».
3. **SQLite как source of truth**, CSV — плоский экспорт для просмотра. AI-агенту дальше работать через `sqlite3 rentals.db "SELECT..."`, а глазами смотреть `rentals.csv` в Numbers/Excel.
4. **Дедупликация по FB listing ID** (URL-slug типа `/marketplace/item/1234567890`). Upsert по этому ключу: если ID уже есть — обновляем `last_seen_at`, `price` (может меняться) и `description`; если нет — insert с `first_seen_at`.
5. **Rate limiting**: между запросами detail-страниц пауза 3–7 секунд (jitter), между сессиями скрапа — на усмотрение пользователя (запускает руками). Общий cap: не больше 60 detail-запросов за прогон.
6. **Heuristic extractors** для `bedrooms` и `property_type` — regex/keyword на заголовке+описании. AI-обогащение — будущая итерация (в этой не делаем осознанно).

## Technical Details

### Схема БД (SQLite)

```sql
CREATE TABLE listings (
  fb_id           TEXT PRIMARY KEY,       -- ID из URL /marketplace/item/{id}
  url             TEXT NOT NULL,
  title           TEXT NOT NULL,
  price_thb       INTEGER,                -- нормализовано в THB, NULL если не распарсили
  price_raw       TEXT,                   -- оригинальная строка с сайта
  district        TEXT,                   -- сырая строка локации из карточки
  bedrooms        INTEGER,                -- из extract_bedrooms, NULL если не нашли
  property_type   TEXT,                   -- villa/condo/apartment/house/studio/unknown
  description     TEXT,                   -- полное описание (может быть длинным)
  published_at    TEXT,                   -- ISO дата публикации (что даёт FB, например "3 days ago" → нормализуем)
  first_seen_at   TEXT NOT NULL,          -- ISO datetime первого парсинга
  last_seen_at    TEXT NOT NULL,          -- ISO datetime последнего парсинга
  raw_json        TEXT                    -- сырой dict с детальной страницы для дебага и будущего AI
);

CREATE INDEX idx_last_seen ON listings(last_seen_at DESC);
CREATE INDEX idx_price ON listings(price_thb);
```

### URL-builder

```python
def build_marketplace_url(
    category_id: str = "110729412288885",  # Phuket Property Rentals
    min_price: int = 15000,
    max_price: int = 30000,
    days_since_listed: int = 7,
) -> str:
    ...
# → https://www.facebook.com/marketplace/{category_id}/propertyrentals?minPrice=15000&maxPrice=30000&daysSinceListed=7&sortBy=creation_time_descend&exact=false
```

### CLI команды

- `fb-rent login` — открывает headed браузер, ждёт когда пользователь залогинится, сохраняет сессию, выходит.
- `fb-rent scrape [--max 40] [--days 7] [--dry-run]` — основная команда, парсит список + детали, пишет в БД, регенерирует CSV.
- `fb-rent export [--path rentals.csv]` — только регенерация CSV из БД без парсинга (полезно для проверок).

### Структура проекта

```
fb-rent/
├── pyproject.toml
├── README.md
├── .gitignore
├── src/fb_rent/
│   ├── __init__.py
│   ├── cli.py              # click CLI
│   ├── config.py           # константы, пути, defaults
│   ├── storage.py          # SQLite слой
│   ├── extractors.py       # чистые парсеры (price, bedrooms, type)
│   ├── url_builder.py      # build_marketplace_url
│   ├── browser.py          # Playwright session helpers
│   ├── scraper_list.py     # скрапинг списка
│   └── scraper_detail.py   # скрапинг detail страницы
├── tests/
│   ├── test_extractors.py
│   ├── test_storage.py
│   └── test_url_builder.py
└── docs/
    └── plans/
```

## What Goes Where

- **Implementation Steps** (`[ ]` checkboxes): весь код скрапера, БД, CLI, тесты — то что делаем в этом репо на этой итерации.
- **Post-Completion** (no checkboxes): ручной FB-логин, отладка селекторов на живом сайте, планы на итерацию 2.

## Implementation Steps

### Task 1: Project skeleton + dependencies

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `src/fb_rent/__init__.py`
- Create: `src/fb_rent/config.py`
- Create: `README.md` (минимальный)

- [x] создать `pyproject.toml` с зависимостями: `playwright`, `click`, `python-dateutil`; dev: `pytest`
- [x] создать `src/fb_rent/config.py` с константами: `DB_PATH`, `CSV_PATH`, `BROWSER_PROFILE_DIR` (все под `~/.fb-rent/` для локального стора, с override через env `FB_RENT_HOME`)
- [x] создать `.gitignore` (`.venv/`, `__pycache__/`, `*.db`, `*.csv`, `~/.fb-rent/`, `.pytest_cache/`)
- [x] создать минимальный `README.md` с командой установки (`pip install -e . && playwright install chromium`)
- [x] прогнать `python -c "import fb_rent.config; print(fb_rent.config.DB_PATH)"` — должно работать

### Task 2: URL builder

**Files:**
- Create: `src/fb_rent/url_builder.py`
- Create: `tests/test_url_builder.py`

- [x] реализовать `build_marketplace_url(category_id, min_price, max_price, days_since_listed) -> str` в `src/fb_rent/url_builder.py` с дефолтами из config
- [x] URL-параметры: `minPrice`, `maxPrice`, `daysSinceListed`, `sortBy=creation_time_descend`, `exact=false`
- [x] написать `tests/test_url_builder.py`: дефолтный URL для Пхукета 15k-30k, кастомные диапазоны, крайние значения
- [x] тест на неверные аргументы (min > max — должно бросить `ValueError`)
- [x] `pytest tests/test_url_builder.py -v` — все зелёные

### Task 3: Text extractors (price, bedrooms, property type)

**Files:**
- Create: `src/fb_rent/extractors.py`
- Create: `tests/test_extractors.py`

- [x] `parse_price(raw: str) -> int | None` — парсит строки типа `"THB25,000"`, `"฿15 000/month"`, `"20K THB"`, возвращает целое в THB или None
- [x] `extract_bedrooms(title: str, description: str) -> int | None` — regex по `2BR`, `2 bedrooms`, `studio` (=0), `1BR` и т.д.
- [x] `extract_property_type(title: str, description: str) -> str` — keyword match: `villa|condo|apartment|house|studio|townhouse`, дефолт `"unknown"`
- [x] написать `tests/test_extractors.py`: table-driven по 8-10 реальных примеров из FB (заранее скопируй руками пару листингов для fixtures)
- [x] edge cases: пустая строка, только цифры без валюты, "PRICE ON REQUEST", очень большие суммы, unicode-символы валют
- [x] `pytest tests/test_extractors.py -v` — все зелёные

### Task 4: SQLite storage layer

**Files:**
- Create: `src/fb_rent/storage.py`
- Create: `tests/test_storage.py`

- [ ] `init_db(path: Path)` — создаёт таблицу и индексы из схемы выше, идемпотентно
- [ ] `upsert_listing(conn, listing: dict) -> str` — insert или update по `fb_id`; возвращает `"inserted"` / `"updated"`; при update: обновляет `last_seen_at`, `price_thb`, `description`, `raw_json`, сохраняет `first_seen_at`
- [ ] `export_csv(conn, path: Path)` — регенерирует CSV из БД, отсортировано по `last_seen_at DESC`; колонки в порядке: `price_thb`, `title`, `bedrooms`, `district`, `property_type`, `published_at`, `url`, `description`, `first_seen_at`, `last_seen_at`
- [ ] написать `tests/test_storage.py` с in-memory SQLite (`":memory:"`): init создаёт таблицу, insert нового, upsert существующего сохраняет `first_seen_at`, export возвращает правильный порядок колонок
- [ ] тесты на edge cases: NULL price, юникод в описании, дубликаты
- [ ] `pytest tests/test_storage.py -v` — все зелёные

### Task 5: Browser/session helper (Playwright)

**Files:**
- Create: `src/fb_rent/browser.py`

- [ ] `launch_persistent(headless: bool) -> BrowserContext` — использует `chromium.launch_persistent_context(user_data_dir=BROWSER_PROFILE_DIR)`, реалистичный `user_agent` (последний stable Chrome на macOS), `viewport 1280x900`, `locale='en-US'`
- [ ] `is_logged_in(context) -> bool` — открывает `https://www.facebook.com/`, проверяет наличие селектора профиля / отсутствие форм логина
- [ ] `login_flow(context)` — открывает `facebook.com/login` в headed режиме, ждёт event ручного логина (polling `is_logged_in` каждые 3 сек до 5 минут), выходит
- [ ] DESIGN: тесты для этого модуля НЕ пишем осознанно — интеграция с живым браузером; проверяем руками через CLI в Task 8
- [ ] MANUAL: `fb-rent login` открывает браузер, залогиниться руками, вторая команда работает headless

### Task 6: List scraper

**Files:**
- Create: `src/fb_rent/scraper_list.py`

- [ ] `scrape_list(context, url: str, max_items: int) -> list[dict]` — открывает `url`, ждёт загрузки карточек, скроллит вниз пока не наберёт `max_items` уникальных или не перестанут появляться новые (2 попытки без прироста → стоп)
- [ ] для каждой карточки на списке извлекает: `fb_id` (из ссылки `/marketplace/item/{id}`), `url`, `title`, `price_raw`, `district` (обычно вторая строка на карточке), `cover_image_url`
- [ ] селекторы: используем `role`/`aria-label` где можно (FB меняет CSS-классы часто), fallback на семантические `a[href*="/marketplace/item/"]`
- [ ] при 0 карточках или detected login-redirect — бросает `LoginRequiredError` с понятным сообщением
- [ ] DESIGN: осознанно без unit-тестов (нужен живой FB); проверим руками в Task 9
- [ ] защита от бесконечного скролла: hard limit `max_items * 2` попыток скролла
- [ ] MANUAL: подтвердить работу селекторов на живом FB (best-effort guess в коде — почти гарантированно потребуется итерация)

### Task 7: Detail scraper

**Files:**
- Create: `src/fb_rent/scraper_detail.py`

- [ ] `scrape_detail(context, url: str) -> dict` — открывает URL листинга, ждёт загрузки, кликает «See more» на описании если есть, извлекает: `description` (полное), `published_at` (сырой текст FB типа "Listed 3 days ago" — сохраняем как есть в первой итерации), `price_raw` (может отличаться от списка), `raw_json` (всё что смогли достать в dict)
- [ ] rate limit: `random.uniform(3, 7)` сек перед каждым вызовом (не внутри — контролирует orchestrator)
- [ ] обработка 404 / удалённого листинга: возвращает `None`, не крашится
- [ ] DESIGN: осознанно без unit-тестов; ручная проверка в Task 9
- [ ] MANUAL: подтвердить работу селекторов на живой карточке FB (best-effort guess в коде)

### Task 8: CLI orchestration

**Files:**
- Create: `src/fb_rent/cli.py`
- Modify: `pyproject.toml` (добавить entry-point `fb-rent = fb_rent.cli:main`)

- [ ] `fb-rent login` — вызывает `browser.login_flow`, печатает статус
- [ ] `fb-rent scrape [--max 40] [--days 7] [--dry-run] [--skip-details]` — orchestrator:
  - открывает persistent context (headless)
  - проверяет `is_logged_in`, при False — понятная ошибка «run `fb-rent login`»
  - строит URL через `url_builder`
  - `scrape_list` → для каждого listing (лимит на прогон 60): `scrape_detail` с задержкой, extract полей, upsert в БД
  - `--dry-run`: печатает что бы сделал, не пишет БД
  - `--skip-details`: только список без захода на карточки (быстрая инкрементальная проверка)
  - в конце: `export_csv`, печатает summary «inserted: X, updated: Y, csv: /path/to/rentals.csv»
- [ ] `fb-rent export [--path]` — только регенерация CSV
- [ ] `fb-rent --help` печатает все команды с описанием
- [ ] DESIGN: тесты на CLI НЕ пишем (тонкая обёртка); логика уже покрыта юнит-тестами нижних слоёв
- [ ] прогнать `pip install -e .` и `fb-rent --help` — команды видны

### Task 9: Verify acceptance criteria

**Автоматизируемое** (агент выполняет):
- [ ] `pytest tests/ -v` — все юнит-тесты зелёные
- [ ] `fb-rent --help` — команды видны, `fb-rent scrape --help` показывает все флаги
- [ ] `fb-rent scrape --max 5 --dry-run` без залогиненной сессии — печатает понятную ошибку «run `fb-rent login`», не крашится stacktrace'ом
- [ ] `fb-rent export --path /tmp/empty.csv` на пустой БД — создаёт CSV с заголовком, без строк, exit code 0

**Требует ручных действий** (агент помечает как skipped):
- [ ] MANUAL: `fb-rent login` — залогиниться руками, увидеть success сообщение
- [ ] MANUAL: `fb-rent scrape --max 5 --dry-run` — печатает список из 5 листингов без записи
- [ ] MANUAL: `fb-rent scrape --max 10` — реальный запуск, в БД ~10 записей, CSV сгенерирован
- [ ] MANUAL: открыть `rentals.csv` в Numbers/Excel — колонки читаемы, поля заполнены (NULL допустим для heuristic-полей)
- [ ] MANUAL: цены всех записей в диапазоне 15000-30000 THB (или NULL если не распарсили)
- [ ] MANUAL: повторный `fb-rent scrape --max 10` — `updated > 0`, `inserted` может быть 0, дубликатов нет
- [ ] MANUAL: `fb-rent scrape --max 60` — прогон на большем объёме, замерить время; не должно быть банов/капчи

### Task 10: [Final] Update documentation

**Files:**
- Modify: `README.md`

- [ ] в `README.md` добавить: установку (venv, pip install, playwright install chromium), quickstart (login → scrape → csv), описание всех CLI команд с примерами, где живёт БД (`~/.fb-rent/rentals.db`), troubleshooting (session expired → re-login)
- [ ] отметить в README что это iteration 1 и линкануть на этот plan файл для контекста будущих итераций
- [ ] переместить этот plan файл: `mkdir -p docs/plans/completed && mv docs/plans/20260824-fb-marketplace-scraper-mvp.md docs/plans/completed/`

## Post-Completion

*Требует ручных действий или относится к будущим итерациям.*

**Что нужно сделать руками после автономного прогона** (собрано из всех `MANUAL:` пунктов выше):
1. `fb-rent login` — залогиниться в открывшемся браузере (2FA, если включена, вводится тоже там); подтвердить что headless-режим потом видит сессию
2. `fb-rent scrape --max 5 --dry-run` — smoke-check, что список карточек парсится
3. `fb-rent scrape --max 10` — реальный прогон, проверить что БД и CSV заполнились
4. Открыть `rentals.csv` в Numbers/Excel — визуально проверить цены (15000-30000 THB), поля, локации (Rawai/Chalong/Kata/Patong/…)
5. Повторный `fb-rent scrape --max 10` — убедиться в дедупликации (updated > 0)
6. `fb-rent scrape --max 60` — прогон на большем объёме, замерить время для планирования Hermes-интервала
7. Если селекторы в `scraper_list.py` / `scraper_detail.py` не сработали — итерация на живых данных (ожидаемая часть работы, не «баг»)

**Известные ограничения этой итерации** (fix в следующих):
- `published_at` хранится как сырая строка FB типа "Listed 3 days ago" — парсинг в ISO дату отложен
- `bedrooms` / `property_type` — heuristic, промахи возможны; AI-обогащение планируется отдельной итерацией
- нет учёта личных требований к жилью — просто сбор в базу, фильтрация глазами по CSV; scoring/фильтры — следующая итерация
- нет уведомлений о новых листингах; появятся с Hermes-планировщиком

**Следующие итерации** (не в скоупе, для контекста):
1. AI-обогащение (bedrooms/type/качество/scoring по личным требованиям) через Claude API
2. Парсинг FB-групп аренды Пхукета
3. Планировщик Hermes: расписание + diff-нотификации о новых листингах
4. Web-viewer (простой Flask/FastAPI или статический HTML из CSV) с карточками, обрезанным описанием, фильтрами
5. Мониторинг протухания сессии + автоматический retry с алертом
