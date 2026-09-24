---
id: pony-apps
type: goal-and-requirements
title: Приложения (app) в pony ORM
status: draft
tags: [apps, schema, postgresql, mysql, sqlite, migrations]
references: [pony-migrations, pony-entity-declarations]
---

# Цель

Добавить в pony ORM абстракцию **приложения** (app) в духе Django: `myapp = db.app('myapp')`
группирует сущности, а миграции ведутся **per-app**.
Надстройка аддитивная: сущности без app ведут себя как раньше.

# Область

- Смысл app зависит от диалекта:
  - **PostgreSQL** — app = **реальная схема**: таблицы app живут в схеме с именем app
    (`_table_ = (schema, table)`);
  - **MySQL/MariaDB и SQLite** — app = **логическая группировка**: схемы/БД не создаются,
    таблицы сохраняют обычные имена, app влияет только на каталог миграций (`migrations/<app>/`)
    и колонку `app` в `pony_migrations` (решение 8).
- Переиспользует существующие schema-qualified имена (`_table_ = (schema, table)`, `split_table_name`)
  и движок миграций `pony-migrations`; существующий API (`generate_mapping`/`create_tables`/`drop_*`)
  не меняется.

# Принятые решения

1. **API.** `db.app(name, schema=name)` регистрирует и возвращает объект `Application`;
   тот же объект доступен как атрибут базы `db.<name>` (`db.myapp`):
   - `.Entity` — базовый класс сущностей app (подкласс `db.Entity`, помечает `_app_`);
   - `.schema_name` — имя схемы (по умолчанию = имя app, переопределяется `schema=`);
   - `.migrations` — фасад миграций этого app.
   Имя app не должно конфликтовать с существующими атрибутами `Database`
   (`schema`, `Entity`, `provider`, ...) — на конфликте `db.app()` бросает ошибку.

2. **Принадлежность сущности.** `class Account(myapp.Entity): ...`. При `generate_mapping`
   имя таблицы строится по `_app_`:
   - **PostgreSQL**: без `_table_` → `(schema_name, <имя по конвенции провайдера>)`;
     `_table_` = строка → `(schema_name, <строка>)`;
   - **MySQL/SQLite**: имя таблицы остаётся обычным (без квалификации) — `schema_name`
     не используется для имён;
   - `_table_` = кортеж `(schema, table)` → полное имя как есть, переопределяет app
     (escape hatch, любой диалект).

3. **Cross-app связи разрешены.** `Required`/`Set` на сущность другого app работает; в
   PostgreSQL FK идёт между схемами (имена таблиц/индексов/FK — schema-qualified), в
   MySQL/SQLite — обычный FK внутри одной БД. Зависимость миграций между app отслеживается
   в шапке `-- depends: <app>/<файл>` (решение 6).

4. **Создание схемы (только PostgreSQL).** `CREATE SCHEMA IF NOT EXISTS <schema>` эмитится
   перед таблицами для каждой схемы app, кроме дефолтной `public` провайдера, — и в начальный
   `.sql` миграций, и в легаси-путь `db.create_tables()`/`generate_create_script()`. Для
   MySQL/SQLite схемы не создаются (имена таблиц — строки, `get_schema_names()` пуст).

5. **Миграции per-app.** Каталог `migrations/<app>/`. Трекинг — общая таблица `pony_migrations`
   с колонкой `app` (PK `(app, name)`); миграции без app (легаси) — `app = ''`. У каждого app
   свой граф, ровно одна голова на app (топосорт/merge — как в `pony-migration-graph`).

6. **Cross-app зависимости миграций.** В шапке `-- depends: <файл>[, <app>/<файл>]`
   (`# depends:` для `.py`); голое `<файл>` — свой app, `<app>/<файл>` — миграция другого
   app (`depends: 003.sql, myapp/0002.sql`). Если сущности app ссылаются на другой app
   (FK/m2m), `add` автоматически прописывает зависимость от головы (или `0001_initial`)
   этого app.

7. **Фасады и CLI.** `db.migrations` — агрегат по всем app (`apply`/`plan` применяют все app
   в порядке зависимостей); `myapp.migrations` — только этот app. CLI:
   `pony-migrate <app> <command>` — app позиционным аргументом перед командой
   (`pony-migrate myapp apply`), дальше существующая команда `add`/`apply`/`plan`/`merge`;
   без app `pony-migrate <command>` применяет все app.

8. **Логическая группировка в MySQL/MariaDB и SQLite** (решение пользователя). Схемы/БД не
   создаются; таблицы app живут в текущей БД с обычными именами; app влияет только на каталог
   миграций и колонку `app` в трекинге. Принадлежность таблицы к app для миграций определяется
   по `entity._app_` (а не по имени схемы). Одинаковое имя таблицы в двух app — ошибка маппинга.
   `schema_name` сохраняется в API (default = имя app, `schema=` переопределяет), но для этих
   диалектов в именах таблиц не участвует.

# Вне области (non-goals)

- Маршрутизация между несколькими БД (multi-database) — все app живут в одной БД.
- Изменение поведения `generate_mapping`/`create_tables`/`drop_*` для сущностей без app.
