---
id: pony-apps
type: goal-and-requirements
title: Приложения (app) как схемы PostgreSQL в pony ORM
status: draft
tags: [apps, schema, postgresql, migrations]
references: [pony-migrations, pony-entity-declarations]
---

# Цель

Добавить в pony ORM абстракцию **приложения** (app) в духе Django: `myapp = db.app('myapp')`
группирует сущности в отдельную **схему PostgreSQL**, а миграции ведутся **per-app**.
Надстройка аддитивная: сущности без app ведут себя как раньше.

# Область

- **Только PostgreSQL** (psycopg3), как в `pony-migrations`. MariaDB/MySQL/SQLite — вне.
- app = **реальная схема** Postgres, а не логическая метка: таблицы app живут в схеме с именем app.
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
   имя таблицы строится по `_app_.schema_name`:
   - без `_table_` → `(schema_name, <имя по конвенции провайдера>)`;
   - `_table_` = строка → `(schema_name, <строка>)`;
   - `_table_` = кортеж `(schema, table)` → полное имя как есть, переопределяет app (escape hatch).

3. **Cross-app связи разрешены.** `Required`/`Set` на сущность другого app работает; FK идёт
   между схемами. Имена таблиц/индексов/FK — schema-qualified (механизм уже есть).

4. **Создание схемы.** `CREATE SCHEMA IF NOT EXISTS <schema>` эмитится перед таблицами для
   каждой схемы app, кроме дефолтной `public` провайдера, — и в начальный `.sql` миграций, и в
   легаси-путь `db.create_tables()`/`generate_create_script()`.

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

# Вне области (non-goals)

- MariaDB/MySQL и SQLite — вне (PostgreSQL первым, как в `pony-migrations`).
- «Логические» app без схемы: app в этом форке всегда = схема Postgres.
- Маршрутизация между несколькими БД (multi-database) — все app живут в одной БД.
- Изменение поведения `generate_mapping`/`create_tables`/`drop_*` для сущностей без app.
