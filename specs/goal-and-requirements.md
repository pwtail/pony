---
id: pony-async-goal-and-requirements
type: goal-and-requirements
title: Асинхронная поддержка pony ORM (PostgreSQL / psycopg3)
status: active
tags: [async, postgresql, psycopg3]
references: [pony-session-scope]
---

# Цель

Добавить в форк pony ORM нативный asyncio-режим: `async with db_session`, `await select(...)`,
`async for`, явную загрузку связанных объектов — без регрессий существующего синхронного режима.

# Область

- **Только PostgreSQL**, драйвер **psycopg3** (пакет `psycopg`): sync — существующий путь через
  dbapi-совместимый интерфейс psycopg (уже на ветке `pony-psycopg3`); async — через
  `psycopg.AsyncConnection` / `psycopg.AsyncCursor`.
- Остальные диалекты (MySQL, SQLite, Oracle, CockroachDB): async **вне области**; их sync-работа
  не меняется.
- **Sync-режим остаётся** и остаётся поведенчески неизменным.

# Принятые решения (не пересматриваются без явного решения)

1. **Без адаптера через пул потоков** (`asyncio.to_thread` и подобное) — не поддерживается.
2. **Без greenlet** — следовательно, неявная ленивая загрузка в async-режиме невозможна
   в принципе: загрузка только явными await-операциями.
3. **Один диалектный код на оба режима**: генерация SQL (SQLBuilder/Translator), конвертеры
   типов, schema-классы — общие; async-провайдер зеркалит только операции соединения
   (connect / execute / commit / rollback / release).
4. **Async-сессия — тот же символ `db_session`**: `async with db_session:` реализуется
   `__aenter__`/`__aexit__` того же контекст-менеджера, sync-вход (`with db_session:`) не меняется.
5. **Смешение режимов — ошибка**: синхронная сессия внутри асинхронной
   (`with db_session:` внутри `async with db_session:` / `@db_session`-корутины) бросает
   `TransactionError` с подсказкой «используйте `async with`». Критерий — **открытая
   async-сессия** (`local.async_db_context`), а не «мы внутри корутины»: вне async-сессии
   синхронный код разрешён и в корутине — это осознанный блокирующий вызов (поэтому
   sync-ячейки Jupyter, исполняемые внутри задачи ядра, работают как раньше).
6. **`with db:` / `async with db:` / `db.session` — per-database скоуп.**
   `Database` получает собственный скоуп сессии, привязанный к этой базе: на выходе
   коммитится/откатывается только её кэш, обращение к другой базе внутри —
   `TransactionError`. Глобальный `db_session` не меняется. Контракт, вложенность
   и guard'ы — в `pony-session-scope`. Имя `session` резервируется —
   `db.app('session')` даёт `MappingError`.

# Требования

## Функциональные (async-режим)

- **F1. Сессия.** `async with db_session:` — транзакция на async-соединении. Флаги
  `retry / immediate / serializable / optimistic / ddl / strict` сохраняют смысл. На выходе —
  flush + commit, при исключении — rollback. Async-функцию можно декорировать
  `@db_session`: на вызов открывается сессия, на выходе commit, при исключении rollback,
  `retry`/`retry_exceptions` поддержаны; `ddl` для корутин запрещён (schema-операции
  синхронные). Per-database скоуп (решение 6, `pony-session-scope`):
  `async with db:` / `db.session(...)` — та же сессия, но привязанная к этой базе;
  sync-аналоги — `with db:` / `with db.session(...)`.
- **F2. Запросы.** `await select(...)` / `await Entity.select(...)` возвращают список;
  `async for` — итерация; фильтры, джойны, `order_by`; агрегаты
  (`await query.count()/sum()/avg()/min()/max()/group_concat()`, модульные
  `count()/sum()/...` по генератору) и `await query.exists()/first()/get()`;
  срезы и пагинация (`await query[:10]`, `await query.limit()`, `await query.page()`);
  поиск по атрибутам `await Entity.get(...)`, `await Entity.exists(...)`, `await get(...)`;
  удаление `await delete(...)` и `await query.delete(bulk=True)`;
  m2m-мутации (`obj.related_set.add/remove`) исполняются на flush.
  Доступ по ключу — `await Entity[pk]` (сначала identity map — только полностью
  загруженный объект, — затем запрос): работает и для составного ключа
  (`await OrderItem[order, product]`), и когда ключ задан «сырыми» колонками
  (`await Link[1, 2]`, где pk — related-объект с составным ключом). `prefetch(...)`
  работает в async: связи догружаются батчами, как в sync-режиме.
- **F3. Явная загрузка.** `await obj.load(*attrs)` (без аргументов — весь объект);
  `await obj.related_set` — загрузка коллекции; `async for rel in obj.related_set`;
  после загрузки коллекция поддерживает обычные `for` / `[0]` / `len()` / `in`.
  `await obj.load('attr')` работает и для обратной стороны связи «один к одному»
  (атрибут без собственных колонок): связанный объект ищется запросом по reverse-атрибуту.
  Коллекции отдают seed-объекты: атрибуты элементов догружаются `await obj.load()`
  (правило 1). Удаление объекта с коллекциями и обратными связями требует их
  предварительной загрузки (`await obj.tags`, `await obj.load('passport')` →
  `obj.delete()`; правило 2), bulk-удаление — не требует.
- **F4. Чтение без I/O.** Атрибуты и коллекции доступны только если уже загружены
  (значение в `_vals_`, не seed; коллекция с `is_fully_loaded`). Иначе — `NotLoadedError`
  с подсказкой. Загруженные коллекции поддерживают обычные `for` / `[0]` / `len()` / `in`.
- **F5. Запись без I/O.** Присваивание и создание объектов — только память (как в sync),
  flush при выходе из сессии. Присваивание reverse-атрибута требует загруженного старого
  значения — иначе невозможна синхронизация reverse-индексов.
- **F6. Identity map на сессию**, как в sync: один объект на pk, объекты не пересекают
  сессии, undo/rollback-семантика та же.
- **F7. Запрет смешения режимов.** Синхронный вход или синхронный доступ к базе внутри
  async-сессии — `TransactionError` с подсказкой (решение 5). Вне async-сессии синхронный
  код в корутине разрешён (блокирует loop осознанно).

## Нефункциональные

- **N1. Структурная неблокируемость.** Async-путь не содержит синхронных вызовов драйвера —
  достигается тем, что async-провайдер имеет только await-методы.
- **N2. Sync-сьют зелёный.** После всех этапов существующие тесты проходят без правок
  (единственное внутреннее изменение — перенос контекстного состояния на contextvars,
  прозрачный для поведения).
- **N3. Одна сессия = одна задача.** Конкурентное использование одной async-сессии двумя
  задачами — ошибка (соединения psycopg не переключаются между задачами).

# Вне области (non-goals)

- async для SQLite / Oracle / CockroachDB (PostgreSQL и MariaDB / MySQL — реализованы).
- async для schema-интроспекции и DDL (`generate_mapping`, `create_tables`): остаются sync.
  **Решение (пользователя):** оставляем за пределами — schema-операции выполняются до
  старта event loop (при импорте модуля или на старте приложения, до `asyncio.run`);
  внутри async-сессии они дают `TransactionError` как смешение режимов.
  Явный sync-путь из async-кода не открываем: блокирующий DDL в event loop не даём
  даже по флагу.

# Подтверждённые ограничения async-режима   

Что осталось за пределами async-режима (осознанно, а не «не успели»):

- schema-операции (`generate_mapping`, `create_tables`) — только sync, вне корутины;
- диалекты кроме PostgreSQL (`postgres_async`) и MariaDB / MySQL (`mariadb_async`);
- смешение sync- и async-сессий в одной транзакции не поддерживается;
- `@db_session` не поддерживает async-генераторы (обычные корутины и sync-генераторы —
  поддерживаются).

Два контрактных правила async-режима (проверены тестами):

- коллекции отдают seed-объекты — атрибуты элементов догружаются явно
  (`await item.load()`);
- удаление объекта с коллекциями/обратными связями требует их предварительной загрузки;
  bulk-удаление — не требует.

Закрыто и покрыто тестами (`pony/orm/tests/test_async_api.py`,
`pony/orm/tests/test_mariadb_async.py`): агрегаты, срезы и пагинация,
`Entity.get/exists`, `await Entity[pk]` (identity map → запрос, составной ключ,
«сырые» колонки), bulk-удаление, m2m-мутации, явные
`await flush()/commit()/rollback()` (плюс `async_flush/async_commit/async_rollback`),
`prefetch()`, `load()` обратной стороны связи «один к одному», `@db_session` на
корутинах (commit/rollback/retry), per-database скоуп `with db:` / `async with db:` /
`db.session` (решение 6, `pony-session-scope`: guard чужой базы и вложенность;
sync-тесты в `test_db_session.py`, async-тесты в `test_async_api.py`),
а также понятные `TransactionError` вместо
`None`/`TypeError` на sync-only путях.
