# Async support (asyncio, PostgreSQL)

Экспериментальная асинхронная поддержка: `asyncio` + PostgreSQL через psycopg3
(`psycopg` + `psycopg_pool`). Синхронный режим полностью сохраняется.

## Подключение

```python
from pony.orm import Database

db = Database("postgres_async", dsn="dbname=mydb user=...")
# "postgres_async" даёт и sync-сессии (как обычный "postgres"), и async-сессии.
```

MariaDB — аналогично, коннектор `mariadb` 2.0RC (`pip install --pre mariadb[pool]`):

```python
db = Database("mariadb", user="...", password="...", host="...", database="...")        # sync
adb = Database("mariadb_async", user="...", password="...", host="...", database="...")  # sync + async
```

## Сессии и запросы

```python
from pony.orm import db_session, select

async with db_session:
    objs = await select(x for x in Person).order_by(Person.id)   # список

async with db_session:
    async for obj in select(x for x in Person if x.age > 18):
        ...

async with db_session:
    p = Person(name="Ann")      # создание и запись — без I/O
    p.age = 30
# выход из сессии: flush + commit
```

- Вход в **синхронный** `with db_session:` внутри корутины — ошибка
  (`TransactionError`): он заблокировал бы event loop. Sync-код в потоках
  (`run_in_executor`) работает как раньше.
- Одна async-сессия = одна задача; две задачи одновременно — две независимые
  сессии (соединения из общего пула, identity map изолирован).

## Явная загрузка

Неявной ленивой загрузки в async-режиме нет. Незагруженный атрибут или
коллекция бросает `NotLoadedError`; загруженное читается обычным образом
(включая `for` / `[0]` / `len()` / `in` для коллекций):

```python
async with db_session:
    d = (await select(x for x in Dept))[0]
    await d.persons                 # загрузка коллекции
    for p in d.persons:             # sync-итерация уже загруженного
        ...

    await p.load("dept")           # загрузка атрибута (в т.ч. lazy и seed-объектов)
    await p.load()                 # загрузка всех незагруженных атрибутов
    async for rel in d.persons:     # async-итерация с загрузкой
        ...
```

## Транзакции

`await commit()` / `await rollback()` / `await flush()` — async-аналоги
глобальных функций (внутри async-сессии). Флаги `db_session` работают как
в sync (`immediate`, `serializable`, `optimistic`, `allowed_exceptions`).

## Ограничения (текущее состояние)

- PostgreSQL (psycopg3) и MariaDB (коннектор `mariadb` 2.0RC); остальные диалекты — sync.
- `prefetch()` в async-режиме — `NotImplementedError` (используйте явный `load()`).
- `await Entity[pk]` не реализован: `Entity[pk]` — синхронная операция,
  в async-сессии запрещена; выборка по pk — через `select(...)`.
- `load()` для reverse-атрибутов без собственных колонок — `NotImplementedError`.
- Schema-операции (`generate_mapping`, `create_tables`) — только sync, оборачивать
  в `with io:`.
- Смешение sync- и async-сессий в одной транзакции не поддерживается.
