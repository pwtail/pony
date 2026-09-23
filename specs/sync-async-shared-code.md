---
id: pony-async-shared-code
type: task-spec
title: Единый код для sync- и async-режимов (Gen-классы)
status: done
parent: pony-async-architecture
depends-on: [pony-async-architecture]
tags: [async, sync, refactoring]
---

# Цель

Устранить дублирование: исходно sync-путь (`SessionCache`, `Database._exec_sql`,
цепочка сохранения, выборки в core.py) и async-путь (`AsyncSessionCache`,
async_core.py) — две копии одной логики. После пункта 1-а логика существует
в **одном экземпляре** (Gen-классы, написанные в async-стиле), а sync- и
async-режимы — это две тонкие обёртки-драйвера.

Демо механизма: `~/PycharmProjects/JupyterProject1/asyn.ipynb` (идея
шагаемой корутины) и `drive.ipynb` (итоговая структура: состояние в кэше,
`SessionCacheGen(self)`, `drive` / `Delegate`).

# Механизм

- **Состояние — в кэше сессии.** `AbstractSessionCache` держит state (identity
  map, соединение, транзакция, индексы, очереди сохранения) и sync-хелперы,
  которые зовёт core: `flush_disabled`, `call_after_save_hooks`,
  `update_simple_index`, `db_update_simple_index`, `update_composite_index`,
  `db_update_composite_index`. Режим и session-методы определяют наследники:
  `SyncSessionCache` (is_async = False) и `AsyncSessionCache` (is_async = True);
  имени `SessionCache` в коде нет.
- **Логика — в Gen-классе.** `SessionCacheGen(cache)` получает обратную
  ссылку и читает состояние через `self.cache`; методы написаны в async
  стиле. Gen-методы из `core_gen` вызываются как `cache._gen.<метод>()`
  (вызовы Gen→Gen внутри Gen — обычные `await self.<метод>()`).
- **Sync-драйвер**: однострочная обёртка в `SyncSessionCache` —
  `def connect(self): return drive(self._gen.connect())` (девять
  session-методов), где `drive` — шагатель корутины:

```python
def drive(coro):            # pony/orm/drive.py
    try:
        next(coro.__await__())
    except StopIteration as ex:
        return ex.value
```

  Работает потому, что внутри Gen-кода **все await'ы — только на адаптерные
  I/O-операции**, которые в sync-режиме завершаются синхронно (ниже).
- **Async-драйвер — `Delegate`**: `connect = Delegate('_gen')` отдаёт
  корутину Gen вызывающему (`await cache.connect()`). `AsyncSessionCache`
  наследует `AbstractSessionCache` (то же состояние и хелперы), переопределяя
  только режим (`is_async = True`) и эти девять методов.
- `drive()` и `Delegate` живут в `pony/orm/drive.py`; sync-обёртки —
  явные методы `SyncSessionCache` (никаких дескрипторов для sync-пути).

  Режим (`is_async`) — единственный источник выбора адаптера: `Gen._ops()`
  возвращает `provider.sync_ops` / `provider.async_ops` по флагу кэша.

# Адаптер I/O-операций (ProviderOps)

Единственное место, где режимы различаются физически:

- Интерфейс: `connect`, `set_transaction_mode`, `execute`, `commit`,
  `rollback`, `release`, `drop` — каждая операция возвращает awaitable.
- **Sync-реализация**: `async def execute(...): return sync_execute(...)` —
  корутина без реальных await-точек: при первом `next()` тело выполняется
  целиком (вызов sync-psycopg), StopIteration — сразу. Идёт через те же
  sync-соединения и `PoolConnectionWrapper` (io-guard продолжает работать).
- **Async-реализация**: настоящие `psycopg` async-вызовы (текущий
  `AsyncPostgreSQLProvider.async_*`).

Диалектный код (SQLBuilder, конвертеры, schema) уже общий — не меняется.

# Что покрывает пункт

1. `SessionCacheGen` ← логика текущего `AsyncSessionCache` (идентичные
   методы и логика, как после рефакторинга session_cache/async_session_cache).
   Состояние при этом остаётся в `AbstractSessionCache` (см. механизм выше).
2. `exec_sql_gen` ← текущий `async_exec_sql`; sync `Database._exec_sql`
   становится `return drive(exec_sql_gen(...))`.
3. `save*_gen` ← текущая async-цепочка `async_save_*`; sync `_save_*` в core.py
   делегируют через `drive()`.
4. `query_fetch_gen` / `fetch_objects_gen` / `load_*_gen` ←
   `async_query_fetch`, `async_fetch_objects`, `async_load_*`; sync-выборки
   делегируют через `drive()`.
5. Глобалы: sync `flush/commit/rollback` и async-глобалы — один Gen-код,
   два драйвера. (`with io:` / `io_bypass` из процесса удалены — Gen-код
   зовёт I/O напрямую через adapter.)

Результат: `async_core.py` и дублирующие sync-куски в core.py сжимаются;
остаётся Gen-ядро + два драйвера.

# Инварианты

- **I1. Поведение не меняется.** Существующие sync- и async-сьюты проходят
  без правок — это критерий готовности (рефакторинг организации кода, не
  семантики).
- **I2. Sync-режим не требует event loop.** `_drive` работает в любом потоке;
  внутри Gen-кода запрещены реальные await'ы (sleep, async-сокеты) — только
  адаптерные операции. Нарушение = ошибка на первом `next()`.
- **I3. В async-режиме Gen-код не блокирует loop** (структурно: адаптер
  async-режима — только настоящие async-вызовы).
- **I4. io-guard и sync-entry guard сохраняют поведение.**

# Риски и открытые вопросы

- **R1. Производительность sync-пути (ЗАМЕР, решение за пользователем).**
  sqlite in-memory, 2 прогона: update 1000 объектов — 0.065s (до изменений) vs
  0.104s (Gen-драйвер, +60%); select 200×1000 — 0.019 vs 0.025 (+32%);
  insert 1000 — 0.017 vs 0.034 (+100%). Оверхед 1.3–2x на микрооперациях
  (микросекунды на операцию) включает не только шагание корутин, но и
  contextvars и прочие изменения форка.
  **PostgreSQL + psycopg3** (2 прогона): update 1000 — 0.58/0.71s vs 0.64/0.65s;
  select 200×1000 — 0.05/0.06s vs 0.06/0.04s; insert 1000 — 0.24/0.22s vs
  0.28/0.25s — оверхед Gen-драйвера **в пределах шума** (время доминирует
  сетевая работа БД). **Решено: оставляем Gen** — единый код остаётся,
  нативный sync-движок в форке не восстанавливается. Код замера:
  `benchmarks/bench_sync.py` (выбор СУБД опцией `--db sqlite|postgres`, движка —
  `--engine gen|native|auto`; native сравнивает с деревом апстрима через
  subprocess, DSN через PONY_BENCH_DSN).
- **R2. Трейсбеки (проверено).** Исключение из шагаемой корутины приходит с кадром
  пользователя и коротким стеком pony: проба с нарушением уникальности в sync-сессии
  даёт `TransactionIntegrityError`, в трейсбеке остаётся строка пользователя, кадров
  `pony/orm/core.py` — 2. `cut_traceback` не мешает.
- **R3. `asyncio.current_task()` (проверено).** Он используется ровно в двух местах:
  входной guard синхронной сессии (`core.py`) и ключ `(поток, задача)` в
  `ContextLocal` (`pony/utils/utils.py`); Gen-код на это различие не полагается.
  Шагаемая корутина задачи не создаёт (в синхронном режиме loop активен только у
  пользовательской корутины, что и отсекает входной guard), изоляция состояния по
  задачам покрыта тестами (`test_task_interleaving` и проверка изоляции кэшей).
- **Q1. Именование**: суффикс `Gen`/`_gen` у общего слоя — классы
  `SessionCacheGen`, функции `exec_sql_gen`, `query_fetch_gen`, `save*_gen`,
  `load_*_gen`, `fetch_objects_gen`; префикс `async_` остаётся только у
  по-настоящему async-only глобалов (`async_flush/async_commit/async_rollback`).
- **Q2. Глубина миграции (закрыт).** m2m-синхронизация и `prefetch` мигрированы:
  `Attribute.add_m2m/remove_m2m` разделены на построение SQL и исполнение
  (`add_m2m_gen`/`remove_m2m_gen`), prefetch получил async-загрузчики
  (`prefetch_gen`, `prefetch_load_all_gen`, `prefetch_load_all_objects_gen`), а разбор
  строк m2m вынесен в общий хелпер `_prefetch_load_all_m2m_rows`, так что sync и async
  идут одним кодом. Проверено на живых Postgres/MariaDB и тестами
  `test_async_api.py`/`test_mariadb_async.py`.

# Этапы

1. Драйвер `_drive` + `ProviderOps` (sync/async) — общие для всех Gen-классов.
2. Миграция сессии: `SessionCacheGen`; `SyncSessionCache`/`AsyncSessionCache` —
   режимные классы над общей частью `AbstractSessionCache`. Sync-сьют зелёный.
3. Миграция exec + save-цепочки на Gen. Sync-сьют зелёный.
4. Миграция выборок/загрузок (query fetch, fetch_objects, load) на Gen.
   Sync + async сьюты зелёные без правок.
5. Зачистка дубликатов + бенчмарк sync-пути (R1).

# Готовность

Sync-сьюты (sqlite / Postgres / MariaDB / MySQL) и async-сьюты (Postgres /
MariaDB) проходят без правок семантики; замер R1 выполнен и зафиксирован
в спеке.

Проверено после перехода на структуру из `drive.ipynb`: целевые тесты
(`test_ops`, `test_async_api`, `test_mariadb_sync`, `test_mariadb_async`),
смоук sync на трёх СУБД (identity map, unique/composite-индексы, lazy, m2m,
rollback, `strict`, `flush_disabled`, delete) и смоук async на Postgres /
MariaDB (select, lazy + `NotLoadedError`, коллекция, flush на выходе,
rollback, изоляция кэшей параллельных задач).
