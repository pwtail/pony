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

- Смешивать режимы нельзя: **синхронный** `with db_session:` внутри асинхронной сессии
  (`async with db_session:` или `@db_session`-корутины) — ошибка (`TransactionError`
  с подсказкой). Вне async-сессии синхронный код работает и внутри корутины: это
  осознанный блокирующий вызов, поэтому sync-ячейки Jupyter тоже продолжают работать.
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
    await p.load("passport")       # и обратной стороны связи «один к одному»
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

    # prefetch: связи догружаются заранее, батчами
    persons = await select(p for p in Person).prefetch(Person.dept)
    for p in persons:
        p.dept.name                    # читается без await

    # доступ по первичному ключу (простой, составной и «сырые» колонки)
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

## Декоратор @db_session

Асинхронную функцию можно декорировать так же, как синхронную: на вызов открывается
сессия, на выходе коммит, при исключении — откат, `retry` и `retry_exceptions` работают.

```python
@db_session
async def add_person(name):
    Person(name=name)          # commit при выходе из функции

@db_session(retry=2, retry_exceptions=[ZeroDivisionError])
async def flaky():
    ...
```

Опция `ddl` для корутин запрещена (schema-операции синхронные), async-генераторы
декоратор не поддерживает.

## Ограничения (текущее состояние)

> **Важно:** осталось совсем немного ограничений (ниже). Запросы, агрегаты, срезы,
> prefetch, доступ по ключу, m2m-мутации, явные транзакции и `@db_session` на корутинах
> в async-режиме работают; синхронная операция в async-сессии поднимает
> `TransactionError` с подсказкой, а не молчаливый `None`.

- PostgreSQL (psycopg3) и MariaDB / MySQL (коннектор `mariadb` 2.0RC); остальные
  диалекты — sync.
- **Синхронная форма доступа по ключу** (`Person[1]` без `await`) в async-сессии
  недоступна: `Person[1]` возвращает awaitable-объект, обращение к нему без `await`
  даёт `TransactionError` с подсказкой. Правильная форма — `await Person[1]`
  (кэш → запрос) или `await Person.get(...)`.
- Коллекции отдают seed-объекты, а удаление объекта с коллекциями требует их
  загрузки — см. «Два правила при работе с коллекциями» выше.
- **Schema-операции** (`generate_mapping`, `create_tables`) — только sync: вызывайте их
  до старта event loop. Внутри async-сессии они дают `TransactionError` (смешение
  режимов), а внутри корутины вне сессии просто блокируют loop.
- Async-генераторы (`async def` с `yield`) декоратор `@db_session` не поддерживает —
  оборачивайте итерацию в `async with db_session:`. Обычные корутины и синхронные
  генераторы — поддерживаются.
- Смешение sync- и async-сессий в одной транзакции не поддерживается.
- Синхронная операция, вызванная в async-сессии, поднимает `TransactionError`
  с подсказкой (раньше в части путей она давала невнятную ошибку или `None`).

Обзор обоих режимов и примеры — в документации (`pony-doc`: `index.rst`,
раздел «Async mode» в `firststeps.rst`).
