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

## Запросы: агрегаты, срезы, поиск, bulk-удаление

Все операции, исполняющие SQL, в async-режиме возвращают корутину:

```python
async with db_session:
    # агрегаты
    n = await select(x for x in Person).count()
    total = await select(x.age for x in Person).sum()
    avg_age = await select(x.age for x in Person).avg()
    oldest = await select(x.age for x in Person).max()
    top = await select(x for x in Person).order_by(Person.age).first()
    found = await select(x for x in Person).exists()
    n = await count(x for x in Person)          # модульный агрегат по генератору

    # срезы и пагинация
    page = await select(x for x in Person).order_by(Person.name)[:10]
    page = await select(x for x in Person).limit(10, offset=20)
    page = await select(x for x in Person).page(2, pagesize=10)

    # доступ по первичному ключу и поиск по атрибутам
    person = await Person[1]
    person = await Person.get(name="Ann")
    exists = await Person.exists(name="Ann")
    name = await get(x.name for x in Person if x.age == 30)

    # удаление
    deleted = await delete(x for x in Person if x.age < 18)          # по объектам
    deleted = await select(x for x in Person if x.age < 18).delete(bulk=True)   # одним SQL

    # m2m-мутации
    person.tags.add(tag)
    person.tags.remove(tag)
```

## Два правила при работе с коллекциями

**1. Коллекции отдают seed-объекты.** После `await obj.related_set` элементы загружены
частично (известен только их первичный ключ), поэтому их атрибуты догружаются явно:

```python
async with db_session:
    person = await select(p for p in Person).first()
    await person.cars
    for car in person.cars:
        await car.load()          # или await car.load("make")
        print(car.make)
```

**2. Удаление объекта с коллекциями требует их предварительной загрузки.** В async-режиме
`obj.delete()` не догружает коллекции сам (в sync-режиме это происходит неявно), поэтому
m2m- и cascade-коллекции нужно загрузить заранее. Bulk-удаление коллекций не требует:

```python
async with db_session:
    person = await Person[1]
    await person.tags             # коллекцию нужно загрузить
    person.delete()

async with db_session:
    await delete(p for p in Person if p.age < 18)    # bulk — без загрузки коллекций
```

## Транзакции

Транзакция коммитится при выходе из `async with db_session:` и откатывается,
если внутри сессии было исключение. Явные операции доступны и внутри сессии —
в async-режиме те же функции возвращают корутину:

```python
async with db_session:
    ...
    await flush()        # отправить изменения в базу
    await commit()       # зафиксировать транзакцию
    await rollback()     # откатить
```

Есть и явные async-имена (то же самое, для кода без двусмысленности):
`async_flush`, `async_commit`, `async_rollback`.

Флаги `db_session` работают как в sync (`immediate`, `serializable`,
`optimistic`, `allowed_exceptions`).

## Ограничения (текущее состояние)

> **Важно:** это не полная замена синхронного режима. Всё перечисленное ниже в
> async-сессии поднимает `TransactionError` с подсказкой (а не работает «наполовину»).

- PostgreSQL (psycopg3) и MariaDB / MySQL (коннектор `mariadb` 2.0RC); остальные
  диалекты — sync.
- **Синхронная форма доступа по ключу** (`Person[1]` без `await`) в async-сессии
  недоступна: `Person[1]` возвращает awaitable-объект, обращение к нему без `await`
  даёт `TransactionError` с подсказкой. Правильная форма — `await Person[1]`
  (кэш → запрос) или `await Person.get(...)`.
- Коллекции отдают seed-объекты, а удаление объекта с коллекциями требует их
  загрузки — см. «Два правила при работе с коллекциями» выше.
- `prefetch()` — `NotImplementedError` (используйте явный `load()`).
- `load()` для reverse-атрибутов без собственных колонок — `NotImplementedError`.
- Schema-операции (`generate_mapping`, `create_tables`) — только sync, вне корутины.
- Декораторы `@db_session` и `@transaction` — sync-only; в async используйте
  `async with db_session:`.
- Смешение sync- и async-сессий в одной транзакции не поддерживается.
- Синхронная операция, вызванная в async-сессии, поднимает `TransactionError`
  с подсказкой (раньше в части путей она давала невнятную ошибку или `None`).

Обзор обоих режимов и примеры — в документации (`pony-doc`: `index.rst`,
раздел «Async mode» в `firststeps.rst`).
