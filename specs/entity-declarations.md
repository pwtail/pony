---
id: pony-entity-declarations
type: module-design
title: Декларативные возможности Entity в pony ORM (шпаргалка)
status: draft
tags: [entities, schema, reference]
references: [pony-migrations, pony-introspection]
---

# Назначение

Шпаргалка-обзор всего, что можно объявить в entity-классах pony ORM (этот форк):
атрибуты и их опции, связи, ключи, индексы, ограничения, наследование, типы данных.
Собрано по коду `pony/orm/core.py` (Attribute, Required, Optional, PrimaryKey, Set,
Discriminator, Index, EntityMeta), `pony/orm/dbapiprovider.py` (конвертеры типов)
и `pony/orm/ormtypes.py`. Служит эталоном для интроспекции и diff-миграций:
последний раздел связывает декларации с выводимостью из схемы БД.

# Форма entity

```python
from pony.orm import Database, Required, Optional, PrimaryKey, Set

db = Database(...)

class Person(db.Entity):          # имя класса — с заглавной буквы
    """Человек"""                 # docstring → COMMENT ON TABLE (PostgreSQL)
    id = PrimaryKey(int, auto=True)
    name = Required(str)
```

- `_table_ = 'persons'` — имя таблицы; `_table_ = ('schema', 'persons')` — со схемой;
  по умолчанию имя таблицы выводится из имени класса конвенцией провайдера.
- Наследование — только **STI**: подклассы хранятся в таблице корневого класса;
  задавать свою `_table_` подклассу нельзя; атрибуты подкласса обязаны быть nullable.
- База — `db.Entity`; его можно переопределить (`class Entity(db.Entity): ...`)
  для общих хуков и базовых классов.
- Определять entity после `generate_mapping` нельзя.

# Атрибуты

Виды: `Required` / `Optional` / `PrimaryKey` (обязательный, часть PK) / `Set`
(коллекция) / `Discriminator` (STI-дискриминатор, см. «Наследование»).

```python
id = PrimaryKey(int, auto=True)                     # автоинкремент
age = Required(int, size=32, min=0)                 # параметры типа — позиционно/именно
name = Required(str, 40)                            # max_len позиционно
bio = Optional(str, nullable=True)                  # Optional: NULL в БД
photo = Required(str, column='photo_path')          # имя колонки
```

## Общие опции атрибута

| Опция | Значения | Что даёт |
|---|---|---|
| `column` / `columns` | str / список str | имена колонок (для связи — по числу колонок PK родителя) |
| `sql_type` | str | явный SQL-тип колонки (переопределяет вывод из типа) |
| `auto` | `True` / `'identity'` | автоинкремент PK; `'identity'` — GENERATED AS IDENTITY (PostgreSQL) |
| `default` | значение или callable | python-дефолт (значение присваивается на insert) |
| `sql_default` | SQL-строка / `True` / `False` | DEFAULT в DDL (применяется, если значение не задано) |
| `nullable` | bool | NULL-способность колонки |
| `unique` | bool или имя индекса | UNIQUE (имя — именованный индекс) |
| `index` | bool или имя индекса | обычный индекс (имя — именованный) |
| `using` | `btree`/`hash`/`gin`/`gist`/`brin` | метод индекса (только с `index`/`unique`) |
| `where` | лямбда или функция с одним параметром, либо `raw_sql(...)` | частичный индекс (только с `index`/`unique`; PostgreSQL и SQLite) |
| `comment` | str | COMMENT ON COLUMN (PostgreSQL) |
| `lazy` | bool | ленивая загрузка атрибута |
| `volatile` | bool | значение не кешируется в identity map |
| `optimistic` | bool | оптимистичная проверка неизменности при update |
| `py_check` | callable | проверка значения на Python-уровне |
| `hidden` | bool | скрыть из repr |
| `interleave` | `True`/`False` | INTERLEAVE IN PARENT (CockroachDB) |
| `fk_name` | str | имя FK-констрейнта (на Required-стороне связи) |
| `reverse` | str или Attribute | имя обратного атрибута (связи) |
| `cascade_delete` | bool | ORM-каскад удаления (на одной из сторон связи) |

`PrimaryKey` не принимает `unique`/`using`/`where`; `float` не может быть
PK/unique; `Set` не принимает `auto`/`default`/`unique`/`py_check`.

# Связи

```python
group = Required(Group, reverse='students')   # многие-к-одному (FK-колонки здесь)
students = Set(Group)                          # один-ко-многим
passport = Required(Passport, reverse='person')  # один-к-одному: одна сторона
person = Optional(Person)                         #   без собственных колонок (virtual)
tags = Set(Tag, table='person_tag')            # многие-ко-многим
```

- `Required(Entity)` / `Optional(Entity)` — ссылка на entity; аргументом может быть
  класс, строка-имя (`Required("Department")`) или функция, возвращающая класс
  (forward-ссылка).
- `reverse` — имя/атрибут обратной связи; связь задаётся с одной стороны, вторая
  сторона объявляет `reverse` (или подбирается автоматически, если она одна).
- Один-к-одному: пара `Required`–`Optional`/`Required`; колонки появляются только
  у одной стороны.
- `cascade_delete=True` — на стороне коллекции (или на Required-стороне);
  не на обеих сторонах одновременно. Для m2m — всегда CASCADE.
- У m2m (`Set`+`Set`): `table` — имя таблицы связи (str или `(schema, table)`),
  `column`/`columns` — имена колонок своей стороны, `reverse_column(s)` и
  `reverse_index` — только для симметричных связей (`Set("Person", reverse="friends")`),
  `fk_name` задаётся на reverse-стороне, `reverse_fk_name` — для симметричных,
  `pk_name` — имя PK-констрейнта таблицы связи.

# Составные ключи и индексы

```python
class OrderItem(db.Entity):
    order = Required(Order)
    product = Required(Product)
    qty = Required(int)
    PrimaryKey(order, product, name='pk_order_item')   # составной PK

class A(db.Entity):
    x = Required(int)
    y = Required(str)
    composite_index(x, y, name='ix_a_xy')        # обычный составной индекс
    composite_key(x, y)                          # UNIQUE (устаревший алиас unique)
    unique(x, y, using='btree')                  # составной UNIQUE
```

- `PrimaryKey(a, b, ..., name=...)` — только в теле класса; единичный
  `PrimaryKey(int)` — обычный атрибут. Имя PK-констрейнта задаётся и для
  единичного PK: `PrimaryKey(int, name='pk_person')` — PK выносится в
  `CONSTRAINT ... PRIMARY KEY (...)` (на SQLite несовместимо с `auto`).
- `composite_index`/`unique`/`composite_key(*attrs, name=, using=, where=, include=,
  nulls_not_distinct=)` — функции уровня модуля (`pony.orm`). `using`, `include`
  (INCLUDE-колонки) и `nulls_not_distinct` — PostgreSQL.
- `where` — предикат частичного индекса (PostgreSQL и SQLite): лямбда или
  именованная функция с одним параметром-экземпляром (`lambda x: x.attr > 0`,
  `def positive(x): return x.b > 0`; транслируется в SQL тем же механизмом,
  что и `@constraint.check`) либо сырой SQL через `raw_sql("...")`; готовая
  строка как значение не принимается.
- В ключах индекса допустимы выражения `raw_sql(...)` и порядок `desc(...)`
  (PostgreSQL и SQLite; в SQLite это индексы по выражениям и DESC).
- `unique`-опция одиночного атрибута эквивалентна `unique(attr)`.

# CHECK-ограничения

```python
from pony.orm import constraint

class Person(db.Entity):
    age = Required(int)

    @constraint.check(name='chk_positive_age')   # имя опционально
    def check_age(self):
        return self.age > 0
```

Метод с булевым выражением транслируется в CHECK-констрейнт таблицы;
имя по умолчанию — `chk_<таблица>__<метод>`.

# Наследование (STI) и дискриминатор

```python
class Person(db.Entity):                        # корень
    id = PrimaryKey(int, auto=True)
    classtype = Discriminator(str)              # явный дискриминатор
    name = Required(str)

class Student(Person):
    _discriminator_ = 'student'                 # значение дискриминатора
    grade = Optional(int, nullable=True)        # атрибуты подкласса nullable
```

- Без явного `Discriminator` колонка `classtype` (str) создаётся автоматически,
  значение по умолчанию — имя класса.
- `Discriminator(str|int, column=...)` — только в корневом классе; значение
  только для чтения, задаётся `_discriminator_` в подклассе.
- Множественное наследование — только «ромбовидное» от одного корня.

# Хуки жизненного цикла

```python
class Person(db.Entity):
    def before_insert(self): ...   # и before_update / before_delete
    def after_insert(self): ...    # и after_update / after_delete
```

# Типы данных

| Тип | Параметры | Примечание |
|---|---|---|
| `int` | `size=8/16/24/32/64`, `min`, `max`, `unsigned` | SQL INTEGER/BIGINT/... |
| `str` | `max_len` (позиционно или именем), `autostrip=True`, `db_encoding` | VARCHAR(n) / TEXT |
| `LongStr` | — | CLOB/TEXT без max_len |
| `bytes` / `buffer` | — | BLOB/BYTEA |
| `bool` | — | BOOLEAN |
| `float` | — | DOUBLE/REAL |
| `Decimal` | `precision=12`, `scale` (позиционно: precision, scale) | NUMERIC |
| `date` / `time` / `datetime` / `timedelta` | — | DATE/TIME/TIMESTAMP/INTERVAL |
| `UUID` | — | UUID (PostgreSQL) |
| `Json` | — | dict/list → json/jsonb (PostgreSQL) |
| `IntArray` / `StrArray` / `FloatArray` | — | массивы PostgreSQL |

Ссылки на другие entity — классом, строкой-именем или функцией (см. «Связи»).
`sql_type` позволяет переопределить SQL-тип для любого атрибута.

# Уровень Database (окружение схемы)

- `db.bind(provider, ...)` — провайдер и параметры соединения;
  `Database('sqlite', 'file.sqlite', create_db=True)`.
- `db.generate_mapping(create_tables=..., check_tables=...)` — построение схемы
  из объявлений (однократно); `db.create_tables()`, `db.check_tables()`,
  `db.drop_all_tables(with_all_data=...)`.
- `sql_debug(True)`, `db.last_sql` — отладка SQL; на схему не влияют.

# Имена объектов схемы

Каждый объект схемы можно назвать явно — модель способна описать произвольную
существующую схему, а не только сгенерированную `create_tables()`:

| Объект | Как задать имя |
|---|---|
| Таблица entity | `_table_ = 'имя'` или `('schema', 'имя')` |
| Таблица m2m | `Set(..., table='имя')` |
| Колонка | `column=` / `columns=` (связи и m2m — см. выше) |
| PK, составной | `PrimaryKey(a, b, name='имя')` |
| PK, одна колонка | `PrimaryKey(int, name='имя')` |
| PK таблицы m2m | `Set(..., pk_name='имя')` |
| UNIQUE, атрибут | `unique='имя'` |
| UNIQUE, составной | `unique(a, b, name='имя')` |
| Индекс, атрибут | `index='имя'` |
| Индекс, составной | `composite_index(a, b, name='имя')` |
| Индекс при FK | `index=` на атрибуте связи |
| FOREIGN KEY | `fk_name=` (Required-сторона; m2m — на reverse, `reverse_fk_name` для симметричных) |
| CHECK | `@constraint.check(name='имя')` |
| COMMENT | позиционно (`comment=`, docstring) — имени нет |

Без явного имени pony генерирует своё (`ix_...`, `unq_...`, `fk_...`,
`chk_<таблица>__<метод>`, `<таблица>_pkey`) — но только когда имя не задано.
Имя последовательности `auto`/`identity` не задаётся: его генерирует СУБД,
а pony на него не ссылается (значение берёт через RETURNING).

# Связь с интроспекцией (для миграций)

Выводимо из схемы БД: таблицы → entity; колонки и их SQL-типы → атрибуты;
NOT NULL → `Required`; PK → `PrimaryKey` (serial/identity → `auto`);
FK → `Required` + `Set`; связочная таблица из двух FK → пара `Set`;
UNIQUE → `unique`; индексы → `index` / `composite_index`; SQL DEFAULT →
`sql_default`.

Полный перечень выводимого и невыводимого (с обоснованиями) — в спеке
`pony-introspection` (этап 2 миграций); режим работает только для миграций
данных.
