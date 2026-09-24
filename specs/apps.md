---
id: pony-apps
type: goal-and-requirements
title: Приложения (application) в pony ORM
status: draft
tags: [apps, schema, postgresql, mysql, sqlite, migrations]
references: [pony-migrations, pony-entity-declarations]
---

# Цель

Добавить в pony ORM абстракцию **приложения** (application) в духе Django:
`myapplication = db.application('myapplication')` группирует сущности, а миграции
ведутся **per-application**. Надстройка аддитивная: сущности без application
работают как в обычном pony, но в миграции не попадают.

# Область

- Смысл application зависит от диалекта:
  - **PostgreSQL** — application = **реальная схема**: таблицы application живут
    в схеме с именем application (`_table_ = (schema, table)`);
  - **MySQL/MariaDB и SQLite** — application = **логическая группировка**:
    схемы/БД не создаются, таблицы сохраняют обычные имена, application влияет
    только на каталог миграций (`migrations/<application>/`) и колонку `app`
    в `pony_migrations` (решение 8).
- Переиспользует существующие schema-qualified имена
  (`_table_ = (schema, table)`, `split_table_name`) и движок миграций
  `pony-migrations`; существующий API
  (`generate_mapping`/`create_tables`/`drop_*`) не меняется.

# Принятые решения

1. **API.** `db.application(name, schema=name)` регистрирует и возвращает объект
   `Application`; тот же объект доступен как атрибут базы `db.<name>`
   (`db.myapplication`):
   - `.Entity` — базовый класс сущностей application (подкласс `db.Entity`,
     помечает `_app_`); сущности application доступны как `app.<Имя>`;
   - `.schema_name` — имя схемы (по умолчанию = имя application, переопределяется
     `schema=`);
   - `.migrations` — фасад миграций этого application.
   Имя application не должно конфликтовать с существующими атрибутами `Database`
   (`schema`, `Entity`, `provider`, ...) — на конфликте `db.application()` бросает
   ошибку.

2. **Принадлежность сущности.** `class Account(myapplication.Entity): ...`. При
   `generate_mapping` имя таблицы строится по `_app_`:
   - **PostgreSQL**: без `_table_` → `(schema_name, <имя по конвенции провайдера>)`;
     `_table_` = строка → `(schema_name, <строка>)`;
   - **MySQL/SQLite**: имя таблицы остаётся обычным (без квалификации) —
     `schema_name` не используется для имён;
   - `_table_` = кортеж `(schema, table)` → полное имя как есть, переопределяет
     application (escape hatch, любой диалект).

3. **Cross-application связи разрешены.** `Required`/`Set` на сущность другого
   application работает; в PostgreSQL FK идёт между схемами (имена
   таблиц/индексов/FK — schema-qualified), в MySQL/SQLite — обычный FK внутри
   одной БД. Зависимость миграций между applications отслеживается в шапке
   `-- depends: <application>/<файл>` (решение 6).

4. **Создание схемы (только PostgreSQL).** `CREATE SCHEMA IF NOT EXISTS <schema>`
   эмитится перед таблицами для каждой схемы application, кроме дефолтной
   `public` провайдера, — и в начальный `.sql` миграций, и в легаси-путь
   `db.create_tables()`/`generate_create_script()`. Для MySQL/SQLite схемы не
   создаются (имена таблиц — строки, `get_schema_names()` пуст).

5. **Миграции per-application.** Каталог `migrations/<application>/`. Трекинг —
   общая таблица `pony_migrations` с колонкой `app` (PK `(app, name)`). Корня нет:
   без application агрегатные команды идут по всем applications, а сущности без
   application в миграции не попадают. У каждого application свой граф, ровно одна
   голова на application (топосорт/merge — как в `pony-migration-graph`).

6. **Cross-application зависимости миграций.** В шапке
   `-- depends: <файл>[, <application>/<файл>]` (`# depends:` для `.py`); голое
   `<файл>` — свой application, `<application>/<файл>` — миграция другого
   application (`depends: 003.sql, myapplication/0002.sql`). Если сущности
   application ссылаются на другой application (FK/m2m), `make` автоматически
   прописывает зависимость от головы (или `0001_initial`) этого application.

7. **Фасады и CLI.** `db.migrations` — агрегат по всем applications
   (`apply`/`plan`/`make`/`merge` идут по всем applications в порядке
   зависимостей); `myapplication.migrations` — только этот application. CLI:
   `pony migrations <application> <command>` — application позиционным аргументом
   перед командой (`pony migrations myapplication make`), команды
   `make`/`apply`/`plan`/`merge`; без application `pony migrations <command>`
   идёт по всем applications.

8. **Логическая группировка в MySQL/MariaDB и SQLite** (решение пользователя).
   Схемы/БД не создаются; таблицы application живут в текущей БД с обычными
   именами; application влияет только на каталог миграций и колонку `app`
   в трекинге. Принадлежность таблицы к application для миграций определяется по
   `entity._app_` (а не по имени схемы). Одинаковое имя таблицы в двух
   applications — ошибка маппинга. `schema_name` сохраняется в API
   (default = имя application, `schema=` переопределяет), но для этих диалектов
   в именах таблиц не участвует.

# Вне области (non-goals)

- Маршрутизация между несколькими БД (multi-database) — все applications живут
  в одной БД.
- Изменение поведения `generate_mapping`/`create_tables`/`drop_*` для сущностей
  без application.
